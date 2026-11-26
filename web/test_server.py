"""가짜 채팅 모델·가짜 백엔드로 AI 서비스 검증: python3 web/test_server.py"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import answers
import server

prompts, detail_calls = [], []
ZONES = [
    {"riskZoneId": 1, "name": "경기도 성남시 중원구 성남동(모란시장사거리 부근)", "latitude": 37.43, "longitude": 127.13, "radius": 100, "riskScore": 90.0, "riskLevel": "HIGH", "accidentCount": 12},
    {"riskZoneId": 2, "name": "경기도 성남시 수정구 신흥동(숯골사거리 부근)", "latitude": 37.444, "longitude": 127.143, "radius": 100, "riskScore": 30.0, "riskLevel": "LOW", "accidentCount": 5},
    {"riskZoneId": 3, "name": "서울특별시 강남구 역삼동(역삼역 부근)", "latitude": 37.5, "longitude": 127.03, "radius": 200, "riskScore": 50.0, "riskLevel": "MEDIUM", "accidentCount": 7},
]


def event(i, minute, level="DANGER"):
    return {"eventId": i, "deviceId": 1, "latitude": 37.43, "longitude": 127.13, "maxRssi": -50 - i, "minEstimatedDistance": 2.0 + i, "alertLevel": level,
            "consecutiveCount": 3, "vibrationTriggered": True, "startedAt": f"2026-11-07T08:{minute:02d}:00", "endedAt": f"2026-11-07T08:{minute:02d}:09"}


EVENTS = [event(1, 0), event(2, 5, "CAUTION"), event(3, 9)]


class FakeBackend(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/v1/risk-zones":
            data = {"riskZones": ZONES}
        elif path == "/api/v1/proximity-events":
            keep = ("eventId", "latitude", "longitude", "alertLevel", "minEstimatedDistance", "vibrationTriggered", "startedAt", "endedAt")
            data = {"content": [{k: e[k] for k in keep} for e in reversed(EVENTS)], "page": 0, "size": 200, "totalElements": len(EVENTS)}
        else:
            detail_calls.append(path)
            data = next(e for e in EVENTS if path.endswith(f"/{e['eventId']}"))
        body = json.dumps({"success": True, "code": "OK", "message": "", "data": data}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


class FakeChat(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        prompts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"\\uc548\\ub155"}}]}\n\ndata: [DONE]\n')


def serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_port}"


server.BACKEND_URL, server.CHAT_URL, base = serve(FakeBackend), serve(FakeChat), serve(server.Handler)
server.TYPE_PAUSE = 0  # 타이핑 효과 없이 바로 (아래에서 따로 확인)
answers.ACCIDENTS = {ZONES[0]["name"]: {"types": ["보행노인"], "casualties": 9, "deaths": 1}}


def get(path):
    try:
        with urllib.request.urlopen(base + path) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post_chat(payload):
    req = urllib.request.Request(base + "/api/chat", json.dumps(payload).encode())
    with urllib.request.urlopen(req) as r:
        return r.read().decode()


zones, events = server.load_data()
assert [e["eventId"] for e in events] == [1, 2, 3] and events[0]["maxRssi"] == -51, "목록에 없는 RSSI 는 상세에서, 오래된 순"
calls = len(detail_calls)
server.load_data()
assert len(detail_calls) == calls, "이벤트 상세는 한 번만 읽음"
A = answers
assert A.place_words(ZONES[0]["name"])[0] == ["모란시장사거리", "모란시장"], "지점 이름 추출"
assert [z["riskZoneId"] for z in A.match_zones("모란시장 위험해?", zones)] == [1]
assert [z["riskZoneId"] for z in A.match_zones("성남 어디가 위험해?", zones)] == [1, 2], "지역은 위험도 순"
assert A.match_zones("안녕", zones) == []
two = [{**ZONES[2], "riskScore": 10.0}, {**ZONES[2], "riskZoneId": 9, "name": "부산광역시 남구 대연동(못골사거리 부근)", "riskScore": 99.0}]
assert [z["riskZoneId"] for z in A.match_zones("강남구 어디가 위험해?", two)] == [3], "가장 길게 걸린 지역만 ('남구' 는 빼고)"

r = A.route("모란시장 위험해?", zones, events)
assert r.kind == "zone" and "**3건** 감지됐고" in r.body and "| 사상자 | 9명 |" in r.body and "**08:09** · 거리 5.0m" in r.body and r.note.startswith("*데이터 기준:"), r.body
assert r.data == "- 구역: 모란시장사거리 · 위험도 90.0점(HIGH) · 누적 사고 12건(보행노인, 사상자 9명, 사망 1명)\n- 감지 3건 · 가장 가까운 감지 08:00 · 거리 3.0m(가까이 접근) · RSSI -51dBm(강함) · 연속 3회(이어짐) · DANGER\n- 동시 다발: 08:00–08:09 · 3건\n- 권장 행동: 걸음을 멈추고 앞뒤 차량부터 확인하세요", r.data
assert r.reference == "모란시장사거리에서는 08:00 감지가 3.0m로 차량이 가까이 다가왔고, 신호도 강했습니다. 08:00–08:09 사이 3건이 몰린 동시 다발 구역이기도 합니다. 누적 사고 12건이 난 곳이니 걸음을 멈추고 앞뒤 차량부터 확인하세요.", r.reference
r = A.route("내 주변 위험해?", zones, events, (37.4301, 127.13))
assert r.kind == "near" and r.data.startswith("- 내 위치 1000m 안 위험구역 1곳 · 가장 위험한 곳 모란시장사거리 90.0점(HIGH)\n- 내 위치 1000m 안 감지 3건 · 가장 가까운 감지 08:00 · 거리 3.0m"), r.data
assert A.route("동시 다발 구역 어디야?", zones, events, (37.4301, 127.13)).kind == "burst", "주변을 묻지 않으면 위치를 섞지 않음"
far = {**event(9, 40, "DANGER"), "latitude": 35.1, "longitude": 129.0}  # 사고다발지역 밖이어도 현재 위치 근처 감지는 알림
r = A.route("방금 감지된 거 위험해?", zones, events + [far], (35.1001, 129.0001))
assert r.data == "- 내 위치 1000m 안 위험구역 0곳\n- 내 위치 1000m 안 감지 1건 · 가장 가까운 감지 08:40 · 거리 11.0m(아직 여유) · RSSI -59dBm(강함) · 연속 3회(이어짐) · DANGER", r.data
assert A.route("지금 위험해?", zones, events).kind == "nohere", "위치를 모르면 주변을 계산하지 않음"
assert "위험구역 1곳" in A.route("지금 위험해?", zones, events, (37.4301, 127.13)).data
r = A.route("최근 마지막 데이터 알려줘", zones, events + [far])
assert r.kind == "recent" and r.data == "- 가장 최근 감지: 사고다발지역 밖 · 11-07 08:40 · 거리 11.0m(아직 여유) · RSSI -59dBm(강함) · 연속 3회(이어짐) · DANGER", r.data
assert "| 위치 | 경기도 성남시 중원구 성남동(모란시장사거리 부근) |" in A.route("마지막 감지 기록", zones, events).body
assert A.route("방금 감지된 거 위험해?", zones, events).kind == "recent", "위치를 모르면 '방금' 은 가장 최근 감지"
assert A.route("모란시장 최근 기록 알려줘", zones, events).kind == "zone", "구역을 물으면 그 구역 기록으로"
assert A.route("내 주변 최근 감지 있어?", zones, events, (37.4301, 127.13)).kind == "near", "주변을 물으면 내 위치 기록으로"
r = A.route("최근 데이터 알려줘", zones, [])
assert r.data is None and "**0건**" in r.body
assert A.route("언제 제일 위험해?", zones, events).kind == "hours", "'언제 제일 위험해' 는 순위가 아니라 시간대"
assert A.route("제일 위험한 구역 어디야?", zones, events).data.startswith("- 감지가 있는 구역 위험도 순: 모란시장사거리 90.0점(HIGH, 감지 3건)")
r = A.route("모란시장에서 거리 2.3m, RSSI -52dBm으로 4회 연속 감지됐어. 얼마나 위험해?", zones, events)
assert r.kind == "event" and r.data.startswith("- 신호: 거리 2.3m(차량이 바로 옆) · RSSI -52dBm(강함) · 연속 4회(이어짐)\n- 구역: 모란시장사거리"), r.data
for q, kind in (("진동이 울리면 어떻게 해야 해?", "faq"), ("동시 다발 구역 기준이 뭐야?", "faq"), ("에버랜드 위험해?", "nodata"), ("안녕", "help")):
    r = A.route(q, zones, events)
    assert r.kind == kind and r.data is None, (q, r.kind)
server.TYPE_PAUSE, server.TYPE_MAX_S = .001, .1
text = "가나다라마" * 300  # 1500자: 5자씩이면 0.3초라 0.1초 안에 끝나게 15자씩
parts = list(server.typed(text))
assert "".join(parts) == text and len(parts) == 100 and len(list(server.typed("짧은 글"))) == 1, "조금씩 보내되 긴 글은 정한 시간 안에 끝나게 묶음"
server.TYPE_PAUSE, server.TYPE_MAX_S = 0, 4
assert server.here_of("x", 1) is None and server.here_of(None, None) is None and server.here_of(99, 1) is None and server.here_of("37.4", "127.1") == (37.4, 127.1)

# 채팅: 본문 → AI 분석(모델) → 출처
answer = post_chat({"messages": [{"role": "user", "content": "내 주변 위험해?"}], "latitude": 37.43, "longitude": 127.13})
assert answer.startswith("## 📍 내 주변 위험구역") and "\n\n### 🤖 AI 분석\n\n안녕\n\n---\n*데이터 기준:" in answer, answer
sent = prompts[-1]
assert sent["messages"][0]["content"] == A.ANALYSIS_SYSTEM and sent["messages"][1]["content"].startswith("질문: 내 주변 위험해?\n\n데이터:\n- 내 위치 1000m 안 위험구역 1곳"), sent
assert sent["max_tokens"] == 120 and len(sent["messages"]) == 2, "모델에는 짧은 분석만, 이전 대화 없이"
before = [{"role": "user", "content": "역삼역 위험해?"}, {"role": "assistant", "content": "## 📍 서울특별시 강남구 역삼동(역삼역 부근) 위험도 리포트"}]
assert server.reply_for(before + [{"role": "user", "content": "최근 마지막 데이터 알려줘"}])[1].kind == "recent", "가리키지 않으면 앞 구역을 섞지 않음"
assert "역삼역 부근) 위험도 리포트" in server.reply_for(before + [{"role": "user", "content": "거기 사고 유형은?"}])[1].body, "거기는 앞 구역을 이어받음"
asked = len(prompts)
faq = post_chat({"messages": [{"role": "user", "content": "경고 단계가 뭐야?"}]})
assert len(prompts) == asked and "AI 분석" not in faq and faq.endswith("---\n*경고 단계: 보행자 디바이스가 판단, 진동은 서버 없이 작동*"), "안내문은 모델 없이 바로"
EVENTS.append(event(4, 30))
post_chat({"messages": [{"role": "user", "content": "모란시장"}]})
assert "감지 4건" in prompts[-1]["messages"][1]["content"], "방금 들어온 이벤트가 바로 채팅에 반영"
url, server.CHAT_URL = server.CHAT_URL, "http://127.0.0.1:9"  # 모델 서버가 꺼져 있으면
down = post_chat({"messages": [{"role": "user", "content": "모란시장"}]})
server.CHAT_URL = url
assert "### 🤖 AI 분석\n\n모란시장사거리에서는 08:00 감지가" in down and "*데이터 기준:" in down, "코드가 쓴 기준 분석으로 대신"

assert get("/")[0] == 200 and "보행자" in get("/")[1].decode()
assert get("/labeling/")[0] == 404 and get("/api/console/docs")[0] == 404 and get("/dataset.jsonl")[0] == 404, "라벨링 제거"
assert json.loads(get("/api/bursts")[1])["bursts"][0]["count"] >= 3
traced = post_chat({"messages": [{"role": "user", "content": "모란시장"}], "trace": True})
first, _, answer = traced.partition("\n")
trace = json.loads(first)
assert answer.startswith("## 📍 경기도 성남시 중원구 성남동(모란시장사거리 부근)") and "AI 분석\n\n안녕" in answer and trace["facts"].startswith("- 구역: 모란시장사거리"), traced[:300]
assert trace["calls"][0]["path"].startswith("/api/v1/risk-zones") and trace["calls"][0]["note"] == "위험구역 3곳" and trace["calls"][1]["note"] == "접근 이벤트 4건", trace
assert not post_chat({"messages": [{"role": "user", "content": "모란시장"}]}).startswith("{"), "앱 요청에는 통신 기록을 섞지 않음"
assert json.loads(get("/api/status")[1])["backend"]["ok"] is True
print("ok")
