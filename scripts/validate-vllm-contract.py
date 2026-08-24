#!/usr/bin/env python3
"""Validate the API subset shared by the CPU mock and production vLLM."""

import argparse
import io
import json
import struct
import sys
import urllib.error
import urllib.request
import uuid
import wave


TASK_MODELS = {
    "chat": "mock-kserve",
    "embedding": "mock-embedding",
    "rerank": "mock-reranker",
    "transcription": "mock-whisper",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--routing-header", help="header used by production task routes")
    for task, default in TASK_MODELS.items():
        parser.add_argument(f"--{task}-model", default=default)
        parser.add_argument(f"--{task}-route", default=task)
    return parser.parse_args()


class Contract:
    def __init__(self, args):
        self.args = args
        self.base = args.base_url.rstrip("/")

    def headers(self, task, content_type="application/json"):
        result = {"content-type": content_type}
        if self.args.routing_header:
            result[self.args.routing_header] = getattr(self.args, f"{task}_route")
        return result

    def request(self, task, path, body=None, content_type="application/json"):
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers=self.headers(task, content_type),
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise AssertionError(f"{path} returned HTTP {error.code}: {detail}") from error

    def json(self, task, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        status, headers, raw = self.request(task, path, body)
        assert status == 200, (path, status)
        assert "application/json" in headers.get_content_type(), headers
        return json.loads(raw)

    def models(self):
        payload = self.json("chat", "/v1/models")
        assert payload["object"] == "list"
        assert any(item["id"] == self.args.chat_model for item in payload["data"])

    def chat(self):
        payload = self.json(
            "chat",
            "/v1/chat/completions",
            {
                "model": self.args.chat_model,
                "messages": [{"role": "user", "content": "Reply with gateway."}],
                "max_tokens": 16,
            },
        )
        assert payload["object"] == "chat.completion"
        assert isinstance(payload["created"], int)
        assert payload["choices"][0]["message"]["role"] == "assistant"
        assert payload["usage"]["total_tokens"] >= 1

    def stream(self):
        status, headers, raw = self.request(
            "chat",
            "/v1/chat/completions",
            json.dumps(
                {
                    "model": self.args.chat_model,
                    "messages": [{"role": "user", "content": "Reply with gateway."}],
                    "max_tokens": 16,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
            ).encode(),
        )
        assert status == 200
        assert "text/event-stream" in headers.get_content_type(), headers
        events = [
            line[6:]
            for line in raw.decode().splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        chunks = [json.loads(event) for event in events]
        assert chunks and all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
        assert any(chunk.get("usage", {}).get("total_tokens", 0) >= 1 for chunk in chunks)

    def embeddings(self):
        payload = self.json(
            "embedding",
            "/v1/embeddings",
            {"model": self.args.embedding_model, "input": ["gateway", "routing"]},
        )
        assert payload["object"] == "list"
        assert len(payload["data"]) == 2
        assert all(item["object"] == "embedding" and item["embedding"] for item in payload["data"])

    def rerank(self):
        payload = self.json(
            "rerank",
            "/v1/rerank",
            {
                "model": self.args.rerank_model,
                "query": "gateway routing",
                "documents": ["unrelated", "gateway model routing"],
                "top_n": 1,
                "return_documents": True,
            },
        )
        result = payload["results"][0]
        assert result["index"] == 1
        assert result["document"]["text"] == "gateway model routing"
        assert isinstance(result["relevance_score"], (int, float))
        assert payload["usage"]["total_tokens"] >= 1

    def transcription(self):
        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(struct.pack("<h", 0) * 4000)
        boundary = "contract-" + uuid.uuid4().hex
        fields = {"model": self.args.transcription_model, "response_format": "json"}
        parts = []
        for name, value in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"silence.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode()
            + audio.getvalue()
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        status, _, raw = self.request(
            "transcription",
            "/v1/audio/transcriptions",
            b"".join(parts),
            f"multipart/form-data; boundary={boundary}",
        )
        assert status == 200
        payload = json.loads(raw)
        assert isinstance(payload["text"], str)


def main():
    contract = Contract(parse_args())
    checks = (
        contract.models,
        contract.chat,
        contract.stream,
        contract.embeddings,
        contract.rerank,
        contract.transcription,
    )
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print("vLLM-compatible contract passed")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, ValueError) as error:
        print(f"FAIL {error}", file=sys.stderr)
        raise SystemExit(1)
