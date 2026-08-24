"""Small OpenAI-compatible CPU runtime for the KServe integration fixture."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MODEL = os.getenv("MODEL_NAME", "mock-kserve")
UPSTREAM = os.getenv("UPSTREAM_ID", "unknown")
PORT = int(os.getenv("PORT", "8000"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[{UPSTREAM}] {fmt % args}", flush=True)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"status": "ok", "pod": UPSTREAM})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("content-length", "0"))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
            return

        model = request.get("model", MODEL)
        text = f"Hello from {UPSTREAM} ({model})"
        usage = {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}

        if request.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()
            chunks = [
                {
                    "id": "chatcmpl-kserve",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": text}}],
                },
                {
                    "id": "chatcmpl-kserve",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [],
                    "usage": usage,
                },
            ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        self.send_json(
            200,
            {
                "id": "chatcmpl-kserve",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            },
        )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
