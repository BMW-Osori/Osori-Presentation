"""채팅 답변: 질문 유형을 가려 본문(제목·요약·표·가이드)은 코드가 바로 쓰고, AI 에는 끝의 1~2문장 분석만 맡김.

AI 서버(CPU 2코어)는 초당 약 10토큰이라 긴 답을 모델이 쓰면 1분 가까이 걸림. 숫자·거리·RSSI 해석은 여기서 계산해 틀리지 않게 하고,
AI 에는 짧은 데이터와 그 데이터로 쓴 기준 분석을 함께 만듦: 기준 분석은 학습 정답이자 모델이 실패했을 때 대신 보여 주는 문장.
학습 데이터(ai/make_chat_dataset.py)도 이 모듈로 만들어 서빙과 형식이 같음.
"""
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

RADIUS_M, WINDOW_MIN, MIN_COUNT = 300, 15, 3  # 동시 다발 구역: 300m·15분 안에 3건 이상
ROOT = Path(__file__).parent
ACCIDENTS = json.loads((ROOT / "accident_data.json").read_text())["zones"]  # 구역 이름 → 사고 유형·사상자 (백엔드에 없는 공공데이터 항목)
NEAR_M = 1000  # 채팅에서 "내 주변"으로 보는 거리
NEAR_WORDS = r"주변|근처|여기|내 위치|현재 위치|지금 위치|이 근방|방금|^(지금 )?위험해"  # 현재 위치를 묻는 질문
RECENT_WORDS = r"최근|마지막|최신|방금"  # 가장 최근 감지를 묻는 질문 (방금은 위치를 모를 때)
ANALYSIS_SYSTEM = (
    "당신은 '보행자 촉각 경고' 시스템의 Safety AI입니다. 질문과 서버가 계산한 데이터를 보고 "
    "보행자에게 위험을 1~2문장으로 분석합니다. 마크다운 없이 평문으로 쓰고, 숫자는 데이터에 있는 값만 씁니다."
)
hhmm = lambda t: t[11:16]


@dataclass
class Reply:
    kind: str
    body: str  # 제목부터 행동 가이드까지 (바로 보냄)
    note: str  # 맨 끝 출처 한 줄
    data: str | None = None  # AI 분석에 줄 데이터. None 이면 AI 없이 끝
    reference: str | None = None  # data 로 쓴 기준 분석


def analysis_messages(question: str, data: str) -> list:
    """AI 분석 요청. 시스템 프롬프트가 늘 같아 Ollama 가 앞부분을 캐시하고, 바뀌는 질문·데이터만 새로 읽음."""
    return [{"role": "system", "content": ANALYSIS_SYSTEM}, {"role": "user", "content": f"질문: {question}\n\n데이터:\n{data}"}]


def data_note(now=None) -> str:
    return f"*데이터 기준: {(now or datetime.now()):%Y-%m-%d %H:%M} 실시간 감지, 한국도로교통공단 사고다발지역*"


# ---- 위치·묶음

def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def events_by_zone(zones: list, events: list) -> dict:
    """이벤트를 반경 안에서 가장 가까운 위험구역에 묶음. {구역 이름: [이벤트]}, 어느 구역에도 안 들면 제외."""
    # ponytail: O(구역 × 이벤트), 이벤트 수십만 건이면 Spring 쪽 집계 쿼리로
    grouped = {z["name"]: [] for z in zones}
    for e in events:
        near = [(distance_m(e["latitude"], e["longitude"], z["latitude"], z["longitude"]), z) for z in zones if abs(e["latitude"] - z["latitude"]) < .01]
        near = [(d, z) for d, z in near if d <= z["radius"]]
        if near:
            grouped[min(near, key=lambda n: n[0])[1]["name"]].append(e)
    return grouped


def bursts(zones: list, events: list) -> list:
    """가까운 곳·가까운 시간에 몰린 이벤트를 묶어 동시 다발 구역으로 반환."""
    # ponytail: O(n²) 쌍 비교, 이벤트 수천 건 넘으면 시간 창으로 먼저 자르기
    parent = list(range(len(events)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    times = [datetime.fromisoformat(e["startedAt"]) for e in events]
    for i in range(len(events)):
        for j in range(i + 1, len(events)):
            near = distance_m(events[i]["latitude"], events[i]["longitude"], events[j]["latitude"], events[j]["longitude"]) <= RADIUS_M
            if abs(times[i] - times[j]) <= timedelta(minutes=WINDOW_MIN) and near:
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(len(events)):
        groups[find(i)].append(i)
    name_of = {e["eventId"]: name for name, es in events_by_zone(zones, events).items() for e in es}
    result = []
    for g in groups.values():
        if len(g) < MIN_COUNT:
            continue
        es = [events[i] for i in g]
        result.append({
            "zone": Counter(name_of.get(e["eventId"], "구역 밖") for e in es).most_common(1)[0][0],
            "eventIds": [e["eventId"] for e in es],
            "count": len(es),
            "latitude": sum(e["latitude"] for e in es) / len(es),
            "longitude": sum(e["longitude"] for e in es) / len(es),
            "start": min(times[i] for i in g).isoformat(),
            "end": max(times[i] for i in g).isoformat(),
        })
    return sorted(result, key=lambda z: -z["count"])


def place_words(name: str) -> tuple[list, list]:
    """'경기도 성남시 중원구 성남동(모란시장사거리 부근)' → (['모란시장사거리', '모란시장'], ['경기', '성남시', '성남', ...]). 앞은 지점, 뒤는 지역."""
    head, _, rest = name.partition("(")
    spot = rest.rstrip(")").removesuffix(" 부근").strip()
    spots = [spot, re.sub(r"(사거리|삼거리|오거리|네거리|교차로|입구|앞)$", "", spot)] if spot else []
    region = []
    for word in head.split():
        region += [word, re.sub(r"(특별자치시|특별자치도|특별시|광역시|시|군|구|읍|면|동|도)$", "", word)]
    return [w for w in spots if len(w) >= 3], [w for w in region if len(w) >= 2]  # '시장' 같은 짧은 지점 이름은 다른 지역까지 걸림


def find_zones(question: str, zones: list) -> tuple[str, list, str]:
    """질문에 나온 지점 → 지역 순으로 위험구역을 찾음. (지점|지역, 위험도 높은 5곳, 질문에서 걸린 말)"""
    found = {"spot": [], "region": []}  # (걸린 말, 구역)
    for z in zones:
        spots, regions = place_words(z["name"])
        if hit := [w for w in spots if w in question]:
            found["spot"].append((max(hit, key=len), z))
        elif hit := [w for w in regions if w in question]:
            found["region"].append((max(hit, key=len), z))
    kind = "spot" if found["spot"] else "region"
    # 가장 길게 걸린 말만: '강남구'를 물으면 '남구'(부산·대구 남구)는 빼야 함
    word = max((w for w, _ in found[kind]), key=len, default="")
    picked = sorted((z for w, z in found[kind] if len(w) == len(word)), key=lambda z: -z["riskScore"])[:5]
    return kind, picked, word


def match_zones(question: str, zones: list) -> list:
    return find_zones(question, zones)[1]


def short(name: str) -> str:
    """AI 데이터·분석에 쓰는 짧은 이름: 지점 이름이 있으면 그것만."""
    spots = place_words(name)[0]
    return spots[0] if spots else name


# ---- 해석: 기준은 여기 한 곳 (본문 표·AI 데이터·기준 분석이 같은 말을 씀)

def dist_tag(d):
    return "차량이 바로 옆" if d < 3 else "가까이 접근" if d < 6 else "아직 여유"


def signal_tag(r):
    return "강함" if r >= -60 else "중간" if r >= -75 else "약함"


def count_tag(c):
    return "계속 접근" if c >= 5 else "이어짐" if c >= 3 else "일시적"


CAME = {"차량이 바로 옆": "차량이 바로 옆까지 왔고", "가까이 접근": "차량이 가까이 다가왔고", "아직 여유": "아직 거리에 여유가 있었고"}
REACHED = {"차량이 바로 옆": "바로 옆까지 왔습니다", "가까이 접근": "가까이 다가왔습니다", "아직 여유": "아직 거리에 여유가 있었습니다"}
WAS = {"강함": "강했습니다", "중간": "중간 세기였습니다", "약함": "약했습니다"}
IS = {"강함": "강한 편입니다", "중간": "중간 세기입니다", "약함": "약한 편입니다"}
MEANS = {"차량이 바로 옆": "차량이 바로 옆에 있다는 뜻", "가까이 접근": "차량이 가까이 다가왔다는 뜻", "아직 여유": "아직 거리에 여유가 있다는 뜻"}
COUNT_MEANS = {"계속 접근": "차량이 계속 머물거나 다가오고 있다는 뜻", "이어짐": "접근이 이어지고 있다는 뜻", "일시적": "일시적인 접근일 수 있다는 뜻"}
ADVICE = {"HIGH": "걸음을 멈추고 앞뒤 차량부터 확인하세요", "MEDIUM": "이어폰 볼륨을 낮추고 좌우를 한 번 더 확인하세요", "LOW": "진입로 근처에서는 속도를 줄이세요"}


def advice(level) -> str:
    """AI 데이터용 권장 행동 줄: 등급에 맞는 문장을 모델이 고르게 두면 다른 등급의 문장을 씀."""
    return f"\n- 권장 행동: {ADVICE[level]}"


def signal(e, at=hhmm) -> str:
    """AI 데이터용 감지 한 줄: 값마다 해석을 붙여 모델이 기준을 따지지 않게."""
    d, r, c = e["minEstimatedDistance"], e["maxRssi"], e["consecutiveCount"]
    return f"{at(e['startedAt'])} · 거리 {d}m({dist_tag(d)}) · RSSI {r}dBm({signal_tag(r)}) · 연속 {c}회({count_tag(c)}) · {e['alertLevel']}"


# ---- 본문 조각

GUIDE = {
    "HIGH": ["즉시 걸음을 멈추고 앞뒤 차량을 확인하세요.", "이어폰을 빼거나 볼륨을 최소로 낮추세요.", "인도 안쪽이나 건물 쪽으로 이동해 차량과 거리를 두세요."],
    "MEDIUM": ["이어폰 볼륨을 낮추고 주변 소리에 귀 기울이세요.", "횡단보도·주차장 출입구에서는 좌우를 한 번 더 확인하세요.", "진동이 반복되면 잠시 멈춰 차량 위치를 확인하세요."],
    "LOW": ["평소처럼 이동하되 차량 진입로 근처에서는 속도를 줄이세요.", "휴대폰 화면만 보며 걷지 마세요.", "진동이 연속으로 오면 바로 주변을 확인하세요."],
}


def guide(level):
    return "\n".join(f"{n}. {g}" for n, g in enumerate(GUIDE[level], 1))


def harm_rows(zone):
    d = ACCIDENTS.get(zone["name"])
    return f"\n| 사고 유형 | {'·'.join(d['types'])} |\n| 사상자 | {d['casualties']}명 |\n| 사망자 | {d['deaths']}명 |" if d else ""


def harm_text(zone):
    d = ACCIDENTS.get(zone["name"])
    return f"({'·'.join(d['types'])}, 사상자 {d['casualties']}명, 사망 {d['deaths']}명)" if d else ""


def timeline(es):
    return "\n".join(f"- **{hhmm(e['startedAt'])}** · 거리 {e['minEstimatedDistance']}m · RSSI {e['maxRssi']}dBm · 연속 {e['consecutiveCount']}회 · {e['alertLevel']}" for e in es)


def zone_table(picked, grouped):
    return "| 구역 | 구역 위험도 | 누적 사고 | 감지 |\n| --- | --- | --- | --- |\n" + "\n".join(
        f"| {z['name']} | {z['riskScore']}점 ({z['riskLevel']}) | {z['accidentCount']}건 | {len(grouped[z['name']])}건 |" for z in picked)


def zone_line(z):
    return f"{short(z['name'])} · 위험도 {z['riskScore']}점({z['riskLevel']}) · 누적 사고 {z['accidentCount']}건{harm_text(z)}"


# ---- 질문 유형별 답

def zone_reply(zone, es, burst, note):
    name, level = zone["name"], zone["riskLevel"]
    rows = f"| 구역 위험도 | {zone['riskScore']}점 ({level}) |\n| 누적 사고 | {zone['accidentCount']}건 |{harm_rows(zone)}"
    d = ACCIDENTS.get(name)
    if not es:
        body = f"""## 📍 {name} 위험도 리포트

> 💡 {name}의 구역 위험도는 **{zone['riskScore']}점, {level}**입니다. 실시간 감지 기록은 아직 없습니다.

### 📊 현황

| 항목 | 값 |
| --- | --- |
{rows}
| 감지 건수 | 0건 |

### ⚠️ 알아 두세요

위험도는 한국도로교통공단 사고다발지역의 사고 내역으로 계산한 값입니다. 지금 감지된 차량이 없어도 사고가 반복된 곳이니 주의하세요.

### ✅ 행동 가이드

{guide(level)}"""
        data = f"- 구역: {zone_line(zone)}\n- 감지 0건{advice(level)}"
        ref = (f"{short(name)}에는 지금 감지된 차량이 없지만, 누적 사고 {zone['accidentCount']}건" + (f"(사상자 {d['casualties']}명)" if d else "")
               + f"이 난 곳입니다. {ADVICE[level]}.")
        return Reply("quiet", body, note, data, ref)
    b = next((b for b in burst if b["zone"] == name), None)
    span = f"{hhmm(b['start'])}–{hhmm(b['end'])} · {b['count']}건" if b else "해당 없음"
    closest = min(es, key=lambda e: e["minEstimatedDistance"])
    body = f"""## 📍 {name} 위험도 리포트

> 💡 {name}에서 차량 접근이 **{len(es)}건** 감지됐고, 구역 위험도는 **{zone['riskScore']}점, {level}**입니다.{f" {hhmm(b['start'])}–{hhmm(b['end'])} 사이 {b['count']}건이 몰린 동시 다발 구역이에요." if b else ""}

### 📊 현황

| 항목 | 값 |
| --- | --- |
| 감지 건수 | {len(es)}건 |
{rows}
| 동시 다발 | {span} |

### 🕒 감지 기록

{timeline(es)}

### ✅ 행동 가이드

{guide(level)}"""
    dt, st = dist_tag(closest["minEstimatedDistance"]), signal_tag(closest["maxRssi"])
    data = f"- 구역: {zone_line(zone)}\n- 감지 {len(es)}건 · 가장 가까운 감지 {signal(closest)}\n- 동시 다발: {span}{advice(level)}"
    ref = (f"{short(name)}에서는 {hhmm(closest['startedAt'])} 감지가 {closest['minEstimatedDistance']}m로 {CAME[dt]}, 신호도 {WAS[st]}. "
           + (f"{hhmm(b['start'])}–{hhmm(b['end'])} 사이 {b['count']}건이 몰린 동시 다발 구역이기도 합니다. " if b else "")
           + f"누적 사고 {zone['accidentCount']}건이 난 곳이니 {ADVICE[level]}.")
    return Reply("zone", body, note, data, ref)


def near_reply(picked, grouped, close, note):
    close_line = (f"내 위치 {NEAR_M}m 안 감지 기록은 **{len(close)}건**입니다.\n\n" + timeline(close[-5:])) if close else f"내 위치 {NEAR_M}m 안 감지 기록은 **0건**입니다."
    nearest = min(close, key=lambda e: e["minEstimatedDistance"]) if close else None
    seen = (f" · 가장 가까운 감지 {signal(nearest)}" if nearest else "")
    seen_ref = (f"주변 감지 중 가장 가까운 차량은 {nearest['minEstimatedDistance']}m로 {REACHED[dist_tag(nearest['minEstimatedDistance'])]}. " if nearest
                else "주변에서 감지된 차량은 없습니다. ")
    if not picked:
        body = f"""## 📍 내 주변 위험구역

> 💡 내 위치 {NEAR_M}m 안에 등록된 사고다발지역은 **0곳**입니다.

### 🕒 내 주변 감지

{close_line}

### ⚠️ 알아 두세요

사고다발지역이 아니어도 저소음 차량은 다가올 수 있습니다. 진동이 울리면 바로 주변을 확인하세요.

### ✅ 행동 가이드

{guide("LOW")}"""
        data = f"- 내 위치 {NEAR_M}m 안 위험구역 0곳\n- 내 위치 {NEAR_M}m 안 감지 {len(close)}건{seen}"
        ref = f"내 주변 {NEAR_M}m 안에는 등록된 사고다발지역이 없습니다. {seen_ref}저소음 차량은 어디서든 다가올 수 있으니 진동이 오면 바로 주변을 확인하세요."
        return Reply("near", body, note, data, ref)
    top = max(picked, key=lambda z: z["riskScore"])
    body = f"""## 📍 내 주변 위험구역

> 💡 내 위치 {NEAR_M}m 안 위험구역은 **{len(picked)}곳**이고, 가장 위험한 곳은 **{top['name']}, {top['riskScore']}점**입니다.

### 📊 구역 목록

{zone_table(picked, grouped)}

### 🕒 내 주변 감지

{close_line}

### ✅ 행동 가이드

{guide(top['riskLevel'])}"""
    data = f"- 내 위치 {NEAR_M}m 안 위험구역 {len(picked)}곳 · 가장 위험한 곳 {short(top['name'])} {top['riskScore']}점({top['riskLevel']})\n- 내 위치 {NEAR_M}m 안 감지 {len(close)}건{seen}{advice(top['riskLevel'])}"
    ref = f"내 주변 {NEAR_M}m 안 위험구역은 {len(picked)}곳이고, 가장 위험한 곳은 {short(top['name'])}({top['riskScore']}점)입니다. {seen_ref}{ADVICE[top['riskLevel']]}."
    return Reply("near", body, note, data, ref)


def region_reply(label, picked, grouped, note):
    top = picked[0]
    body = f"""## 🗺️ {label} 위험구역

> 💡 {label}에서 위험도가 가장 높은 곳은 **{top['name']}, {top['riskScore']}점**입니다.

### 📊 위험도 높은 구역

{zone_table(picked, grouped)}

### ✅ 행동 가이드

{guide(top['riskLevel'])}"""
    live = sum(bool(grouped[z["name"]]) for z in picked)
    data = (f"- 지역: {label} · 위험도 높은 구역: " + " / ".join(f"{short(z['name'])} {z['riskScore']}점({z['riskLevel']}, 사고 {z['accidentCount']}건)" for z in picked[:3])
            + f"\n- 그중 실시간 감지가 있는 구역: {f'{live}곳' if live else '없음'}")
    ref = (f"{label}에서는 {short(top['name'])}({top['riskScore']}점)이 가장 위험" + (f"하고, {short(picked[1]['name'])}({picked[1]['riskScore']}점)이 뒤를 잇습니다." if len(picked) > 1 else "합니다.")
           + (f" 이 중 {live}곳에서 실시간 감지가 있었습니다." if live else " 아직 이 구역들에서 실시간 감지는 없습니다."))
    return Reply("region", body, note, data, ref)


def burst_reply(burst, by_name, note):
    if not burst:
        body = """## 🗺️ 동시 다발 구역

> 💡 현재 동시 다발 구역은 **0곳**입니다.

### ✅ 행동 가이드

1. 평소처럼 이동하되 차량 진입로 근처에서는 속도를 줄이세요.
2. 진동이 연속으로 오면 걸음을 멈추고 차량이 지나갈 때까지 기다리세요."""
        return Reply("burst", body, note, "- 동시 다발 구역 0곳", "지금은 짧은 시간에 감지가 몰린 곳이 없습니다. 그래도 진동이 연속으로 오면 걸음을 멈추고 주변을 확인하세요.")
    rows = "\n".join(f"| {b['zone']} | {hhmm(b['start'])}–{hhmm(b['end'])} | {b['count']}건 | " + (f"{by_name[b['zone']]['riskScore']}점" if b["zone"] in by_name else "–") + " |" for b in burst)
    body = f"""## 🗺️ 동시 다발 구역

> 💡 짧은 시간에 감지가 몰린 **동시 다발 구역은 {len(burst)}곳**입니다. 이 구역에서는 이어폰 볼륨을 낮추고 전방을 주시하세요.

### 📊 구역 목록

| 구역 | 시간 | 건수 | 구역 위험도 |
| --- | --- | --- | --- |
{rows}

### ⚠️ 왜 위험한가요?

짧은 시간에 저소음 차량이 여러 번 접근한 곳입니다. 진동 경고가 연달아 울려 대응이 늦어지기 쉽습니다.

### ✅ 행동 가이드

1. 해당 시간대에는 가능하면 다른 길로 우회하세요.
2. 구역에 들어서면 이어폰 볼륨을 낮추고 휴대폰 화면에서 눈을 떼세요.
3. 진동이 연속으로 오면 걸음을 멈추고 차량이 지나갈 때까지 기다리세요."""
    b = burst[0]
    where, span = short(b["zone"]), f"{hhmm(b['start'])}–{hhmm(b['end'])}"
    data = f"- 동시 다발 구역 {len(burst)}곳 · 가장 많은 곳: {where} {span} {b['count']}건"
    ref = f"짧은 시간에 감지가 몰린 곳은 {len(burst)}곳이고, {where}에서 {span} 사이 {b['count']}건으로 가장 많았습니다. 그 시간대에는 가능하면 다른 길로 돌아가세요."
    return Reply("burst", body, note, data, ref)


def top_reply(live, grouped, note):
    if not live:
        body = """## 🔥 지금 가장 위험한 구역

> 💡 아직 실시간 감지가 있는 구역이 **0곳**입니다. 사고 이력으로 본 위험도는 지역 이름으로 물어보세요.

### ✅ 행동 가이드

1. "성남 어디가 위험해?"처럼 지역 이름으로 물어보세요.
2. 진동이 연속으로 오면 바로 주변을 확인하세요."""
        return Reply("top", body, note)
    rows = "\n".join(f"| {n} | {z['name']} | {z['riskScore']}점 | {len(grouped[z['name']])}건 | {z['accidentCount']}건 |" for n, z in enumerate(live, 1))
    body = f"""## 🔥 지금 가장 위험한 구역

> 💡 감지 기록이 있는 구역 중 위험도가 가장 높은 곳은 **{live[0]['name']}, {live[0]['riskScore']}점**입니다.

### 📊 순위

| 순위 | 구역 | 구역 위험도 | 감지 건수 | 누적 사고 |
| --- | --- | --- | --- | --- |
{rows}

### ✅ 행동 가이드

{guide(live[0]['riskLevel'])}"""
    z = live[0]
    data = "- 감지가 있는 구역 위험도 순: " + " / ".join(f"{short(z['name'])} {z['riskScore']}점({z['riskLevel']}, 감지 {len(grouped[z['name']])}건)" for z in live) + advice(z["riskLevel"])
    ref = f"감지가 있는 구역 중에서는 {short(z['name'])}({z['riskScore']}점, 감지 {len(grouped[z['name']])}건)이 가장 위험합니다. {ADVICE[z['riskLevel']]}."
    return Reply("top", body, note, data, ref)


def summary_reply(zones, events, burst, note):
    alerts, risk = Counter(e["alertLevel"] for e in events), Counter(z["riskLevel"] for z in zones)
    bullets = "\n".join(f"- **{z['zone']}** · {hhmm(z['start'])}–{hhmm(z['end'])} · {z['count']}건" for z in burst) or "- 없음"
    body = f"""## 📋 보행 안전 현황 요약

> 💡 차량 접근 **{len(events)}건**, 그중 DANGER **{alerts['DANGER']}건**입니다. 위험구역 {len(zones)}곳 중 HIGH는 **{risk['HIGH']}곳**이고, 동시 다발 구역은 **{len(burst)}곳**이에요.

### 📊 전체 지표

| 항목 | 값 |
| --- | --- |
| 총 감지 | {len(events)}건 |
| DANGER | {alerts['DANGER']}건 |
| CAUTION | {alerts['CAUTION']}건 |
| 위험구역 | {len(zones)}곳 |
| HIGH 구역 | {risk['HIGH']}곳 |
| MEDIUM 구역 | {risk['MEDIUM']}곳 |
| LOW 구역 | {risk['LOW']}곳 |

### 🗺️ 동시 다발 구역

{bullets}

### ✅ 안전 수칙

{guide("MEDIUM")}"""
    pct = round(100 * alerts["DANGER"] / len(events)) if events else 0
    data = (f"- 전체 감지 {len(events)}건 · DANGER {alerts['DANGER']}건({pct}%) · CAUTION {alerts['CAUTION']}건\n"
            f"- 위험구역 {len(zones)}곳 · HIGH {risk['HIGH']}곳 · 동시 다발 구역 {len(burst)}곳")
    ref = (f"지금까지 감지 {len(events)}건 중 DANGER가 {alerts['DANGER']}건({pct}%)입니다. "
           + (f"동시 다발 구역도 {len(burst)}곳 있으니 지도 탭에서 미리 확인하세요." if burst else "동시 다발 구역은 없습니다."))
    return Reply("summary", body, note, data, ref)


def hours_reply(events, note):
    by_hour = Counter(int(e["startedAt"][11:13]) for e in events if e["alertLevel"] == "DANGER")
    if not by_hour:
        return Reply("hours", "## 🕒 위험 시간대 분석\n\n> 💡 지금까지 DANGER 감지가 없어 위험 시간대를 꼽을 수 없습니다.", note)
    peak, n = by_hour.most_common(1)[0]
    rows = "\n".join(f"| {h}시 | {c}건 |" for h, c in sorted(by_hour.items()))
    body = f"""## 🕒 위험 시간대 분석

> 💡 DANGER가 가장 많았던 시간대는 **{peak}시, {n}건**입니다.

### 📊 시간대별 DANGER 건수

| 시간 | DANGER |
| --- | --- |
{rows}

### ✅ 행동 가이드

{guide("HIGH")}"""
    data = f"- DANGER 가장 많은 시간: {peak}시 {n}건 · 전체 DANGER {sum(by_hour.values())}건"
    ref = f"DANGER가 가장 많았던 때는 {peak}시({n}건)입니다. 이 시간대에 걸을 때는 이어폰 볼륨을 낮추고 전방을 주시하세요."
    return Reply("hours", body, note, data, ref)


def recent_reply(events, grouped, note):
    if not events:
        return Reply("recent", f"## 🕒 최근 감지 기록\n\n> 💡 아직 감지 기록이 **0건**입니다.\n\n### ✅ 행동 가이드\n\n{guide('LOW')}", note)
    e = events[-1]
    zone = {e["eventId"]: name for name, es in grouped.items() for e in es}.get(e["eventId"])
    at = f"{e['startedAt'][5:10]} {hhmm(e['startedAt'])}"
    d = e["minEstimatedDistance"]
    body = f"""## 🕒 최근 감지 기록

> 💡 가장 최근 감지는 **{at}** {zone or '사고다발지역 밖'}에서 난 **{e['alertLevel']}**입니다.

### 📊 감지 정보

| 항목 | 값 |
| --- | --- |
| 시각 | {at} |
| 위치 | {zone or '구역 밖'} |
| 추정 거리 | {d}m |
| 신호 세기 (RSSI) | {e['maxRssi']}dBm |
| 연속 감지 | {e['consecutiveCount']}회 |
| 경고 단계 | {e['alertLevel']} |

### ⚠️ 알아 두세요

'구역 밖'은 사고다발지역으로 등록되지 않은 곳이지만, 저소음 차량은 어디서든 다가올 수 있습니다. 다른 감지 기록은 구역 이름으로 물어보세요.

### ✅ 행동 가이드

{guide("HIGH" if e["alertLevel"] == "DANGER" else "MEDIUM")}"""
    where = short(zone) if zone else "사고다발지역 밖"
    with_date = lambda t: f"{t[5:10]} {hhmm(t)}"
    data = f"- 가장 최근 감지: {where} · {signal(e, with_date)}"
    ref = f"가장 최근 감지는 {at} {where}에서 있었습니다. 거리 {d}m로 {REACHED[dist_tag(d)]}. 경고 단계는 {e['alertLevel']}입니다."
    return Reply("recent", body, note, data, ref)


def event_reply(d, r, c, zone, note):
    level = "HIGH" if d <= 3 else "MEDIUM"
    rows = (f"| 위치 | {zone['name']} |\n" if zone else "") + f"| 추정 거리 | {d}m |\n| 신호 세기 (RSSI) | {r}dBm |\n| 연속 감지 | {c}회 |" + (f"\n| 구역 누적 사고 | {zone['accidentCount']}건 |" if zone else "")
    body = f"""## 🚨 차량 접근 분석

> 💡 추정 거리 **{d}m**, 신호 세기 **{r}dBm**, **{c}회** 연속 감지입니다.{f" {zone['name']} 구역 위험도는 **{zone['riskScore']}점, {zone['riskLevel']}**입니다." if zone else ""}

### 📊 감지 신호

| 항목 | 값 |
| --- | --- |
{rows}

### ✅ 행동 가이드

{guide(level)}"""
    dt, st, ct = dist_tag(d), signal_tag(r), count_tag(c)
    data = f"- 신호: 거리 {d}m({dt}) · RSSI {r}dBm({st}) · 연속 {c}회({ct})" + (f"\n- 구역: {zone_line(zone)}{advice(zone['riskLevel'])}" if zone else "")
    ref = (f"{d}m면 {MEANS[dt]}이고, 신호도 {IS[st]}. 연속 {c}회 감지는 {COUNT_MEANS[ct]}입니다."
           + (f" 이 구역은 누적 사고 {zone['accidentCount']}건이 난 곳이니 {ADVICE[zone['riskLevel']]}." if zone else ""))
    return Reply("event", body, note, data, ref)


def no_data_reply(place, note):
    body = f"""## 📍 {place} 데이터 없음

> 💡 {place}은(는) **사고다발지역 데이터에서 찾지 못했습니다**. 사고가 없다는 뜻은 아닙니다.

### ✅ 대신 이렇게 해 보세요

1. "모란시장 위험해?"처럼 지점이나 동네 이름으로 물어보세요.
2. "동시 다발 구역 어디야?"로 차량이 몰린 곳을 확인하세요.
3. 데이터가 없는 곳에서도 진동 경고가 오면 바로 멈추고 주변을 확인하세요."""
    return Reply("nodata", body, note)


HELP = """## 🤖 이렇게 물어보세요

> 💡 감지 기록과 사고다발지역 데이터로 답할 수 있는 질문입니다.

### ✅ 예시

1. "모란시장 위험해?"처럼 지점·동네 이름으로
2. "내 주변 위험해?" (지도 탭에서 내 위치를 켜 두세요)
3. "최근 감지 알려줘", "동시 다발 구역 어디야?", "언제 제일 위험해?"
4. "진동이 울리면 어떻게 해야 해?\""""
NO_HERE = """## 📍 내 위치를 알 수 없어요

> 💡 현재 위치를 받지 못해 **내 주변** 위험을 계산할 수 없습니다.

### ✅ 이렇게 해 보세요

1. 지도 탭에서 '내 위치'를 켜고 다시 물어보세요.
2. "모란시장 위험해?"처럼 장소 이름으로 물어볼 수도 있습니다."""

# 고정 안내문: 숫자가 데이터에 따라 바뀌지 않아 AI 없이 바로 답함. (질문 패턴, 본문). 순서대로 먼저 걸리는 것
FAQ = [
    (r"동시 다발.*(기준|조건|정의|뜻)", f"""## 🗺️ 동시 다발 구역 기준

> 💡 반경 **{RADIUS_M}m** 안에서 **{WINDOW_MIN}분** 이내에 차량 접근이 **{MIN_COUNT}건 이상** 감지된 곳입니다.

### 📊 판단 조건

| 조건 | 값 |
| --- | --- |
| 거리 | {RADIUS_M}m 이내 |
| 시간 | {WINDOW_MIN}분 이내 |
| 건수 | {MIN_COUNT}건 이상 |

### ⚠️ 왜 따로 보나요?

한 번의 감지보다 짧은 시간에 여러 번 감지되는 곳이 더 위험합니다. 차량이 몰리거나 전방 주시가 어려운 구간이라 진동 경고가 연달아 울립니다.

### ✅ 행동 가이드

1. 지도 탭에서 동시 다발 구역을 미리 확인하세요.
2. 해당 구역에서는 이어폰 볼륨을 낮추세요.
3. 앱 알림 탭에서 '동시 다발 알림'을 켜 두세요.""", None),
    (r"경고 단계", """## 📳 차량 접근 경고 단계

> 💡 접근 이벤트의 경고 단계는 **SAFE, CAUTION, DANGER, RELEASED** 네 가지입니다. 대기, 경고, 심화, 해제 순서로 바뀌어요.

### 📊 단계표

| 단계 | 값 | 의미 |
| --- | --- | --- |
| 대기 | SAFE | 위험 범위 안에 차량 없음 |
| 경고 | CAUTION | 설정된 임계 거리 안으로 차량 접근 |
| 심화 | DANGER | 차량이 매우 가까운 상태 |
| 해제 | RELEASED | 차량이 위험 범위를 벗어남 |

### ⚠️ 위험구역 단계와 다릅니다

경고 단계는 지금 이 순간의 차량 접근 상황이고, HIGH·MEDIUM·LOW는 그 장소가 얼마나 위험한지를 나타내는 구역 단계입니다.

### ✅ 단계별 행동

1. **DANGER**: 걸음을 멈추고 인도 안쪽으로 이동하세요.
2. **CAUTION**: 이어폰 볼륨을 낮추고 좌우를 확인하세요.
3. **RELEASED**: 주변을 확인한 뒤 이동하세요.""", "*경고 단계: 보행자 디바이스가 판단, 진동은 서버 없이 작동*"),
    (r"위험 단계|위험도 기준|단계 기준", """## 🚦 위험구역 단계 기준

> 💡 위험구역의 위험도가 **70점 이상이면 HIGH, 40점 이상이면 MEDIUM, 그 아래는 LOW** 입니다.

### 📊 단계표

| 단계 | 점수 | 의미 |
| --- | --- | --- |
| HIGH | 70~100점 | 사고 및 보행 위험도가 높은 구역 |
| MEDIUM | 40~69점 | 주의가 필요한 구역 |
| LOW | 0~39점 | 일반 구역 |

### ⚠️ 점수를 올리는 요인

- 구역에서 감지된 **최소 거리**가 가까울수록
- 구역의 **최대 RSSI** 가 강할수록
- **연속 감지**와 **DANGER 감지**가 많을수록
- **누적 사고**가 많은 구역일수록

### ✅ 단계별 행동

1. **HIGH**: 걸음을 멈추고 인도 안쪽으로 이동하세요.
2. **MEDIUM**: 이어폰 볼륨을 낮추고 좌우를 확인하세요.
3. **LOW**: 평소처럼 이동하되 진입로에서는 속도를 줄이세요.""", "*판단 기준: safety-ai 위험도 분석 모델*"),
    (r"(RSSI|신호 세기).*(뭐|무엇|뜻|의미)", """## 📡 RSSI (신호 세기)

> 💡 RSSI 는 차량 송신기 신호가 **얼마나 세게 잡히는지**를 dBm 단위로 나타낸 값입니다. 0에 가까울수록 차량이 가깝습니다.

### 📊 해석 기준

| RSSI | 의미 |
| --- | --- |
| -60dBm 이상 | 강함 · 차량이 매우 가까움 |
| -60 ~ -75dBm | 중간 · 차량이 다가오는 중 |
| -75dBm 미만 | 약함 · 지나가거나 멀어지는 중 |

### ⚠️ 함께 보는 값

- **추정 거리**: RSSI 로 계산한 차량과의 거리
- **연속 감지 횟수**: 같은 차량이 몇 번 연달아 잡혔는지
- **누적 사고**: 해당 구역의 사고 이력

### ✅ 활용

1. 신호가 강하고 연속 감지가 많은 구역일수록 구역 위험도가 높아집니다.
2. 앱의 경고 기록에서 RSSI 와 거리를 함께 확인할 수 있습니다.
3. 신호가 약해도 진동이 반복되면 주변을 확인하세요.""", "*판단 기준: 70점 이상 HIGH · 40점 이상 MEDIUM · 그 외 LOW*"),
    (r"진동.*(어떻게|대처|하면|울리면)", """## 📳 진동 경고 대처법

> 💡 진동은 **저소음 차량이 가까이 왔다는 신호**입니다. 바로 걸음을 멈추고 주변을 확인하세요.

### 📊 진동 패턴

| 패턴 | 의미 |
| --- | --- |
| 짧게 1회 | 임계 거리 내 접근 (CAUTION) |
| 길게 반복 | 매우 가까운 접근 (DANGER) |
| 연속 여러 번 | 동시 다발 구역 진입 |

### ⚠️ 주의할 점

진동이 길게 반복되면 DANGER 단계입니다. 차량이 3m 이내에 있을 수 있으니 휴대폰 화면을 보며 걷는 것을 멈추세요.

### ✅ 행동 가이드

1. 즉시 걸음을 멈추고 앞뒤 차량을 확인하세요.
2. 이어폰을 빼거나 볼륨을 최소로 낮추세요.
3. 인도 안쪽이나 건물 쪽으로 이동해 차량이 지나가길 기다리세요.""", "*경고 단계: CAUTION 임계 거리 내 접근 · DANGER 매우 가까움*"),
    (r"이어폰", """## 🎧 이어폰 착용 보행

> 💡 괜찮지만 **위험 구역에서는 볼륨을 낮추세요.** 웨어러블이 차량 접근을 진동으로 알려 드립니다.

### 📊 상황별 권장

| 상황 | 권장 |
| --- | --- |
| 일반 인도 | 평소 볼륨 |
| 횡단보도·골목 | 볼륨 낮추기 |
| 동시 다발 구역 | 한쪽 이어폰 빼기 |

### ⚠️ 주의할 점

저소음 차량은 소리로 알아채기 어렵습니다. 노이즈 캔슬링을 켜면 주변 소리가 더 줄어드니 위험 구역에서는 주변음 모드를 쓰세요.

### ✅ 행동 가이드

1. 웨어러블을 연결해 두고 진동 경고를 켜 두세요.
2. 진동이 오면 바로 볼륨을 낮추고 주변을 확인하세요.
3. 출퇴근 시간 역 주변에서는 특히 주의하세요.""", "*참고: 보행자 촉각 경고 시스템*"),
    (r"연결|페어링|웨어러블|뱃지", """## ⌚ 웨어러블 연결 방법

> 💡 앱 **홈 탭의 '기기 찾기'** 에서 보행자 디바이스를 검색해 연결하면 됩니다. 페어링 창은 뜨지 않습니다.

### 📊 연결 순서

| 단계 | 내용 |
| --- | --- |
| 1 | 웨어러블(OSORI-BADGE) 전원 켜기 |
| 2 | 홈 탭 → 기기 찾기 → 주변 기기 검색 |
| 3 | 찾은 기기에서 '연결' 선택 |
| 4 | 연결되면 상단에 '연결됨' 표시 |

### ⚠️ 연결이 안 될 때

- 휴대폰 블루투스와 위치 권한이 켜져 있는지 확인하세요.
- 웨어러블과 1m 이내에서 다시 검색하세요.
- 다른 휴대폰에 연결돼 있으면 그쪽 앱을 먼저 끄세요.

### ✅ 연결 후

1. 차량 신호를 받으면 웨어러블과 휴대폰이 함께 진동합니다.
2. 주변 위험구역의 위험도가 경고 기록에 표시됩니다.
3. 알림 탭에서 진동·푸시 설정을 바꿀 수 있습니다.""", "*참고: 보행자 촉각 경고 앱*"),
    (r"저소음 차량(이|은|이란)?\s*(뭐|무엇|뭔)", """## 🔇 저소음 차량이란?

> 💡 전기차·하이브리드처럼 **저속에서 소리가 거의 나지 않는 차량**입니다. 이어폰을 낀 보행자는 접근을 알아채기 어렵습니다.

### 📊 특징

| 항목 | 내용 |
| --- | --- |
| 대표 차종 | 전기차, 하이브리드, 수소차 |
| 위험 구간 | 시속 20km 이하 저속 주행 |
| 취약 보행자 | 이어폰 사용자, 고령자, 시각장애인 |

### ⚠️ 왜 위험한가요?

엔진 소리가 없어 뒤나 옆에서 다가와도 들리지 않습니다. 골목, 주차장 출입구, 횡단보도처럼 차량과 보행자가 가까이 섞이는 곳에서 특히 위험합니다.

### ✅ 우리 시스템의 역할

1. 차량 송신기의 RF 신호를 웨어러블이 감지합니다.
2. 임계 거리 안이면 웨어러블과 휴대폰이 진동으로 경고합니다.
3. 앱이 감지 기록을 서버에 저장하고, AI 가 위험구역의 위험도를 산출합니다.""", "*참고: 보행자 촉각 경고 시스템 구상안*"),
    (r"누구|소개|뭘 할 수|무엇을 할|뭐 할 수", """## 🤖 Safety AI 소개

> 💡 **보행자 촉각 경고** 시스템의 안전 도우미입니다. 저소음 차량 접근 데이터를 바탕으로 보행 위험을 알려드려요.

### 📌 할 수 있는 일

- **구역별 위험도**: "모란시장 위험해?"처럼 물어보면 감지 기록과 위험도를 정리해 드립니다.
- **동시 다발 구역**: 짧은 시간에 차량이 몰린 곳을 알려드립니다.
- **신호 해석**: 거리·RSSI·연속 감지 횟수로 위험 정도를 설명합니다.
- **대처 방법**: 진동 경고가 왔을 때 어떻게 해야 하는지 안내합니다.

### ✅ 이렇게 물어보세요

1. 동시 다발 구역 어디야?
2. 오늘 전체 현황 요약해줘
3. 진동이 울리면 어떻게 해야 해?""", "*데이터 기준: 감지 이벤트*"),
]
NOT_PLACE = r"^(지금|오늘|요즘|여기|거기|이곳|저기|밤|낮|아침|저녁|출근길|퇴근길|길)$"


def route(question: str, zones: list, events: list, here=None) -> Reply:
    """질문 유형을 가려 답을 만듦. 먼저 걸리는 순서: 신호 수치 → 안내문 → 장소 → 내 주변 → 최근 → 동시 다발 → 시간대 → 순위 → 요약 → 데이터 없음 → 도움말."""
    q, note = question, data_note()
    grouped, burst = events_by_zone(zones, events), bursts(zones, events)
    by_name = {z["name"]: z for z in zones}
    kind, picked, word = find_zones(q, zones)
    if m := re.search(r"거리\s*([\d.]+)\s*m.*?RSSI\s*(-?\d+)\s*dBm.*?(\d+)\s*회", q):
        return event_reply(float(m[1]), int(m[2]), int(m[3]), picked[0] if kind == "spot" and picked else None, note)
    for pattern, body, faq_note in FAQ:
        if re.search(pattern, q):
            return Reply("faq", body, faq_note or note)
    if picked:
        if kind == "spot" and len(picked) == 1:
            return zone_reply(picked[0], grouped[picked[0]["name"]], burst, note)
        return region_reply(word, picked, grouped, note)
    if here and re.search(NEAR_WORDS, q):
        near = sorted((z for z in zones if abs(z["latitude"] - here[0]) < .02), key=lambda z: distance_m(*here, z["latitude"], z["longitude"]))
        near = sorted([z for z in near if distance_m(*here, z["latitude"], z["longitude"]) <= NEAR_M][:3], key=lambda z: -z["riskScore"])
        close = [e for e in events if distance_m(*here, e["latitude"], e["longitude"]) <= NEAR_M]
        return near_reply(near, grouped, close, note)
    if re.search(RECENT_WORDS, q):
        return recent_reply(events, grouped, note)
    if re.search(NEAR_WORDS, q):
        return Reply("nohere", NO_HERE, "*Safety AI 안내*")
    if re.search(r"동시 다발|몰리|몰린|몰려", q):
        return burst_reply(burst, by_name, note)
    if re.search(r"언제|시간대|몇 ?시", q):  # "언제 제일 위험해?" 는 순위가 아니라 시간대
        return hours_reply(events, note)
    if re.search(r"제일 위험|가장 위험|위험한 (곳|구역)|순위", q):
        return top_reply(sorted((z for z in zones if grouped[z["name"]]), key=lambda z: -z["riskScore"])[:3], grouped, note)
    if re.search(r"요약|현황|전체|통계", q):
        return summary_reply(zones, events, burst, note)
    if (m := re.match(r"\s*(.+?)\s*(은|는|이|가|에서|쪽은)?\s*(위험해|위험하|상황|어때|괜찮)", q)) and not re.search(NOT_PLACE, m[1]):
        return no_data_reply(m[1], note)
    return Reply("help", HELP, "*Safety AI 안내*")
