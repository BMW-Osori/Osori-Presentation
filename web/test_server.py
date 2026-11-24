"""가짜 채팅 모델·가짜 백엔드로 AI 서비스 검증: python3 web/test_server.py"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
server.ACCIDENTS = {ZONES[0]["name"]: {"types": ["보행노인"], "casualties": 9, "deaths": 1}}


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
assert server.place_words(ZONES[0]["name"])[0] == ["모란시장사거리", "모란시장"], "지점 이름 추출"
assert [z["riskZoneId"] for z in server.match_zones("모란시장 위험해?", zones)] == [1]
assert [z["riskZoneId"] for z in server.match_zones("성남 어디가 위험해?", zones)] == [1, 2], "지역은 위험도 순"
assert server.match_zones("안녕", zones) == []
text = server.facts("모란시장 위험해?", zones, events)
assert "위험도 90.0점(HIGH)" in text and "보행노인, 사상자 9명, 사망 1명" in text and "08:09 거리 5.0m · RSSI -53dBm · 연속 3회 · DANGER" in text and "동시 다발 08:00–08:09 3건" in text, text
assert "전체 감지: 3건, DANGER 2건" in text and "동시 다발 구역 1곳" in text, text
assert "모란시장사거리 부근): 위험도" in server.facts("내 주변 위험해?", zones, events, (37.4301, 127.13)), "내 주변은 가까운 구역을 그대로"
assert "질문에 나온 감지 구역: 없음" in server.facts("동시 다발 구역 어디야?", zones, events, (37.4301, 127.13)), "주변을 묻지 않으면 위치를 섞지 않음"
# 사고다발지역 밖이어도 현재 위치 근처 감지는 프롬프트에 들어감
far = {**event(9, 40, "DANGER"), "latitude": 35.1, "longitude": 129.0}
text = server.facts("방금 감지된 거 위험해?", zones, events + [far], (35.1001, 129.0001))
assert "내 위치 1000m 안 위험구역: 0곳" in text and "내 위치 1000m 안 감지: 1건" in text and "08:40 거리 11.0m · RSSI -59dBm · 연속 3회 · DANGER" in text, text
assert "내 위치" not in server.facts("지금 위험해?", zones, events), "위치를 모르면 위치 줄을 넣지 않음"
assert "내 위치 1000m 안 위험구역: 1곳" in server.facts("지금 위험해?", zones, events, (37.4301, 127.13))
assert "질문에 나온 감지 구역: 없음" in server.facts("안녕", zones, events), "없는 구역을 지어내지 않게 명시"
assert server.here_of("x", 1) is None and server.here_of(None, None) is None and server.here_of(99, 1) is None and server.here_of("37.4", "127.1") == (37.4, 127.1)

assert post_chat({"messages": [{"role": "user", "content": "내 주변 위험해?"}], "latitude": 37.43, "longitude": 127.13}) == "안녕"
system = prompts[-1]["messages"][0]["content"]
assert "모란시장사거리 부근): 위험도 90.0점" in system, "채팅에 백엔드 실시간 데이터와 현재 위치가 반영됨"
before = [{"role": "user", "content": "역삼역 위험해?"}, {"role": "assistant", "content": "## 📍 서울특별시 강남구 역삼동(역삼역 부근) 위험도 리포트"}]
assert "역삼역" not in server.context_for(before + [{"role": "user", "content": "최근 마지막 데이터 알려줘"}]), "가리키지 않으면 앞 구역을 섞지 않음"
assert "역삼역 부근): 위험도 50.0점" in server.context_for(before + [{"role": "user", "content": "거기 사고 유형은?"}]), "거기는 앞 구역을 이어받음"
post_chat({"messages": before + [{"role": "user", "content": "최근 마지막 데이터 알려줘"}]})
assert [m["role"] for m in prompts[-1]["messages"]] == ["system", "user"] and prompts[-1]["messages"][1]["content"] == "최근 마지막 데이터 알려줘", "모델에는 이전 답 없이 마지막 질문만"
EVENTS.append(event(4, 30))
post_chat({"messages": [{"role": "user", "content": "모란시장"}]})
assert "감지 4건" in prompts[-1]["messages"][0]["content"], "방금 들어온 이벤트가 바로 채팅에 반영"

assert get("/")[0] == 200 and "보행자" in get("/")[1].decode()
assert get("/labeling/")[0] == 404 and get("/api/console/docs")[0] == 404 and get("/dataset.jsonl")[0] == 404, "라벨링 제거"
assert json.loads(get("/api/bursts")[1])["bursts"][0]["count"] >= 3
traced = post_chat({"messages": [{"role": "user", "content": "모란시장"}], "trace": True})
first, _, answer = traced.partition("\n")
trace = json.loads(first)
assert answer == "안녕" and "참고 데이터" in trace["facts"] and trace["calls"][0]["path"].startswith("/api/v1/risk-zones") and trace["calls"][0]["note"] == "위험구역 3곳" and trace["calls"][1]["note"] == "접근 이벤트 4건", traced[:200]
assert post_chat({"messages": [{"role": "user", "content": "모란시장"}]}) == "안녕", "앱 요청에는 통신 기록을 섞지 않음"
assert json.loads(get("/api/status")[1])["backend"]["ok"] is True
print("ok")
