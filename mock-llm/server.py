#!/usr/bin/env python3
"""
Minimal OpenAI-compatible mock LLM.

Exists so the gateway comparison runs with zero credentials and deterministic
token counts -- both gateways meter/ratelimit on `usage`, so the mock must
report it, including on the final SSE chunk when stream_options.include_usage
is set.

Env:
  MODEL_NAME    advertised model id (default: mock-gpt)
  LATENCY_MS    artificial per-request delay
  PORT          listen port (default 8080)
"""
import json, os, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = os.environ.get("MODEL_NAME", "mock-gpt")
LATENCY = float(os.environ.get("LATENCY_MS", "0")) / 1000.0
PORT = int(os.environ.get("PORT", "8080"))
UPSTREAM_ID = os.environ.get("UPSTREAM_ID", MODEL)


def _tokens(text: str) -> int:
    # deterministic, roughly 4 chars/token -- we care about stability, not fidelity
    return max(1, len(text) // 4)


def _prompt_text(body: dict) -> str:
    if "messages" in body:
        return " ".join(str(m.get("content", "")) for m in body.get("messages") or [])
    return str(body.get("prompt", ""))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep pod logs readable
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    # ---------- helpers ----------
    def _send(self, code, payload, ctype="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Mock-Upstream", UPSTREAM_ID)
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    # ---------- routes ----------
    def do_GET(self):
        if self.path in ("/health", "/healthz", "/ready"):
            return self._send(200, {"status": "ok", "model": MODEL})
        if self.path.endswith("/models"):
            return self._send(200, {
                "object": "list",
                "data": [{"id": MODEL, "object": "model", "owned_by": "mock",
                          "created": int(time.time())}],
            })
        return self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self):
        body = self._read_body()
        if LATENCY:
            time.sleep(LATENCY)

        if self.path.endswith("/embeddings"):
            inp = body.get("input")
            items = inp if isinstance(inp, list) else [inp or ""]
            return self._send(200, {
                "object": "list",
                "model": body.get("model", MODEL),
                "data": [{"object": "embedding", "index": i,
                          "embedding": [0.0011 * ((i + j) % 97) for j in range(8)]}
                         for i, _ in enumerate(items)],
                "usage": {"prompt_tokens": sum(_tokens(str(x)) for x in items),
                          "total_tokens": sum(_tokens(str(x)) for x in items)},
            })

        is_chat = self.path.endswith("/chat/completions")
        if not (is_chat or self.path.endswith("/completions")):
            return self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

        model = body.get("model", MODEL)
        p_tok = _tokens(_prompt_text(body))
        reply = f"Hello from {UPSTREAM_ID} (model={model}). This is a mock completion."
        c_tok = _tokens(reply)
        usage = {"prompt_tokens": p_tok, "completion_tokens": c_tok,
                 "total_tokens": p_tok + c_tok}
        rid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        if body.get("stream"):
            return self._stream(rid, created, model, reply, usage, is_chat, body)

        if is_chat:
            payload = {"id": rid, "object": "chat.completion", "created": created,
                       "model": model, "usage": usage,
                       "choices": [{"index": 0, "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": reply}}]}
        else:
            payload = {"id": rid, "object": "text_completion", "created": created,
                       "model": model, "usage": usage,
                       "choices": [{"index": 0, "finish_reason": "stop", "text": reply}]}
        return self._send(200, payload)

    def _stream(self, rid, created, model, reply, usage, is_chat, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Mock-Upstream", UPSTREAM_ID)
        self.send_header("Connection", "close")
        self.end_headers()
        obj = "chat.completion.chunk" if is_chat else "text_completion"

        def emit(chunk):
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()

        for word in reply.split(" "):
            delta = {"content": word + " "} if is_chat else None
            choice = ({"index": 0, "delta": delta, "finish_reason": None} if is_chat
                      else {"index": 0, "text": word + " ", "finish_reason": None})
            emit({"id": rid, "object": obj, "created": created, "model": model,
                  "choices": [choice]})
            time.sleep(0.02)

        final = {"index": 0, "finish_reason": "stop"}
        final["delta" if is_chat else "text"] = {} if is_chat else ""
        emit({"id": rid, "object": obj, "created": created, "model": model,
              "choices": [final]})

        # only advertise usage when asked -- mirrors real OpenAI behaviour that
        # both gateways' token metering paths depend on
        if (body.get("stream_options") or {}).get("include_usage"):
            emit({"id": rid, "object": obj, "created": created, "model": model,
                  "choices": [], "usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    print(f"mock-llm listening :{PORT} model={MODEL} upstream={UPSTREAM_ID}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
