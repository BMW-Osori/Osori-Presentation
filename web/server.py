"""AI 서비스: 시연 사이트, 동시 다발 구역, 채팅(Ollama). 위험구역·이벤트는 요청마다 백엔드(Osori)에서 실시간으로 읽음."""
import contextlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

BACKEND_URL = os.environ.get("BACKEND_URL")  # 운영: Spring Boot 내부 주소. 없으면 예시 데이터(sample_data.json)
CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8080")  # 로컬: mlx_lm.server + LoRA (ai/train.sh), EC2: Ollama
CHAT_MODEL = os.environ.get("CHAT_MODEL", "default_model")  # mlx_lm.server 는 기본 모델, EC2 는 safety-chat
# mlx-lm 0.31 서버는 --adapter-path 를 기본 모델에 적용하지 않아(버그) 요청마다 어댑터 경로를 넘김
CHAT_ADAPTERS = os.environ.get("CHAT_ADAPTERS", str(Path(__file__).resolve().parent.parent / "ai" / "adapters"))
CHAT_SYSTEM = (
    "당신은 '보행자 촉각 경고' 시스템의 안전 도우미 Safety AI입니다. "
    "저소음 차량 접근 감지 데이터를 바탕으로 한국어로 답합니다.\n"
    "답변은 노션(Notion) 문서처럼 마크다운으로 길고 자세하게 씁니다.\n"
    "- 첫 줄은 `## 이모지 제목`, 다음은 `> 💡 핵심 요약` 콜아웃\n"
    "- `### 📊`, `### ⚠️`, `### ✅` 처럼 이모지 소제목으로 3~5개 섹션\n"
    "- 수치는 `| 항목 | 값 |` 표나 `- **항목**: 값` 목록, 행동 요령은 번호 목록\n"
    "- 마지막은 `---` 구분선과 `*데이터 기준: …*` 한 줄\n"
    "- 숫자는 참고 데이터에 있는 값만 쓰고 지어내지 않습니다."
)
RADIUS_M, WINDOW_MIN, MIN_COUNT = 300, 15, 3  # 동시 다발 구역: 300m·15분 안에 3건 이상
ROOT = Path(__file__).parent
ACCIDENTS = json.loads((ROOT / "accident_data.json").read_text())["zones"]  # 구역 이름 → 사고 유형·사상자 (백엔드에 없는 공공데이터 항목)
NEAR_M = 1000  # 채팅에서 "내 주변"으로 보는 거리
KOREA = "latitude=36.5&longitude=127.8&radius=1000000"  # 전국 위험구역 조회 (백엔드 조회 API 에 반경이 필수)
SCAN_EVENTS = 200
NEAR_WORDS = r"주변|근처|여기|내 위치|현재 위치|지금 위치|이 근방|방금|^(지금 )?위험해"  # 현재 위치를 묻는 질문
_details = {}  # 이벤트 상세. 목록 API 에 RSSI·연속 횟수가 없어 건별로 읽음. 이벤트는 바뀌지 않으므로 보관


def backend(path: str, calls=None):
    """백엔드 응답({success, code, message, data})에서 data 만 꺼냄. calls 를 주면 통신 기록을 남김 (시연 콘솔)."""
    started = datetime.now()
    with urllib.request.urlopen(BACKEND_URL + path, timeout=10) as res:
        data = json.load(res)["data"]
        if calls is not None:
            calls.append({"method": "GET", "path": path, "status": res.status, "ms": round((datetime.now() - started).total_seconds() * 1000)})
    return data


def load_data(calls=None) -> tuple[list, list]:
    """(위험구역, 접근 이벤트). 이벤트는 오래된 순. 매번 백엔드에서 새로 읽어 방금 들어온 이벤트도 포함."""
    if not BACKEND_URL:
        data = json.loads((ROOT / "sample_data.json").read_text())
        return data["riskZones"], sorted(data["events"], key=lambda e: e["startedAt"])
    zones = backend(f"/api/v1/risk-zones?{KOREA}", calls)["riskZones"]
    if calls is not None:
        calls[-1]["note"] = f"위험구역 {len(zones)}곳"
    # ponytail: 최근 200건만. 이벤트 상세가 건별 조회라 처음 한 번은 건수만큼 호출함
    events = []
    listed = backend(f"/api/v1/proximity-events?size={SCAN_EVENTS}&sort=startedAt,desc", calls)["content"]
    if calls is not None:
        calls[-1]["note"] = f"접근 이벤트 {len(listed)}건"
    for e in listed:
        if e["eventId"] not in _details:
            _details[e["eventId"]] = backend(f"/api/v1/proximity-events/{e['eventId']}", calls)
        events.append(_details[e["eventId"]])
    return zones, sorted(events, key=lambda e: e["startedAt"])


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


def match_zones(question: str, zones: list) -> list:
    """질문에 나온 지점 → 지역 순으로 위험구역을 찾아 위험도 높은 5곳까지."""
    spot, region = [], []
    for z in zones:
        spots, regions = place_words(z["name"])
        if any(w in question for w in spots):
            spot.append(z)
        elif any(w in question for w in regions):
            region.append(z)
    return sorted(spot or region, key=lambda z: -z["riskScore"])[:5]


def zone_line(z: dict, es: list, burst: list, hhmm) -> list:
    """구역 한 곳의 사실 줄들: 공공데이터 사고 내역 + 실시간 감지 기록."""
    b = next((b for b in burst if b["zone"] == z["name"]), None)
    d = ACCIDENTS.get(z["name"])
    harm = f"({'·'.join(d['types'])}, 사상자 {d['casualties']}명, 사망 {d['deaths']}명)" if d else ""
    return [
        f"- {z['name']}: 위험도 {z['riskScore']}점({z['riskLevel']}), 누적 사고 {z['accidentCount']}건{harm}, 감지 {len(es)}건"
        + (f"({', '.join(hhmm(e['startedAt']) for e in es)})" if es else "")
        + (f", 동시 다발 {hhmm(b['start'])}–{hhmm(b['end'])} {b['count']}건" if b else ""),
        *(f"  - {hhmm(e['startedAt'])} 거리 {e['minEstimatedDistance']}m · RSSI {e['maxRssi']}dBm · 연속 {e['consecutiveCount']}회 · {e['alertLevel']}" for e in es),
    ]


def facts(question: str, zones: list, events: list, here=None) -> str:
    """질문에 나온 구역(또는 내 주변)의 실제 수치 + 전체 요약. 모델이 숫자를 지어내지 않도록 매 질문에 붙임.
    요약 줄의 문구는 모델을 튜닝할 때 쓴 형식 그대로: 바꾸면 모델이 값을 잘못 옮김."""
    hhmm = lambda t: t[11:16]
    grouped = events_by_zone(zones, events)
    burst = bursts(zones, events)
    picked = match_zones(question, zones)
    asks_near = here and not picked and re.search(NEAR_WORDS, question)
    if asks_near:
        near = sorted((z for z in zones if abs(z["latitude"] - here[0]) < .02), key=lambda z: distance_m(*here, z["latitude"], z["longitude"]))
        picked = sorted([z for z in near if distance_m(*here, z["latitude"], z["longitude"]) <= NEAR_M][:3], key=lambda z: -z["riskScore"])  # 모델은 첫 줄을 가장 위험한 곳으로 읽음
    lines = [line for z in picked for line in zone_line(z, grouped[z["name"]], burst, hhmm)]
    if asks_near:
        lines.append(f"- 내 위치 {NEAR_M}m 안 위험구역: {len(picked)}곳")
        # 사고다발지역 밖에서 감지돼도 내 위치 기준으로는 알려야 함 (zone_line 에는 구역 안 감지만 나옴)
        close = [e for e in events if distance_m(*here, e["latitude"], e["longitude"]) <= NEAR_M]
        lines.append(f"- 내 위치 {NEAR_M}m 안 감지: {len(close)}건")
        lines += [f"  - {hhmm(e['startedAt'])} 거리 {e['minEstimatedDistance']}m · RSSI {e['maxRssi']}dBm · 연속 {e['consecutiveCount']}회 · {e['alertLevel']}" for e in close[-5:]]
    elif not lines:  # 모델이 없는 구역의 수치를 전체 요약에서 지어내지 않도록 명시
        lines.append("- 질문에 나온 감지 구역: 없음")
    alerts = Counter(e["alertLevel"] for e in events)
    risk = Counter(z["riskLevel"] for z in zones)
    scores = [z["riskScore"] for z in zones] or [0]
    lines.append(f"- 전체 감지: {len(events)}건, DANGER {alerts['DANGER']}건, CAUTION {alerts['CAUTION']}건")
    lines.append(f"- 위험구역: {len(zones)}곳, HIGH {risk['HIGH']}곳, MEDIUM {risk['MEDIUM']}곳, LOW {risk['LOW']}곳, "
                 f"평균 {sum(scores) / len(scores):.1f}점, 최고 {max(scores)}점")
    by_hour = Counter(int(e["startedAt"][11:13]) for e in events if e["alertLevel"] == "DANGER")
    lines.append("- 시간대별 DANGER: " + (", ".join(f"{h}시 {c}건" for h, c in sorted(by_hour.items())) or "없음"))
    risk_of = {z["name"]: z["riskScore"] for z in zones}
    lines.append(f"- 동시 다발 구역 {len(burst)}곳: " + (" / ".join(
        f"{b['zone']} {hhmm(b['start'])}–{hhmm(b['end'])} {b['count']}건" + (f"(구역 위험도 {risk_of[b['zone']]}점)" if b["zone"] in risk_of else "") for b in burst) or "없음"))
    live = sorted((z for z in zones if grouped[z["name"]]), key=lambda z: -z["riskScore"])[:3]  # 전국 1위보다 감지가 있는 구역이 답에 쓸모 있음
    lines.append("- 위험도 상위 구역: " + (" / ".join(f"{z['name']} {z['riskScore']}점(감지 {len(grouped[z['name']])}건, 사고 {z['accidentCount']}건)" for z in live) or "없음"))
    lines.append(f"- 데이터 기준: {datetime.now():%Y-%m-%d %H:%M} 실시간 감지, 한국도로교통공단 사고다발지역")
    return "참고 데이터:\n" + "\n".join(lines)


def context_for(messages: list, here=None, calls=None) -> str:
    """질문마다 백엔드를 새로 읽어 모델에 줄 참고 데이터를 만듦."""
    question = " ".join(m["content"] for m in messages[-3:] if m["role"] == "user")  # 후속 질문도 앞 구역을 이어받게
    zones, events = load_data(calls)
    return facts(question, zones, events, here)


def chat(messages: list, context: str):
    """모델에 연결하고 답변 조각을 받는 대로 내는 제너레이터를 반환. 연결 실패는 호출 시점에 예외."""
    body = {
        "model": CHAT_MODEL,
        "messages": [{"role": "system", "content": f"{CHAT_SYSTEM}\n\n{context}"}, *messages],
        "adapters": CHAT_ADAPTERS,  # Ollama 는 무시 (튜닝 결과가 모델에 합쳐져 있음)
        "max_tokens": 900,  # 노션 형식 답변(최장 약 660토큰)은 담고, CPU 8토큰/초에서 120초 안에 끝나는 상한
        "repetition_penalty": 1.1,  # 같은 구절 반복 루프 방지
        "repetition_context_size": 64,
        "temperature": 0.1,  # 시연에서 수치를 옮길 때 흔들리지 않게
        "stream": True,
    }
    req = urllib.request.Request(
        f"{CHAT_URL}/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    res = urllib.request.urlopen(req, timeout=120)  # 스트리밍에서는 조각 사이 대기 한도

    def pieces():
        with res:
            for line in res:
                if line.startswith(b"data: {"):
                    for choice in json.loads(line[6:])["choices"]:
                        if text := choice.get("delta", {}).get("content"):
                            yield text

    return pieces()


def here_of(latitude, longitude):
    """요청의 현재 위치. 없거나 잘못되면 None."""
    try:
        here = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    return here if -90 <= here[0] <= 90 and -180 <= here[1] <= 180 else None


def status() -> dict:
    """시연 화면의 연결 표시: 백엔드와 채팅 모델이 실제로 응답하는지."""
    result = {}
    for name, url in (("backend", f"{BACKEND_URL}/api/v1/proximity-events?size=1" if BACKEND_URL else None), ("llm", f"{CHAT_URL}/v1/models")):
        try:
            started = datetime.now()
            if url:
                urllib.request.urlopen(url, timeout=3).close()
            result[name] = {"ok": bool(url), "ms": round((datetime.now() - started).total_seconds() * 1000)}
        except (urllib.error.URLError, TimeoutError):
            result[name] = {"ok": False, "ms": None}
    return result


class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, ctype="application/json; charset=utf-8", headers=()):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for h in headers:
            self.send_header(*h)
        self.end_headers()
        self.wfile.write(body)

    def start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def send_stream(self, pieces):
        """text/plain 으로 받는 대로 전송. 길이를 모르므로 연결 종료가 응답의 끝 (HTTP/1.0)."""
        with contextlib.closing(pieces):  # 사용자가 끊으면 모델 생성도 중단
            try:
                for text in pieces:
                    self.wfile.write(text.encode())
            except (BrokenPipeError, ConnectionResetError):  # 사용자가 끊음: 보낼 곳 없음
                pass
            except Exception:  # 모델 쪽 실패(시간 초과, 깨진 응답): 헤더는 이미 나갔으므로 본문으로 알림
                self.wfile.write("\n\n*(답변이 중간에 끊겼습니다. 다시 시도해 주세요.)*".encode())

    def proxy_backend(self):
        """로컬 개발용: Caddy 없이 한 주소로 Spring Boot 까지. 운영에서는 Caddy 가 /api/v1 을 바로 넘겨 여기 오지 않음."""
        if not BACKEND_URL:
            return self.send(502, {"error": "BACKEND_URL 이 없어 Spring Boot 로 넘길 수 없습니다"})
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))) if self.command == "POST" else None
        req = urllib.request.Request(BACKEND_URL + self.path, body, {"Content-Type": "application/json"}, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                self.send(res.status, res.read())
        except urllib.error.HTTPError as e:
            self.send(e.code, e.read())

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path.startswith("/api/v1/"):
            return self.proxy_backend()
        if url.path == "/":
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/accident_data.json":
            return self.send(200, (ROOT / "accident_data.json").read_bytes())
        try:
            if url.path == "/api/bursts":
                zones, events = load_data()
                params = {"radius_m": RADIUS_M, "window_min": WINDOW_MIN, "min_count": MIN_COUNT}
                return self.send(200, {"bursts": bursts(zones, events), "params": params})
            if url.path == "/api/status":
                return self.send(200, status())
        except (urllib.error.URLError, TimeoutError):
            return self.send(502, {"error": "백엔드 데이터를 읽지 못했습니다. 잠시 후 다시 시도하세요."})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path.startswith("/api/v1/"):
            return self.proxy_backend()
        if url.path != "/api/chat":
            return self.send(404, {"error": "not found"})
        if "Content-Length" not in self.headers:  # chunked 전송 미지원
            return self.send(411, {"error": "Content-Length 헤더가 필요합니다"})
        try:
            length = int(self.headers["Content-Length"])
            if length > 20_000:
                raise ValueError("too large")
            data = json.loads(self.rfile.read(length))
            # 최근 10턴만, user/assistant 문자열만 통과
            arg = [{"role": m["role"], "content": m["content"]} for m in data["messages"][-10:]]
            if any(m["role"] not in ("user", "assistant") or not isinstance(m["content"], str) for m in arg):
                raise ValueError("bad message")
            if not arg:
                raise ValueError("empty")
        except (ValueError, TypeError, KeyError):
            return self.send(400, {"error": '요청 형식이 올바르지 않습니다. {"messages": [{"role": "user", "content": "..."}]}'})
        calls = []
        try:
            context = context_for(arg, here_of(data.get("latitude"), data.get("longitude")), calls)
        except (urllib.error.URLError, TimeoutError):
            return self.send(502, {"error": "백엔드 데이터를 읽지 못했습니다. 잠시 후 다시 시도하세요."})
        if data.get("trace") is True:  # 시연 콘솔: 첫 줄에 통신 기록을 먼저 보내고, 모델 답변은 그 뒤로 이어짐
            self.start_stream()
            self.wfile.write(json.dumps({"calls": calls, "facts": context, "model": CHAT_MODEL}, ensure_ascii=False).encode() + b"\n")
            try:
                return self.send_stream(chat(arg, context))
            except Exception as e:  # 헤더는 이미 나갔으므로 본문으로 알림
                return self.wfile.write(f"답변을 받지 못했습니다: {e}".encode())
        try:
            pieces = chat(arg, context)
            self.start_stream()
            self.send_stream(pieces)
        except urllib.error.URLError:  # 모델 서버 미기동
            self.send(502, {"error": f"AI 모델 서버({CHAT_URL})가 꺼져 있습니다. 잠시 후 다시 시도하세요."})
        except Exception as e:  # 모델 없음, 잘못된 출력
            self.send(502, {"error": f"AI 응답 처리 실패: {e}"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    print(f"http://localhost:{port}  (채팅: {CHAT_URL}, 백엔드: {BACKEND_URL or '예시 데이터'})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
