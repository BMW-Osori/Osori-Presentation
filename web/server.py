"""AI 서비스: 시연 사이트, 동시 다발 구역, 채팅(Ollama). 위험구역·이벤트는 요청마다 백엔드(Osori)에서 실시간으로 읽음.
채팅 답은 answers.py 가 본문을 바로 쓰고, 끝의 AI 분석 1~2문장만 모델이 받는 대로 이어 붙임."""
import contextlib
import json
import os
import queue
import random
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from answers import MIN_COUNT, RADIUS_M, WINDOW_MIN, analysis_messages, bursts, route

BACKEND_URL = os.environ.get("BACKEND_URL")  # 운영: Spring Boot 내부 주소. 없으면 예시 데이터(sample_data.json)
CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8080")  # 로컬: mlx_lm.server + LoRA (ai/train.sh), EC2: Ollama
CHAT_MODEL = os.environ.get("CHAT_MODEL", "default_model")  # mlx_lm.server 는 기본 모델, EC2 는 safety-chat
# mlx-lm 0.31 서버는 --adapter-path 를 기본 모델에 적용하지 않아(버그) 요청마다 어댑터 경로를 넘김
CHAT_ADAPTERS = os.environ.get("CHAT_ADAPTERS", str(Path(__file__).resolve().parent.parent / "ai" / "adapters"))
ROOT = Path(__file__).parent
KOREA = "latitude=36.5&longitude=127.8&radius=1000000"  # 전국 위험구역 조회 (백엔드 조회 API 에 반경이 필수)
SCAN_EVENTS = 200
FOLLOW_WORDS = r"거기|그곳|그 구역|그 지역|그 동네|그쪽"  # 앞 질문의 구역을 가리키는 후속 질문
TYPE_CPS, TYPE_MAX_S = 70, 7  # 코드가 쓴 글도 AI 가 쓰듯 초당 약 70자. 긴 답도 7초 안에 끝나게 전체를 빠르게 (0 이면 바로)
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


def reply_for(messages: list, here=None, calls=None):
    """질문마다 백엔드를 새로 읽어 답을 만듦. (질문, answers.Reply)"""
    asked = [m["content"] for m in messages if m["role"] == "user"]
    question = asked[-1]
    # 앞 질문의 구역은 "거기 사고 유형은?"처럼 가리킬 때만 이어받음: 늘 섞으면 "최근 데이터" 같은 질문에도 앞 구역이 붙음
    if len(asked) > 1 and re.search(FOLLOW_WORDS, question):
        question = f"{asked[-2]} {question}"
    zones, events = load_data(calls)
    return question, route(question, zones, events, here)


def analyze(question: str, data: str):
    """AI 분석 조각을 받는 대로 내는 제너레이터를 반환. 연결 실패는 호출 시점에 예외."""
    body = {
        "model": CHAT_MODEL,
        # 이전 대화는 안 줌: 튜닝이 한 턴 형식이라 이전 답을 베낌. 앞 구역은 reply_for 가 질문으로 이어받음
        "messages": analysis_messages(question, data),
        "adapters": CHAT_ADAPTERS,  # Ollama 는 무시 (튜닝 결과가 모델에 합쳐져 있음)
        "max_tokens": 120,  # 분석 1~2문장(약 60토큰)은 담고, CPU 초당 약 10토큰에서 12초 안에 끝나는 상한
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


def typed(text: str):
    """글을 AI 가 쓰듯 내는 제너레이터: 토큰처럼 1~3자씩, 속도는 조금씩 흔들리고 문장 끝·줄·문단에서 잠깐 멈춤.
    긴 글은 TYPE_MAX_S 안에 끝나게 전체를 같은 비율로 빠르게."""
    if not TYPE_CPS:
        yield text
        return
    parts = re.findall(r"\s*\S{1,3}|\s+", text)  # 띄어쓰기는 다음 조각 앞에 붙임
    before = [.25 if p.startswith("\n\n") else .1 if p.startswith("\n") else 0 for p in parts]
    after = [len(p.strip()) / TYPE_CPS * random.uniform(.6, 1.4) + (.12 if p.rstrip()[-1:] in ".?!" else 0) for p in parts]
    scale = min(1, TYPE_MAX_S / (sum(before) + sum(after) or 1))
    for part, b, a in zip(parts, before, after):
        time.sleep(b * scale)
        yield part
        time.sleep(a * scale)


def analysis_into(question: str, data: str, out: queue.Queue, stop: threading.Event):
    """AI 분석 조각을 out 에 넣음 (끝은 None, 실패는 예외). 본문을 쓰는 동안 미리 돌려 모델이 데이터를 읽는 시간을 겹침."""
    try:
        with contextlib.closing(analyze(question, data)) as pieces:  # 사용자가 끊으면 모델 생성도 중단
            for text in pieces:
                if stop.is_set():
                    break
                out.put(text)
    except Exception as e:
        out.put(e)
    out.put(None)


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

    def send_answer(self, question, reply):
        """본문 → AI 분석 → 출처를 AI 가 쓰듯 조금씩. AI 분석은 본문을 쓰는 동안 미리 시작.
        길이를 모르므로 연결 종료가 응답의 끝 (HTTP/1.0)."""
        pieces, stop = queue.Queue(), threading.Event()
        if reply.data:
            threading.Thread(target=analysis_into, args=(question, reply.data, pieces, stop), daemon=True).start()

        def write(text):
            for piece in typed(text):
                self.wfile.write(piece.encode())

        try:
            write(reply.body)
            if reply.data:
                write("\n\n### 🤖 AI 분석\n\n")
                wrote = False
                while (text := pieces.get()) is not None:
                    if isinstance(text, Exception):  # 모델 서버가 꺼졌거나 시간 초과: 코드가 쓴 기준 분석으로 대신
                        write("*(분석이 중간에 끊겼습니다.)*" if wrote else reply.reference)
                        break
                    write(text)  # 본문을 쓰는 동안 쌓인 조각도 한꺼번에 내지 않고 같은 속도로
                    wrote = True
            write(f"\n\n---\n{reply.note}")
        except (BrokenPipeError, ConnectionResetError):  # 사용자가 끊음: 보낼 곳 없음
            stop.set()

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
            if not arg or arg[-1]["role"] != "user":
                raise ValueError("no question")
        except (ValueError, TypeError, KeyError):
            return self.send(400, {"error": '요청 형식이 올바르지 않습니다. {"messages": [{"role": "user", "content": "..."}]}'})
        calls = []
        try:
            question, reply = reply_for(arg, here_of(data.get("latitude"), data.get("longitude")), calls)
        except (urllib.error.URLError, TimeoutError):
            return self.send(502, {"error": "백엔드 데이터를 읽지 못했습니다. 잠시 후 다시 시도하세요."})
        self.start_stream()
        if data.get("trace") is True:  # 시연 콘솔: 첫 줄에 통신 기록과 AI 에 준 데이터를 먼저 보내고, 답은 그 뒤로 이어짐
            facts = reply.data or "(AI 분석 없음: 고정 안내·도움말은 코드가 바로 답함)"
            self.wfile.write(json.dumps({"calls": calls, "facts": facts, "model": CHAT_MODEL}, ensure_ascii=False).encode() + b"\n")
        self.send_answer(question, reply)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    print(f"http://localhost:{port}  (채팅: {CHAT_URL}, 백엔드: {BACKEND_URL or '예시 데이터'})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
