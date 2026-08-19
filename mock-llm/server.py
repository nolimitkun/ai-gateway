#!/usr/bin/env python3
"""
Minimal OpenAI-compatible mock LLM.

Exists so the gateway comparison runs with zero credentials and deterministic
token counts -- both gateways meter/ratelimit on `usage`, so the mock must
report it, including on the final SSE chunk when stream_options.include_usage
is set.

Beyond the OpenAI surface it also speaks Anthropic's /v1/messages, records
what it actually received, and can be failed at runtime -- so the comparison can
*prove* cross-provider translation, credential injection and failover instead of
asserting them.

Introspection (not part of any provider API, prefixed to avoid collisions):
  GET  /__requests   last 50 requests: path, auth header, model, body keys
  POST /__fail?on=1  start returning 503; on=0 restores. Used for failover tests.

Env:
  MODEL_NAME    advertised model id (default: mock-gpt)
  LATENCY_MS    artificial per-request delay
  PORT          listen port (default 8080)
"""
import json, os, threading, time, uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MODEL = os.environ.get("MODEL_NAME", "mock-gpt")
LATENCY = float(os.environ.get("LATENCY_MS", "0")) / 1000.0
PORT = int(os.environ.get("PORT", "8080"))
UPSTREAM_ID = os.environ.get("UPSTREAM_ID", MODEL)

RECENT = deque(maxlen=50)          # request introspection ring buffer
STATE = {"fail": False}            # runtime failure toggle
LOCK = threading.Lock()


def _record(handler, body):
    """Remember what the upstream actually received.

    The gateway is the only thing between the client and here, so the recorded
    path/headers are direct evidence of what it rewrote.
    """
    with LOCK:
        RECENT.append({
            "path": handler.path,
            "method": handler.command,
            "authorization": handler.headers.get("authorization"),
            "api_key_header": handler.headers.get("x-api-key"),
            "content_type": handler.headers.get("content-type"),
            "model": body.get("model"),
            "body_keys": sorted(body.keys()),
            "anthropic_version": handler.headers.get("anthropic-version"),
            "at": time.time(),
        })


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
        if self.path.startswith("/__requests"):
            with LOCK:
                return self._send(200, {"upstream": UPSTREAM_ID, "failing": STATE["fail"],
                                        "requests": list(RECENT)})
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
        if self.path.startswith("/__fail"):
            q = parse_qs(urlparse(self.path).query)
            STATE["fail"] = q.get("on", ["1"])[0] not in ("0", "false")
            return self._send(200, {"upstream": UPSTREAM_ID, "failing": STATE["fail"]})

        body = self._read_body()
        _record(self, body)

        # Simulated outage for failover tests. 503 is what both gateways treat
        # as a retryable/unhealthy upstream response.
        if STATE["fail"]:
            return self._send(503, {"error": {"message": f"{UPSTREAM_ID} is down",
                                              "type": "server_error"}})
        if LATENCY:
            time.sleep(LATENCY)

        # Anthropic's native surface. Reaching this path at all proves the
        # gateway translated an OpenAI-shaped client request into Anthropic's
        # wire format, rather than just proxying it through.
        if urlparse(self.path).path.endswith("/messages"):
            sys_txt = str(body.get("system", ""))
            p_tok = _tokens(sys_txt + _prompt_text(body))
            reply = f"Hello from {UPSTREAM_ID} via Anthropic Messages API."
            c_tok = _tokens(reply)
            return self._send(200, {
                "id": "msg_" + uuid.uuid4().hex[:24],
                "type": "message",
                "role": "assistant",
                "model": body.get("model", MODEL),
                "content": [{"type": "text", "text": reply}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": p_tok, "output_tokens": c_tok},
            })

        if urlparse(self.path).path.endswith("/embeddings"):
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

        # --- provider-native surfaces -------------------------------------
        # Serving the real upstream paths lets us verify the *round trip*: a
        # gateway that rewrites the request must also translate the native
        # response back into the client's dialect.
        path = urlparse(self.path).path

        if path.endswith("/converse"):          # AWS Bedrock Converse
            reply = f"Hello from {UPSTREAM_ID} via Bedrock Converse."
            p_tok = _tokens(_prompt_text(body)); c_tok = _tokens(reply)
            return self._send(200, {
                "output": {"message": {"role": "assistant",
                                       "content": [{"text": reply}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": p_tok, "outputTokens": c_tok,
                          "totalTokens": p_tok + c_tok},
            })

        if path.endswith(":rawPredict") or path.endswith("/invoke"):
            # Anthropic-on-cloud (Vertex rawPredict / Bedrock invoke) answers in
            # Anthropic's native Messages shape, not the cloud provider's.
            reply = f"Hello from {UPSTREAM_ID} via Anthropic on cloud."
            p_tok = _tokens(_prompt_text(body)); c_tok = _tokens(reply)
            return self._send(200, {
                "id": "msg_" + uuid.uuid4().hex[:24],
                "type": "message", "role": "assistant",
                "model": body.get("model", MODEL),
                "content": [{"type": "text", "text": reply}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": p_tok, "output_tokens": c_tok},
            })

        if path.endswith(":generateContent"):
            # Vertex AI / Gemini native
            reply = f"Hello from {UPSTREAM_ID} via Vertex generateContent."
            p_tok = _tokens(_prompt_text(body)); c_tok = _tokens(reply)
            return self._send(200, {
                "candidates": [{"content": {"role": "model",
                                            "parts": [{"text": reply}]},
                                "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": p_tok,
                                  "candidatesTokenCount": c_tok,
                                  "totalTokenCount": p_tok + c_tok},
            })

        is_chat = path.endswith("/chat/completions")
        if not (is_chat or path.endswith("/completions")):
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
