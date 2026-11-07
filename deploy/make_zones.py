"""공공데이터 '전국교통사고다발지역표준데이터' CSV(한국도로교통공단) → 백엔드 적재 파일 + 시연 데이터.
python3 deploy/make_zones.py <csv>  →  deploy/risk-zones.json, web/accident_data.json, web/sample_data.json
폴리곤은 중심에서 반경 100·200m 인 원이므로 중심과 반경만 씀. 위치 이름이 같으면 한 구역으로 합침."""
import csv
import hashlib
import json
import math
import random
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECENT = "2022"  # 위험구역으로 올리는 사고연도 (최근 3년)
EPDO = {"사망자수": 12, "중상자수": 6, "경상자수": 3, "부상신고자수": 1}  # 사망 사고 환산 가중치
MODEL_VERSION = "koroad-epdo-v1"


def radius_of(polygon: str) -> int:
    n = [float(x) for x in re.findall(r"-?\d+\.?\d*", polygon)]
    pts = list(zip(n[0::2], n[1::2]))
    cx, cy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return round(statistics.mean(math.hypot(x - cx, y - cy) for x, y in pts))


def level_of(score: float) -> str:
    return "HIGH" if score >= 70 else "MEDIUM" if score >= 40 else "LOW"


def stats(rows: list) -> dict:
    def total(group):
        return {"areas": len(group), "accidents": sum(int(r["사고건수"]) for r in group), "casualties": sum(int(r["사상자수"]) for r in group),
                "deaths": sum(int(r["사망자수"]) for r in group)}

    def by(key):
        groups = defaultdict(list)
        for r in rows:
            groups[key(r)].append(r)
        return {k: total(v) for k, v in sorted(groups.items())}

    return {"total": total(rows), "years": by(lambda r: r["사고연도"]), "types": by(lambda r: r["사고유형구분"]),
            "regions": by(lambda r: re.sub(r"\d+$", "", r["사고다발지역시도시군구"].split()[0])),
            "referenceDate": rows[0]["데이터기준일자"], "provider": rows[0]["제공기관명"]}


def zones_of(rows: list) -> list:
    merged = defaultdict(list)
    for r in rows:
        if r["사고연도"] >= RECENT:
            merged[r["사고지역위치명"]].append(r)
    epdo_of = lambda group: sum(w * int(r[k]) for r in group for k, w in EPDO.items())
    full = sorted(map(epdo_of, merged.values()))[int(len(merged) * .99)]  # 상위 1% 환산값이면 100점
    zones = []
    for name, group in merged.items():
        score = min(100.0, round(epdo_of(group) / full * 100, 1))
        zones.append({
            "sourceKey": "koroad-" + hashlib.sha1(name.encode()).hexdigest()[:16], "name": name,
            "latitude": float(group[0]["위도"]), "longitude": float(group[0]["경도"]),
            "radius": max(radius_of(r["사고다발지역폴리곤정보"]) for r in group), "riskScore": score, "riskLevel": level_of(score),
            "accidentCount": sum(int(r["사고건수"]) for r in group), "modelVersion": MODEL_VERSION,
            "detail": {"types": sorted({r["사고유형구분"] for r in group}), "years": sorted({r["사고연도"] for r in group}),
                       "casualties": sum(int(r["사상자수"]) for r in group), "deaths": sum(int(r["사망자수"]) for r in group),
                       "severe": sum(int(r["중상자수"]) for r in group), "minor": sum(int(r["경상자수"]) for r in group),
                       "reported": sum(int(r["부상신고자수"]) for r in group)},
        })
    return sorted(zones, key=lambda z: -z["riskScore"])


def sample_events(zones: list) -> list:
    """시연용 가상 접근 이벤트: 성남시 상위 구역 주변. 시각은 예시이며 적재 때 현재 기준으로 옮김."""
    rng = random.Random(7)
    start, events = datetime(2026, 11, 6, 7, 30), []
    for rank, zone in enumerate(zones[:8]):
        burst = rank < 2  # 상위 두 곳은 동시 다발 구역이 되게 몰아서
        at = start + timedelta(hours=rank * 1.7)
        for n in range(4 if burst else rng.randint(1, 3)):
            at += timedelta(minutes=rng.randint(2, 4) if burst else rng.randint(20, 80))
            distance = round(rng.uniform(1.5, 9.5), 1)
            events.append({"eventId": 0, "deviceId": 1, "latitude": round(zone["latitude"] + rng.uniform(-.0004, .0004), 6),
                           "longitude": round(zone["longitude"] + rng.uniform(-.0004, .0004), 6),
                           "maxRssi": round(-45 - 24 * math.log10(distance)), "minEstimatedDistance": distance,
                           "alertLevel": "DANGER" if distance <= 3 else "CAUTION", "consecutiveCount": rng.randint(2, 6),
                           "vibrationTriggered": True, "startedAt": at.isoformat(), "endedAt": (at + timedelta(seconds=rng.randint(4, 12))).isoformat()})
    events.sort(key=lambda e: e["startedAt"])
    for i, e in enumerate(events, 1):
        e["eventId"] = i
    return events


if __name__ == "__main__":
    rows = list(csv.DictReader(open(sys.argv[1], encoding="cp949")))
    zones = zones_of(rows)
    (ROOT / "deploy" / "risk-zones.json").write_text(json.dumps([{k: v for k, v in z.items() if k != "detail"} for z in zones], ensure_ascii=False))
    (ROOT / "web" / "accident_data.json").write_text(json.dumps({"stats": stats(rows), "zones": {z["name"]: z["detail"] for z in zones}}, ensure_ascii=False))
    local = [z for z in zones if "성남시" in z["name"]]
    sample = [{"riskZoneId": i, **{k: v for k, v in z.items() if k not in ("detail", "sourceKey")}} for i, z in enumerate(local, 1)]
    (ROOT / "web" / "sample_data.json").write_text(json.dumps({"riskZones": sample, "events": sample_events(sample)}, ensure_ascii=False, indent=1))
    print(f"행 {len(rows)} → 구역 {len(zones)}, 성남시 {len(local)}, 등급", {l: sum(z["riskLevel"] == l for z in zones) for l in ("HIGH", "MEDIUM", "LOW")})
    assert all(len(z["name"]) <= 100 and z["radius"] > 0 and -90 <= z["latitude"] <= 90 for z in zones)
