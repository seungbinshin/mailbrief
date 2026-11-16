import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from mailbrief.config import LLMConfig
from mailbrief.llm import OpenAICompatClient
from mailbrief.sources.graph import _parse

SEEN = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((self.path, self.headers.get("Authorization"), body))
        if "response_format" in body:  # server that does not support JSON mode
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"response_format not supported"}')
            return
        out = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_openai_compat_client_retries_without_json_mode(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("MAILBRIEF_LLM_API_KEY", "secret")
    cfg = LLMConfig(base_url=f"http://127.0.0.1:{srv.server_port}/v1", model="m", max_retries=2)
    try:
        assert OpenAICompatClient(cfg).chat("s", "u") == '{"ok": true}'
    finally:
        srv.shutdown()
    assert SEEN[0][0] == "/v1/chat/completions" and SEEN[0][1] == "Bearer secret"
    assert "response_format" in SEEN[0][2] and "response_format" not in SEEN[-1][2]


def test_graph_datetime_parse():
    tz = ZoneInfo("Asia/Seoul")
    d = _parse("2026-11-16T01:00:00.0000000", tz)          # calendarView with Prefer timezone
    assert d.hour == 1 and d.tzinfo == tz
    d = _parse("2026-11-16T01:00:00Z", tz)                  # receivedDateTime in UTC
    assert d.hour == 10


def test_system_in_user_and_extra_body():
    SEEN.clear()
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = LLMConfig(base_url=f"http://127.0.0.1:{srv.server_port}/v1", model="m", json_mode=False,
                    system_in_user=True, extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    try:
        OpenAICompatClient(cfg).chat("SYS", "USR")
    finally:
        srv.shutdown()
    body = SEEN[-1][2]
    assert [m["role"] for m in body["messages"]] == ["user"] and body["messages"][0]["content"].startswith("SYS")
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
