"""빈 백엔드에 시연 데이터를 공개 API 로 넣음: python3 seed_backend.py <sample_data.json> [백엔드 주소]
위험구역은 risk-zones.json 을 백엔드 적재 기능으로 먼저 넣고, 여기서는 시연 디바이스와 가상 접근 이벤트만 넣음."""
import json
import sys
import urllib.request
from datetime import datetime, timedelta

BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080"


def call(path, body=None):
    req = urllib.request.Request(BASE + path, None if body is None else json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)["data"]


if call("/api/v1/proximity-events?size=1")["totalElements"]:
    sys.exit("이미 이벤트가 있어 넣지 않았습니다")
device = call("/api/v1/devices", {"deviceUuid": "OSORI-DEMO-001", "name": "시연 디바이스", "deviceType": "WEARABLE"})["deviceId"]
events = json.load(open(sys.argv[1]))["events"]
shift = datetime.now() - timedelta(minutes=3) - datetime.fromisoformat(events[-1]["startedAt"])  # 가장 최근 이벤트가 3분 전이 되게
for e in events:
    started, ended = (datetime.fromisoformat(e[k]) + shift for k in ("startedAt", "endedAt"))
    body = {k: e[k] for k in ("latitude", "longitude", "maxRssi", "minEstimatedDistance", "alertLevel", "consecutiveCount", "vibrationTriggered")}
    created = call("/api/v1/proximity-events", {"deviceId": device, **body, "startedAt": started.isoformat(timespec="seconds"), "endedAt": ended.isoformat(timespec="seconds")})
    call(f"/api/v1/proximity-events/{created['eventId']}/samples", {"samples": [
        {"rssi": e["maxRssi"] - 12 + 6 * i, "estimatedDistance": round(e["minEstimatedDistance"] * (1.6 - .3 * i), 1), "measuredAt": (started + timedelta(seconds=i)).isoformat(timespec="seconds")}
        for i in range(3)]})
print(f"디바이스 {device}, 이벤트 {len(events)}건을 넣었습니다")
