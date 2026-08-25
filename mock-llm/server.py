"""Deterministic multi-task CPU runtime for the KServe gateway fixture."""

import hashlib
import json
import math
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MODEL = os.getenv("MODEL_NAME", "mock-kserve")
UPSTREAM = os.getenv("UPSTREAM_ID", "unknown")
# Accelerator class of the node pool this replica runs on. Empty serves the
# whole catalog from one pod, which is what the single-service fixture does.
ACCELERATOR = os.getenv("ACCELERATOR", "").strip().lower()
PORT = int(os.getenv("PORT", "8000"))
EMBEDDING_DIMENSIONS = 8
CREATED = 1767225600

# Deterministic mock catalog. Every entry is a fixture: no weights are loaded
# and no vendor API is contacted. The identifiers, tiers, and capability flags
# exist so gateway routing, model allow-lists, and client SDKs can be tested
# against a realistic model list.
CATALOG = [
    {
        "id": "kimi-k3",
        "accelerator": "b300",
        "task": "chat",
        "tier": "big",
        "owned_by": "moonshot-ai",
        "context_window": 262144,
        "max_output_tokens": 16384,
        "features": ["chat", "streaming", "tools"],
    },
    {
        "id": "glm-5.3",
        "accelerator": "b300",
        "task": "chat",
        "tier": "big",
        "owned_by": "zhipu-ai",
        "context_window": 204800,
        "max_output_tokens": 16384,
        "features": ["chat", "streaming", "tools"],
    },
    {
        "id": "deepseek-v4-pro",
        "accelerator": "b300",
        "task": "chat",
        "tier": "big",
        "owned_by": "deepseek",
        "context_window": 163840,
        "max_output_tokens": 32768,
        "features": ["chat", "streaming", "tools", "reasoning"],
    },
    {
        "id": "deepseek-v4-flash",
        "accelerator": "h200",
        "task": "chat",
        "tier": "medium",
        "owned_by": "deepseek",
        "context_window": 131072,
        "max_output_tokens": 8192,
        "features": ["chat", "streaming", "tools"],
    },
    {
        "id": "qwen3.8-27b",
        "accelerator": "h100",
        "task": "chat",
        "tier": "small",
        "owned_by": "alibaba",
        "context_window": 65536,
        "max_output_tokens": 8192,
        "features": ["chat", "streaming", "tools"],
    },
    {
        "id": "mock-kserve",
        "accelerator": "cpu",
        "task": "chat",
        "tier": "fixture",
        "owned_by": "kserve-mock",
        "context_window": 8192,
        "max_output_tokens": 1024,
        "features": ["chat", "streaming"],
    },
    {
        "id": "qwen3-embedding-8b",
        "accelerator": "h100",
        "task": "embedding",
        "tier": "big",
        "owned_by": "alibaba",
        "context_window": 32768,
        "dimensions": 4096,
        "matryoshka": True,
        "features": ["retrieval", "multilingual", "instruction-prefix"],
    },
    {
        "id": "bge-m3",
        "accelerator": "l40s",
        "task": "embedding",
        "tier": "medium",
        "owned_by": "baai",
        "context_window": 8192,
        "dimensions": 1024,
        "matryoshka": False,
        "features": ["retrieval", "multilingual", "long-context"],
    },
    {
        "id": "e5-mistral-7b-instruct",
        "accelerator": "h100",
        "task": "embedding",
        "tier": "big",
        "owned_by": "microsoft",
        "context_window": 32768,
        "dimensions": 4096,
        "matryoshka": False,
        "features": ["retrieval", "instruction-prefix"],
    },
    {
        "id": "jina-embeddings-v3",
        "accelerator": "l40s",
        "task": "embedding",
        "tier": "medium",
        "owned_by": "jina-ai",
        "context_window": 8192,
        "dimensions": 1024,
        "matryoshka": True,
        "features": ["retrieval", "multilingual", "task-lora"],
    },
    {
        "id": "nomic-embed-text-v2-moe",
        "accelerator": "l40s",
        "task": "embedding",
        "tier": "small",
        "owned_by": "nomic-ai",
        "context_window": 2048,
        "dimensions": 768,
        "matryoshka": True,
        "features": ["retrieval", "multilingual"],
    },
    {
        "id": "mock-embedding",
        "accelerator": "cpu",
        "task": "embedding",
        "tier": "fixture",
        "owned_by": "kserve-mock",
        "context_window": 2048,
        "dimensions": EMBEDDING_DIMENSIONS,
        "matryoshka": False,
        "features": ["retrieval"],
    },
    {
        "id": "bge-reranker-v2-m3",
        "accelerator": "l40s",
        "task": "rerank",
        "tier": "medium",
        "owned_by": "baai",
        "context_window": 8192,
        "max_documents": 256,
        "features": ["rerank", "multilingual"],
    },
    {
        "id": "jina-reranker-v2-base-multilingual",
        "accelerator": "l40s",
        "task": "rerank",
        "tier": "small",
        "owned_by": "jina-ai",
        "context_window": 8192,
        "max_documents": 128,
        "features": ["rerank", "multilingual"],
    },
    {
        "id": "mock-reranker",
        "accelerator": "cpu",
        "task": "rerank",
        "tier": "fixture",
        "owned_by": "kserve-mock",
        "context_window": 2048,
        "max_documents": 64,
        "features": ["rerank"],
    },
    {
        "id": "whisper-large-v3",
        "accelerator": "l40s",
        "task": "transcription",
        "tier": "big",
        "owned_by": "openai",
        "features": ["asr", "diarization", "timestamps", "translation"],
        "max_speakers": 8,
    },
    {
        "id": "voxtral-small-24b",
        "accelerator": "h100",
        "task": "transcription",
        "tier": "big",
        "owned_by": "mistral-ai",
        "features": ["asr", "diarization", "timestamps", "audio-understanding"],
        "max_speakers": 8,
    },
    {
        "id": "voxtral-mini-3b",
        "accelerator": "l40s",
        "task": "transcription",
        "tier": "small",
        "owned_by": "mistral-ai",
        "features": ["asr", "timestamps"],
        "max_speakers": 1,
    },
    {
        "id": "mock-whisper",
        "accelerator": "cpu",
        "task": "transcription",
        "tier": "fixture",
        "owned_by": "kserve-mock",
        "features": ["asr", "diarization", "timestamps"],
        "max_speakers": 4,
    },
]

MODELS = {entry["id"]: entry for entry in CATALOG}

# A MODEL_NAME that is not in the catalog still has to serve chat, because the
# LLMInferenceService names the served model.
if MODEL not in MODELS:
    MODELS[MODEL] = {
        "id": MODEL,
        "accelerator": ACCELERATOR or "cpu",
        "task": "chat",
        "tier": "fixture",
        "owned_by": "kserve-mock",
        "context_window": 8192,
        "max_output_tokens": 1024,
        "features": ["chat", "streaming"],
    }

# Models this replica serves. A pod in an accelerator pool answers only for the
# models its cards are sized for; anything else belongs to another pool.
SERVED = {
    model_id: entry
    for model_id, entry in MODELS.items()
    if not ACCELERATOR or entry["accelerator"] == ACCELERATOR
}


def default_model(task, preferred):
    """Return the model used when a request omits one, if this pool serves it."""
    if preferred in SERVED and SERVED[preferred]["task"] == task:
        return preferred
    for model_id, entry in SERVED.items():
        if entry["task"] == task:
            return model_id
    return None


DEFAULT_MODEL = {
    "chat": default_model("chat", MODEL),
    "embedding": default_model("embedding", "mock-embedding"),
    "rerank": default_model("rerank", "mock-reranker"),
    "transcription": default_model("transcription", "mock-whisper"),
}

# Completion length reported per tier, so a client can tell the tiers apart.
TIER_COMPLETION_TOKENS = {"big": 48, "medium": 24, "small": 12, "fixture": 5}

TRUE_VALUES = ("1", "true", "yes", "on")

TRANSCRIPT_LINES = [
    "the gateway routed this request to the inference pool",
    "the endpoint picker selected one of the model replicas",
    "speech to text runs on the same multi task runtime",
    "diarization labels each speaker turn in the audio",
    "every response in this fixture is deterministic",
    "reranking and embeddings share the model catalog",
]


def token_count(value):
    return len(re.findall(r"\w+", value, re.UNICODE))


def digest_int(*parts):
    """Return a stable integer derived from the given parts."""
    seed = "|".join(str(part) for part in parts).encode()
    return int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")


def deterministic_embedding(value, dimensions=EMBEDDING_DIMENSIONS):
    """Return a stable normalized vector without a model dependency."""
    components = []
    block = 0
    while len(components) < dimensions:
        digest = hashlib.sha256(f"{block}:{value}".encode()).digest()
        for offset in range(0, len(digest), 2):
            integer = int.from_bytes(digest[offset : offset + 2], "big")
            components.append((integer / 32767.5) - 1.0)
        block += 1
    components = components[:dimensions]
    norm = math.sqrt(sum(component * component for component in components)) or 1.0
    return [round(component / norm, 6) for component in components]


def rerank_score(query, document):
    query_terms = set(re.findall(r"\w+", query.lower(), re.UNICODE))
    document_terms = set(re.findall(r"\w+", document.lower(), re.UNICODE))
    if not query_terms:
        return 0.0
    return round(len(query_terms & document_terms) / len(query_terms), 6)


def model_card(entry):
    """Return an OpenAI-compatible model object with the mock metadata."""
    card = {
        "id": entry["id"],
        "object": "model",
        "created": CREATED,
        "owned_by": entry["owned_by"],
        "task": entry["task"],
        "tier": entry["tier"],
        "accelerator": entry["accelerator"],
        "features": entry["features"],
        "mock": True,
    }
    extras = ("context_window", "max_output_tokens", "dimensions",
              "max_documents", "max_speakers")
    for key in extras:
        if key in entry:
            card[key] = entry[key]
    return card


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


def diarized_segments(seed, duration, speaker_count):
    """Split a mock transcript into deterministic speaker turns.

    Every pinned speaker has to get a turn, so a large num_speakers raises the
    segment count rather than leaving speakers out of the transcript.
    """
    segment_count = max(2 + digest_int(seed, "segments") % 4, speaker_count)
    span = round(duration / segment_count, 3)
    segments = []
    for index in range(segment_count):
        line = TRANSCRIPT_LINES[digest_int(seed, "line", index) % len(TRANSCRIPT_LINES)]
        start = round(index * span, 3)
        segments.append(
            {
                "id": index,
                "start": start,
                "end": round(start + span, 3),
                "text": line,
                "speaker": "SPEAKER_%02d" % (index % speaker_count),
                "no_speech_prob": round(digest_int(seed, "quiet", index) % 500 / 1e4, 4),
            }
        )
    return segments


def speaker_summary(segments):
    speakers = {}
    for segment in segments:
        entry = speakers.setdefault(
            segment["speaker"],
            {"id": segment["speaker"], "segments": 0, "speech_seconds": 0.0},
        )
        entry["segments"] += 1
        entry["speech_seconds"] += segment["end"] - segment["start"]
    for entry in speakers.values():
        entry["speech_seconds"] = round(entry["speech_seconds"], 3)
    return sorted(speakers.values(), key=lambda entry: entry["id"])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[{UPSTREAM}] {fmt % args}", flush=True)

    def send_response(self, *args, **kwargs):
        self.response_started = True
        BaseHTTPRequestHandler.send_response(self, *args, **kwargs)

    def dispatch(self, handler):
        """Run a handler, answering with an error instead of a dropped socket."""
        self.response_started = False
        try:
            handler()
        except Exception as error:  # noqa: BLE001 - the fixture must still answer
            self.log_message("handler failed: %r", error)
            if not self.response_started:
                self.send_error_payload(
                    500, f"mock runtime error: {error}", "internal_error"
                )

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.write_body(body)

    def send_text(self, status, body, content_type="text/plain; charset=utf-8"):
        payload = body.encode()
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.write_body(payload)

    def write_body(self, body):
        # Health/metrics scrapers use short deadlines and can disconnect while
        # a resource-constrained retained cluster is resuming. That is not a
        # runtime error and must not emit a traceback for every abandoned read.
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_error_payload(self, status, message, error_type, code=None):
        error = {"message": message, "type": error_type}
        if code:
            error["code"] = code
        self.send_json(status, {"error": error})

    def send_request_error(self, message):
        self.send_error_payload(400, message, "invalid_request_error")

    def resolve_model(self, name, task):
        """Return the catalog entry for name, or None after sending an error."""
        if name is None:
            name = DEFAULT_MODEL[task]
            if name is None:
                self.send_request_error(
                    f"this {ACCELERATOR} pool serves no {task} models; "
                    "name a model from GET /v1/models"
                )
                return None
        if not isinstance(name, str):
            self.send_request_error("model must be a string")
            return None
        entry = MODELS.get(name)
        if entry is None:
            self.send_error_payload(
                404,
                f"model '{name}' is not in this mock catalog; see GET /v1/models",
                "invalid_request_error",
                "model_not_found",
            )
            return None
        if name not in SERVED:
            self.send_error_payload(
                404,
                f"model '{name}' runs on {entry['accelerator']} and is not served "
                f"by this {ACCELERATOR} pool",
                "invalid_request_error",
                "model_not_served_here",
            )
            return None
        if entry["task"] != task:
            self.send_request_error(
                f"model '{name}' serves the {entry['task']} task, not {task}"
            )
            return None
        return entry

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

    def routing_headers(self):
        """Return the routing headers a semantic router adds to the request.

        The router runs as the gateway's external processor and names the model
        it chose in x-selected-model; its x-vsr-* decision headers go on the
        response, which this pod never sees. Echoing what did arrive is what
        lets the comparison tell a request the router rewrote from one that
        reached this pod untouched.
        """
        return {
            name.lower(): value
            for name, value in self.headers.items()
            if name.lower() == "x-selected-model"
            or name.lower().startswith("x-vsr-")
        }

    def gateway_headers(self):
        """Echo only non-secret headers that prove gateway policy behavior."""
        allowed = {
            "x-auth-user",
            "x-auth-plan",
            "x-user-id",
            "x-model-class",
        }
        return {
            name.lower(): value
            for name, value in self.headers.items()
            if name.lower() in allowed
        }

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
        path, _, query = self.path.partition("?")
        if path == "/health":
            self.send_json(
                200,
                {
                    "status": "ok",
                    "pod": UPSTREAM,
                    "capabilities": ["chat", "embeddings", "rerank", "stt", "models"],
                    "accelerator": ACCELERATOR or "all",
                    "models": len(SERVED),
                },
            )
        elif path == "/metrics":
            self.send_text(
                200,
                "# HELP vllm:num_requests_running Number of running requests.\n"
                "# TYPE vllm:num_requests_running gauge\n"
                "vllm:num_requests_running 0\n"
                "# HELP vllm:num_requests_waiting Number of waiting requests.\n"
                "# TYPE vllm:num_requests_waiting gauge\n"
                "vllm:num_requests_waiting 0\n"
                "# HELP vllm:gpu_cache_usage_perc Mock KV cache usage.\n"
                "# TYPE vllm:gpu_cache_usage_perc gauge\n"
                "vllm:gpu_cache_usage_perc 0\n",
                "text/plain; version=0.0.4; charset=utf-8",
            )
        elif path == "/v1/models":
            self.dispatch(lambda: self.handle_model_list(query))
        elif path.startswith("/v1/models/"):
            self.dispatch(lambda: self.handle_model_read(path[len("/v1/models/") :]))
        else:
            self.send_json(404, {"error": "not found"})

    def handle_model_list(self, query):
        filters = {}
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            if key in ("task", "tier", "accelerator") and value:
                filters[key] = value
        cards = [
            model_card(entry)
            for entry in SERVED.values()
            if all(entry.get(key) == value for key, value in filters.items())
        ]
        self.send_json(
            200,
            {
                "object": "list",
                "data": sorted(cards, key=lambda card: (card["task"], card["id"])),
                "mock_pod": UPSTREAM,
                "mock_accelerator": ACCELERATOR or "all",
            },
        )

    def handle_model_read(self, model_id):
        entry = MODELS.get(model_id)
        if entry is None:
            self.send_error_payload(
                404,
                f"model '{model_id}' does not exist in this mock catalog",
                "invalid_request_error",
                "model_not_found",
            )
            return
        if model_id not in SERVED:
            self.send_error_payload(
                404,
                f"model '{model_id}' runs on {entry['accelerator']} and is not served "
                f"by this {ACCELERATOR} pool",
                "invalid_request_error",
                "model_not_served_here",
            )
            return
        self.send_json(200, model_card(entry))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        handlers = {
            "/v1/chat/completions": self.handle_chat,
            "/v1/embeddings": self.handle_embeddings,
            "/v1/rerank": self.handle_rerank,
            "/v2/rerank": self.handle_rerank,
            "/rerank": self.handle_rerank,
            "/v1/audio/transcriptions": self.handle_transcription,
        }
        handler = handlers.get(path)
        if not handler:
            self.send_json(404, {"error": "not found"})
            return
        self.dispatch(handler)

    def handle_chat(self):
        request = self.read_json()
        if request is None:
            return
        entry = self.resolve_model(request.get("model"), "chat")
        if entry is None:
            return
        model = entry["id"]
        messages = request.get("messages", [])
        if not isinstance(messages, list):
            self.send_request_error("messages must be an array")
            return
        max_tokens = request.get("max_tokens", request.get("max_completion_tokens"))
        if max_tokens is not None:
            if not isinstance(max_tokens, int) or max_tokens <= 0:
                self.send_request_error("max_tokens must be a positive integer")
                return
            if max_tokens > entry["max_output_tokens"]:
                self.send_error_payload(
                    400,
                    f"max_tokens {max_tokens} exceeds the {entry['max_output_tokens']} "
                    f"token output limit of '{model}'",
                    "invalid_request_error",
                    "context_length_exceeded",
                )
                return

        prompt = " ".join(
            message.get("content", "")
            for message in messages
            if isinstance(message, dict) and isinstance(message.get("content"), str)
        )
        text = (
            f"Hello from {UPSTREAM} ({model}) - {entry['tier']} tier, "
            f"{entry['context_window']} token context"
        )
        prompt_tokens = max(token_count(prompt), 1)
        completion_tokens = TIER_COMPLETION_TOKENS[entry["tier"]]
        finish_reason = "stop"
        # A caller that asks for fewer tokens than the tier emits gets a
        # generation that stops at its limit, like a real runtime.
        if max_tokens is not None and max_tokens < completion_tokens:
            completion_tokens = max_tokens
            text = " ".join(text.split()[:max_tokens])
            finish_reason = "length"
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

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
                    "created": CREATED,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": text},
                            "finish_reason": finish_reason,
                        }
                    ],
                }
            ]
            stream_options = request.get("stream_options")
            if isinstance(stream_options, dict) and stream_options.get("include_usage"):
                chunks.append(
                    {
                        "id": "chatcmpl-kserve",
                        "object": "chat.completion.chunk",
                        "created": CREATED,
                        "model": model,
                        "choices": [],
                        "usage": usage,
                    }
                )
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
                "created": CREATED,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": usage,
                "mock_pod": UPSTREAM,
                "mock_tier": entry["tier"],
                "mock_accelerator": ACCELERATOR or "all",
                "model_accelerator": entry["accelerator"],
                # A router that replaced the system prompt is visible here even
                # when it set no headers at all.
                "mock_system_prompt": any(
                    isinstance(message, dict) and message.get("role") == "system"
                    for message in messages
                ),
                "mock_routing_headers": self.routing_headers(),
                "mock_gateway_headers": self.gateway_headers(),
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
        entry = self.resolve_model(request.get("model"), "embedding")
        if entry is None:
            return
        dimensions = request.get("dimensions", entry["dimensions"])
        if not isinstance(dimensions, int) or dimensions <= 0:
            self.send_request_error("dimensions must be a positive integer")
            return
        if dimensions != entry["dimensions"]:
            if not entry["matryoshka"]:
                self.send_request_error(
                    f"model '{entry['id']}' only returns "
                    f"{entry['dimensions']}-dimensional vectors"
                )
                return
            if dimensions > entry["dimensions"]:
                self.send_request_error(
                    f"dimensions must not exceed {entry['dimensions']} "
                    f"for '{entry['id']}'"
                )
                return
        tokens = sum(token_count(item) for item in inputs)
        self.send_json(
            200,
            {
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "embedding": deterministic_embedding(item, dimensions),
                        "index": index,
                    }
                    for index, item in enumerate(inputs)
                ],
                "model": entry["id"],
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
                "mock_pod": UPSTREAM,
                "mock_accelerator": ACCELERATOR or "all",
                "model_accelerator": entry["accelerator"],
                "mock_dimensions": dimensions,
                "mock_routing_headers": self.routing_headers(),
                "mock_gateway_headers": self.gateway_headers(),
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
        entry = self.resolve_model(request.get("model"), "rerank")
        if entry is None:
            return
        if len(documents) > entry["max_documents"]:
            self.send_request_error(
                f"model '{entry['id']}' accepts at most "
                f"{entry['max_documents']} documents"
            )
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
                "model": entry["id"],
                "results": ranked,
                "usage": {
                    "prompt_tokens": token_count(query)
                    + sum(token_count(document) for document in documents),
                    "total_tokens": token_count(query)
                    + sum(token_count(document) for document in documents),
                },
                "mock_pod": UPSTREAM,
                "mock_accelerator": ACCELERATOR or "all",
                "model_accelerator": entry["accelerator"],
                "mock_routing_headers": self.routing_headers(),
                "mock_gateway_headers": self.gateway_headers(),
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
        entry = self.resolve_model(fields.get("model"), "transcription")
        if entry is None:
            return
        filename = audio["filename"] or "audio"
        size = len(audio["content"])
        response_format = fields.get("response_format", "json")
        if response_format not in ("json", "verbose_json", "text"):
            self.send_request_error(
                "response_format must be json, verbose_json, or text"
            )
            return

        diarize = fields.get("diarization", "false").strip().lower() in TRUE_VALUES
        if diarize and "diarization" not in entry["features"]:
            self.send_request_error(
                f"model '{entry['id']}' performs ASR only, without diarization"
            )
            return
        speaker_count = 1
        if diarize:
            requested = fields.get("num_speakers")
            if requested is not None:
                if not requested.isdigit() or int(requested) < 1:
                    self.send_request_error("num_speakers must be a positive integer")
                    return
                speaker_count = int(requested)
                if speaker_count > entry["max_speakers"]:
                    self.send_request_error(
                        f"model '{entry['id']}' diarizes at most "
                        f"{entry['max_speakers']} speakers"
                    )
                    return
            else:
                speaker_count = min(
                    2 + digest_int(filename, size, "speakers") % 2,
                    entry["max_speakers"],
                )

        seed = f"{filename}:{size}"
        segments = diarized_segments(seed, max(size / 16000, 3.0), speaker_count)
        duration = round(segments[-1]["end"], 3)
        transcript = ", ".join(segment["text"] for segment in segments)
        text = (
            f"Mock transcription of {filename} ({size} bytes) "
            f"from {UPSTREAM}: {transcript}"
        )

        if response_format == "text":
            body = text.encode()
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        payload = {
            "text": text,
            "model": entry["id"],
            "language": fields.get("language", "en"),
            "duration": duration,
            "mock_pod": UPSTREAM,
            "mock_accelerator": ACCELERATOR or "all",
            "model_accelerator": entry["accelerator"],
            "mock_routing_headers": self.routing_headers(),
            "mock_gateway_headers": self.gateway_headers(),
        }
        if response_format == "verbose_json" or diarize:
            if not diarize:
                for segment in segments:
                    segment.pop("speaker")
            payload["task"] = "transcribe"
            payload["segments"] = segments
        if diarize:
            payload["speakers"] = speaker_summary(segments)
            payload["diarization"] = True
        self.send_json(200, payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
