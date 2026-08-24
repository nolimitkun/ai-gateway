"""Deterministic multi-task CPU runtime for the KServe gateway fixture."""

import hashlib
import json
import math
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MODEL = os.getenv("MODEL_NAME", "mock-kserve")
UPSTREAM = os.getenv("UPSTREAM_ID", "unknown")
PORT = int(os.getenv("PORT", "8000"))
EMBEDDING_DIMENSIONS = 8


def token_count(value):
    return len(re.findall(r"\w+", value, re.UNICODE))


def deterministic_embedding(value):
    """Return a stable normalized vector without a model dependency."""
    digest = hashlib.sha256(value.encode()).digest()
    vector = []
    for offset in range(0, EMBEDDING_DIMENSIONS * 2, 2):
        integer = int.from_bytes(digest[offset : offset + 2], "big")
        vector.append((integer / 32767.5) - 1.0)
    norm = math.sqrt(sum(component * component for component in vector)) or 1.0
    return [round(component / norm, 6) for component in vector]


def rerank_score(query, document):
    query_terms = set(re.findall(r"\w+", query.lower(), re.UNICODE))
    document_terms = set(re.findall(r"\w+", document.lower(), re.UNICODE))
    if not query_terms:
        return 0.0
    return round(len(query_terms & document_terms) / len(query_terms), 6)


def parse_multipart(content_type, body):
    boundary_match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
    if not boundary_match:
        raise ValueError("multipart boundary is missing")
    boundary = (boundary_match.group(1) or boundary_match.group(2)).strip().encode()
    fields = {}
    files = {}
    for part in body.split(b"--" + boundary):
        if not part or part in (b"--\r\n", b"--"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        raw_headers, separator, value = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        headers = raw_headers.decode("latin-1")
        name_match = re.search(r'name="([^"]+)"', headers, re.IGNORECASE)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', headers, re.IGNORECASE)
        if filename_match:
            files[name] = {
                "filename": filename_match.group(1),
                "content": value,
            }
        else:
            fields[name] = value.decode("utf-8", errors="replace")
    return fields, files


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

    def send_request_error(self, message):
        self.send_json(
            400,
            {"error": {"message": message, "type": "invalid_request_error"}},
        )

    def read_body(self):
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size_line = self.rfile.readline().split(b";", 1)[0].strip()
                size = int(size_line, 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)
        length = int(self.headers.get("content-length", "0"))
        return self.rfile.read(length)

    def read_json(self):
        try:
            payload = json.loads(self.read_body() or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self.send_request_error("request body must be valid JSON")
            return None
        if not isinstance(payload, dict):
            self.send_request_error("request body must be a JSON object")
            return None
        return payload

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self.send_json(
                200,
                {
                    "status": "ok",
                    "pod": UPSTREAM,
                    "capabilities": ["chat", "embeddings", "rerank", "stt"],
                },
            )
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        handlers = {
            "/v1/chat/completions": self.handle_chat,
            "/v1/embeddings": self.handle_embeddings,
            "/v1/rerank": self.handle_rerank,
            "/v1/audio/transcriptions": self.handle_transcription,
        }
        handler = handlers.get(path)
        if not handler:
            self.send_json(404, {"error": "not found"})
            return
        handler()

    def handle_chat(self):
        request = self.read_json()
        if request is None:
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

    def handle_embeddings(self):
        request = self.read_json()
        if request is None:
            return
        raw_input = request.get("input")
        if isinstance(raw_input, str):
            inputs = [raw_input]
        elif isinstance(raw_input, list) and all(
            isinstance(item, str) for item in raw_input
        ):
            inputs = raw_input
        else:
            self.send_request_error("input must be a string or an array of strings")
            return
        model = request.get("model", "mock-embedding")
        tokens = sum(token_count(item) for item in inputs)
        self.send_json(
            200,
            {
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "embedding": deterministic_embedding(item),
                        "index": index,
                    }
                    for index, item in enumerate(inputs)
                ],
                "model": model,
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
                "mock_pod": UPSTREAM,
            },
        )

    def handle_rerank(self):
        request = self.read_json()
        if request is None:
            return
        query = request.get("query")
        raw_documents = request.get("documents")
        if not isinstance(query, str) or not isinstance(raw_documents, list):
            self.send_request_error("query must be a string and documents an array")
            return
        documents = []
        for document in raw_documents:
            if isinstance(document, str):
                documents.append(document)
            elif isinstance(document, dict) and isinstance(document.get("text"), str):
                documents.append(document["text"])
            else:
                self.send_request_error("each document must be a string or text object")
                return
        top_n = request.get("top_n", len(documents))
        if not isinstance(top_n, int) or top_n < 0:
            self.send_request_error("top_n must be a non-negative integer")
            return
        ranked = sorted(
            (
                {
                    "index": index,
                    "relevance_score": rerank_score(query, document),
                    "document": {"text": document},
                }
                for index, document in enumerate(documents)
            ),
            key=lambda result: (-result["relevance_score"], result["index"]),
        )[:top_n]
        if not request.get("return_documents", False):
            for result in ranked:
                result.pop("document")
        self.send_json(
            200,
            {
                "id": "rerank-kserve",
                "model": request.get("model", "mock-reranker"),
                "results": ranked,
                "usage": {"search_units": 1},
                "mock_pod": UPSTREAM,
            },
        )

    def handle_transcription(self):
        content_type = self.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self.send_request_error("audio transcription requires multipart/form-data")
            return
        try:
            fields, files = parse_multipart(content_type, self.read_body())
        except ValueError as error:
            self.send_request_error(str(error))
            return
        audio = files.get("file")
        if audio is None:
            self.send_request_error("file is required")
            return
        filename = audio["filename"] or "audio"
        size = len(audio["content"])
        model = fields.get("model", "mock-whisper")
        self.send_json(
            200,
            {
                "text": f"Mock transcription of {filename} ({size} bytes) from {UPSTREAM}",
                "model": model,
                "language": fields.get("language", "en"),
                "duration": round(size / 16000, 3),
                "mock_pod": UPSTREAM,
            },
        )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
