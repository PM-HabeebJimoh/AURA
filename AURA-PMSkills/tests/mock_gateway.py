"""A tiny stand-in for an LLM gateway such as Arena.ai, used by the tests and by
``pm-engine`` demos when the real endpoint is unreachable.

    python -m tests.mock_gateway --port 9001 --format openai      # OpenAI-style: /v1/models, /v1/chat/completions
    python -m tests.mock_gateway --port 9002 --format anthropic   # Anthropic-style: /v1/messages
    python -m tests.mock_gateway --port 9003 --format both

Behaviour: requires ``Authorization: Bearer <key>`` (or ``x-api-key``) equal to
``--key`` (default ``test-arena-key``), otherwise 401. Streams SSE when
``stream: true``. The reply echoes the model, the first line of the system
prompt and the user's last message so tests can assert that the engine sent the
right prompt. Set ``--fail-first N`` to answer 529 to the first N requests
(exercises the retry path). ``--max-request-tokens N`` rejects requests whose
prompt (≈ chars/4) plus ``max_tokens`` exceeds N with a Groq-style ``413``
(exercises the engine's context budget).
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_KEY = "test-arena-key"
MODELS = ["claude-sonnet-4-5", "gpt-4o-mini", "gemini-2.5-pro", "llama-3.3-70b"]


def make_reply(model: str, system: str, user: str) -> str:
    head = (system or "").strip().splitlines()[0][:80] if system else ""
    return f"[mock {model}] system: {head} | user: {user.strip()[:120]}\n\n## Result\n\nThis is a mock completion.\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "MockGateway/1.0"
    fmt = "both"
    key = DEFAULT_KEY
    fail_first = 0
    max_request_tokens = 0
    seen: list[dict] = []
    lock = threading.Lock()

    def log_message(self, *a):  # silence
        pass

    # -- helpers ---------------------------------------------------------
    def _auth_ok(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.key}" or self.headers.get("x-api-key") == self.key

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, events: list[str]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for ev in events:
            self.wfile.write(ev.encode())
            self.wfile.flush()

    def _maybe_fail(self) -> bool:
        with self.lock:
            if type(self).fail_first > 0:
                type(self).fail_first -= 1
                self._json(529, {"error": {"type": "overloaded_error", "message": "Overloaded"}})
                return True
        return False

    def _too_large(self, payload: dict) -> bool:
        cap = type(self).max_request_tokens
        if not cap:
            return False
        text = str(payload.get("system", "")) + "".join(str(m.get("content", "")) for m in payload.get("messages", []))
        requested = len(text) // 4 + int(payload.get("max_tokens") or 0)
        if requested <= cap:
            return False
        self._json(413, {"error": {"message": f"Request too large for model {payload.get('model')} on tokens per minute (TPM): Limit {cap}, Requested {requested}, please reduce your message size and try again.", "type": "tokens", "code": "rate_limit_exceeded"}})
        return True

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models" and self.fmt in ("openai", "both"):
            if not self._auth_ok():
                return self._json(401, {"error": {"message": "invalid api key"}})
            return self._json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(n) or b"{}")
        with self.lock:
            type(self).seen.append({"path": self.path, "payload": payload, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if not self._auth_ok():
            return self._json(401, {"error": {"message": "invalid api key"}})
        if self._maybe_fail() or self._too_large(payload):
            return
        model = payload.get("model", "?")
        if self.path.rstrip("/") == "/v1/chat/completions" and self.fmt in ("openai", "both"):
            msgs = payload.get("messages", [])
            system = next((m["content"] for m in msgs if m.get("role") == "system"), "")
            user = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), "")
            text = make_reply(model, system, user)
            if payload.get("stream"):
                chunks = [text[i : i + 40] for i in range(0, len(text), 40)]
                evs = [f"data: {json.dumps({'id': 'x', 'object': 'chat.completion.chunk', 'model': model, 'choices': [{'index': 0, 'delta': {'content': c}}]})}\n\n" for c in chunks]
                return self._sse(evs + ["data: [DONE]\n\n"])
            return self._json(200, {"id": "x", "object": "chat.completion", "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": len(system) // 4, "completion_tokens": len(text) // 4}})
        if self.path.rstrip("/") == "/v1/messages" and self.fmt in ("anthropic", "both"):
            if not self.headers.get("anthropic-version"):
                return self._json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "anthropic-version header is required"}})
            system = payload.get("system", "")
            msgs = payload.get("messages", [])
            user = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), "")
            text = make_reply(model, system, user)
            if payload.get("stream"):
                chunks = [text[i : i + 40] for i in range(0, len(text), 40)]
                evs = ["event: message_start\ndata: {\"type\": \"message_start\"}\n\n"]
                evs += [f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': c}})}\n\n" for c in chunks]
                evs += ["event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"]
                return self._sse(evs)
            return self._json(200, {"id": "m", "type": "message", "role": "assistant", "model": model, "content": [{"type": "text", "text": text}], "usage": {"input_tokens": len(system) // 4, "output_tokens": len(text) // 4}})
        # unknown route for this format → mimic a gateway that does not speak that protocol
        self._json(404, {"error": {"message": f"no route {self.path}"}})


def serve(port: int, fmt: str = "both", key: str = DEFAULT_KEY, fail_first: int = 0, host: str = "127.0.0.1", max_request_tokens: int = 0) -> ThreadingHTTPServer:
    handler = type("H", (Handler,), {"fmt": fmt, "key": key, "fail_first": fail_first, "max_request_tokens": max_request_tokens, "seen": []})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.handler_cls = handler  # type: ignore[attr-defined]
    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9001)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--format", choices=["openai", "anthropic", "both"], default="both")
    ap.add_argument("--key", default=DEFAULT_KEY)
    ap.add_argument("--fail-first", type=int, default=0)
    ap.add_argument("--max-request-tokens", type=int, default=0, help="reject prompt+max_tokens above N with 413 (Groq free tier: 8000)")
    a = ap.parse_args()
    srv = serve(a.port, a.format, a.key, a.fail_first, a.host, a.max_request_tokens)
    print(f"mock gateway ({a.format}) on http://{a.host}:{a.port}/v1  key={a.key}" + (f"  max-request-tokens={a.max_request_tokens}" if a.max_request_tokens else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
