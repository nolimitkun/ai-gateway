import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest

from server import MODELS, Handler, ThreadingHTTPServer, deterministic_embedding


class RuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, body, content_type="application/json"):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        connection.request("POST", path, body=body, headers={"content-type": content_type})
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def request_raw(self, path, payload):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        connection.request(
            "POST", path, body=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read().decode()
        connection.close()
        return response.status, body

    def post_json(self, path, payload):
        return self.request(path, json.dumps(payload))

    def post_json_with_headers(self, path, payload, headers):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        connection.request(
            "POST", path, body=json.dumps(payload),
            headers={"content-type": "application/json", **headers},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def get(self, path):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        connection.request("GET", path)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def multipart(self, fields, filename="sample.wav"):
        boundary = "mock-boundary"
        body = b""
        for name, value in fields.items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode() + b"RIFFmock\r\n" + f"--{boundary}--\r\n".encode()
        return self.request(
            "/v1/audio/transcriptions",
            body,
            f"multipart/form-data; boundary={boundary}",
        )

    def test_chat_completion_remains_openai_compatible(self):
        status, payload = self.post_json(
            "/v1/chat/completions",
            {
                "model": "mock-kserve",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertIsInstance(payload["created"], int)
        self.assertEqual(payload["choices"][0]["message"]["role"], "assistant")

    def test_embeddings_are_deterministic(self):
        status, payload = self.post_json(
            "/v1/embeddings",
            {"model": "mock-embedding", "input": ["gateway", "gateway"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "list")
        self.assertEqual(len(payload["data"]), 2)
        self.assertEqual(payload["data"][0]["embedding"], deterministic_embedding("gateway"))
        self.assertEqual(payload["data"][0]["embedding"], payload["data"][1]["embedding"])

    def test_rerank_orders_by_query_overlap(self):
        status, payload = self.post_json(
            "/v1/rerank",
            {
                "query": "gateway inference",
                "documents": ["unrelated", "gateway inference routing", "gateway"],
                "top_n": 2,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["index"] for item in payload["results"]], [1, 2])
        self.assertTrue(all("document" not in item for item in payload["results"]))

    def test_vllm_rerank_aliases_share_the_contract(self):
        request = {
            "query": "gateway inference",
            "documents": ["unrelated", "gateway inference routing"],
            "top_n": 1,
            "return_documents": True,
        }
        for path in ("/rerank", "/v1/rerank", "/v2/rerank"):
            status, payload = self.post_json(path, request)
            self.assertEqual(status, 200)
            self.assertEqual(payload["results"][0]["index"], 1)
            self.assertEqual(
                payload["results"][0]["document"]["text"],
                "gateway inference routing",
            )

    def test_transcription_accepts_multipart_audio(self):
        boundary = "mock-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="model"\r\n\r\n'
            "mock-whisper\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="sample.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode() + b"RIFFmock\r\n" + f"--{boundary}--\r\n".encode()
        status, payload = self.request(
            "/v1/audio/transcriptions",
            body,
            f"multipart/form-data; boundary={boundary}",
        )
        self.assertEqual(status, 200)
        self.assertIn("sample.wav", payload["text"])
        self.assertEqual(payload["model"], "mock-whisper")

    def test_shared_vllm_contract_validator(self):
        validator = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "validate-vllm-contract.py"
        )
        result = subprocess.run(
            [
                sys.executable,
                validator,
                "--base-url",
                f"http://127.0.0.1:{self.server.server_address[1]}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_model_catalog_lists_every_task_and_tier(self):
        status, payload = self.get("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "list")
        self.assertEqual(len(payload["data"]), len(MODELS))
        ids = {card["id"] for card in payload["data"]}
        self.assertTrue(
            {"kimi-k3", "glm-5.3", "deepseek-v4-pro", "deepseek-v4-flash",
             "qwen3.8-27b"} <= ids
        )
        self.assertTrue({"whisper-large-v3", "voxtral-small-24b"} <= ids)
        self.assertTrue({"qwen3-embedding-8b", "bge-m3", "jina-embeddings-v3"} <= ids)
        self.assertTrue(all(card["object"] == "model" for card in payload["data"]))

    def test_model_catalog_filters_by_task_and_tier(self):
        status, payload = self.get("/v1/models?task=chat&tier=big")
        self.assertEqual(status, 200)
        self.assertEqual(
            sorted(card["id"] for card in payload["data"]),
            ["deepseek-v4-pro", "glm-5.3", "kimi-k3"],
        )

    def test_models_carry_their_accelerator_class(self):
        status, payload = self.get("/v1/models")
        self.assertEqual(status, 200)
        by_id = {card["id"]: card["accelerator"] for card in payload["data"]}
        self.assertEqual(by_id["kimi-k3"], "b300")
        self.assertEqual(by_id["deepseek-v4-pro"], "b300")
        self.assertEqual(by_id["deepseek-v4-flash"], "h200")
        self.assertEqual(by_id["qwen3.8-27b"], "h100")
        self.assertEqual(by_id["whisper-large-v3"], "l40s")
        self.assertEqual(by_id["mock-kserve"], "cpu")

    def test_shared_runtime_reports_the_serving_class_separately(self):
        status, payload = self.post_json(
            "/v1/chat/completions",
            {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["mock_accelerator"], "all")
        self.assertEqual(payload["model_accelerator"], "b300")

        status, payload = self.post_json(
            "/v1/embeddings", {"model": "bge-m3", "input": "gateway"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["mock_accelerator"], "all")
        self.assertEqual(payload["model_accelerator"], "l40s")

    def test_model_catalog_filters_by_accelerator(self):
        status, payload = self.get("/v1/models?accelerator=h100")
        self.assertEqual(status, 200)
        self.assertEqual(
            sorted(card["id"] for card in payload["data"]),
            ["e5-mistral-7b-instruct", "qwen3-embedding-8b", "qwen3.8-27b",
             "voxtral-small-24b"],
        )

    def test_model_read_returns_capability_metadata(self):
        status, payload = self.get("/v1/models/deepseek-v4-flash")
        self.assertEqual(status, 200)
        self.assertEqual(payload["tier"], "medium")
        self.assertEqual(payload["task"], "chat")
        self.assertIn("context_window", payload)

    def test_unknown_model_is_rejected(self):
        status, payload = self.post_json(
            "/v1/chat/completions",
            {"model": "gpt-imaginary", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "model_not_found")

    def test_model_cannot_serve_another_task(self):
        status, payload = self.post_json(
            "/v1/chat/completions",
            {"model": "bge-m3", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 400)
        self.assertIn("embedding", payload["error"]["message"])

    def test_chat_reports_the_requested_tier(self):
        for model, tier, completion in (
            ("kimi-k3", "big", 48),
            ("deepseek-v4-flash", "medium", 24),
            ("qwen3.8-27b", "small", 12),
        ):
            status, payload = self.post_json(
                "/v1/chat/completions",
                {"model": model, "messages": [{"role": "user", "content": "hi there"}]},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["model"], model)
            self.assertEqual(payload["mock_tier"], tier)
            self.assertEqual(payload["usage"]["completion_tokens"], completion)
            self.assertIn(tier, payload["choices"][0]["message"]["content"])

    def test_router_decision_headers_reach_the_model(self):
        status, payload = self.post_json_with_headers(
            "/v1/chat/completions",
            {"model": "kimi-k3", "messages": [{"role": "user", "content": "prove it"}]},
            {
                "x-selected-model": "kimi-k3",
                "x-vsr-skip-processing": "false",
                "x-model-class": "b300",
            },
        )
        self.assertEqual(status, 200)
        # Only the router's own headers are reported: an echo of every header
        # would say nothing about whether the router ran.
        self.assertEqual(
            payload["mock_routing_headers"],
            {"x-selected-model": "kimi-k3", "x-vsr-skip-processing": "false"},
        )

    def test_a_request_no_router_touched_reports_no_decision(self):
        status, payload = self.post_json(
            "/v1/chat/completions",
            {"model": "mock-kserve", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["mock_routing_headers"], {})
        self.assertFalse(payload["mock_system_prompt"])

    def test_an_injected_system_prompt_is_reported(self):
        status, payload = self.post_json(
            "/v1/chat/completions",
            {
                "model": "kimi-k3",
                "messages": [
                    {"role": "system", "content": "Work step by step."},
                    {"role": "user", "content": "prove it"},
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["mock_system_prompt"])

    def test_max_tokens_beyond_the_model_limit_is_rejected(self):
        status, payload = self.post_json(
            "/v1/chat/completions",
            {
                "model": "qwen3.8-27b",
                "max_tokens": 99999,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "context_length_exceeded")

    def test_max_tokens_below_the_tier_truncates_the_completion(self):
        status, payload = self.post_json(
            "/v1/chat/completions",
            {
                "model": "kimi-k3",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["usage"]["completion_tokens"], 1)
        self.assertEqual(payload["choices"][0]["finish_reason"], "length")
        self.assertEqual(len(payload["choices"][0]["message"]["content"].split()), 1)

    def test_streaming_honors_max_tokens(self):
        status, body = self.request_raw(
            "/v1/chat/completions",
            {
                "model": "glm-5.3",
                "max_tokens": 2,
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(status, 200)
        chunks = [
            json.loads(line[len("data: ") :])
            for line in body.splitlines()
            if line.startswith("data: ") and not line.endswith("[DONE]")
        ]
        content = chunks[0]["choices"][0]
        self.assertEqual(len(content["delta"]["content"].split()), 2)
        self.assertEqual(content["finish_reason"], "length")
        self.assertEqual(chunks[-1]["usage"]["completion_tokens"], 2)

    def test_streaming_only_emits_usage_when_requested(self):
        status, body = self.request_raw(
            "/v1/chat/completions",
            {
                "model": "mock-kserve",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(status, 200)
        chunks = [
            json.loads(line[len("data: ") :])
            for line in body.splitlines()
            if line.startswith("data: ") and not line.endswith("[DONE]")
        ]
        self.assertTrue(all("usage" not in chunk for chunk in chunks))
        self.assertTrue(all(isinstance(chunk["created"], int) for chunk in chunks))

    def test_max_tokens_above_the_tier_keeps_the_whole_completion(self):
        status, payload = self.post_json(
            "/v1/chat/completions",
            {
                "model": "qwen3.8-27b",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["usage"]["completion_tokens"], 12)
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")

    def test_messages_must_be_an_array(self):
        for messages in (None, "hi", {"role": "user"}):
            status, payload = self.post_json(
                "/v1/chat/completions", {"model": "kimi-k3", "messages": messages}
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_embedding_models_use_their_own_dimensions(self):
        for model, dimensions in (
            ("qwen3-embedding-8b", 4096),
            ("bge-m3", 1024),
            ("nomic-embed-text-v2-moe", 768),
        ):
            status, payload = self.post_json(
                "/v1/embeddings", {"model": model, "input": "retrieval augmented generation"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(payload["data"][0]["embedding"]), dimensions)
            self.assertEqual(payload["mock_dimensions"], dimensions)

    def test_matryoshka_models_accept_a_smaller_dimension(self):
        status, payload = self.post_json(
            "/v1/embeddings",
            {"model": "qwen3-embedding-8b", "input": "chunk", "dimensions": 512},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"][0]["embedding"]), 512)

        status, payload = self.post_json(
            "/v1/embeddings", {"model": "bge-m3", "input": "chunk", "dimensions": 512}
        )
        self.assertEqual(status, 400)

    def test_rerank_uses_a_retrieval_reranker(self):
        status, payload = self.post_json(
            "/v1/rerank",
            {
                "model": "bge-reranker-v2-m3",
                "query": "gateway inference",
                "documents": ["unrelated", "gateway inference routing"],
                "return_documents": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "bge-reranker-v2-m3")
        self.assertEqual(payload["results"][0]["index"], 1)
        self.assertEqual(
            payload["results"][0]["document"]["text"],
            "gateway inference routing",
        )
        self.assertGreater(payload["usage"]["total_tokens"], 0)

    def test_asr_models_transcribe_without_diarization(self):
        status, payload = self.multipart(
            {"model": "voxtral-mini-3b", "response_format": "verbose_json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "voxtral-mini-3b")
        self.assertTrue(payload["segments"])
        self.assertNotIn("speaker", payload["segments"][0])
        self.assertNotIn("speakers", payload)

    def test_diarization_labels_speaker_turns(self):
        status, payload = self.multipart(
            {"model": "whisper-large-v3", "diarization": "true", "num_speakers": "3"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["diarization"])
        speakers = {segment["speaker"] for segment in payload["segments"]}
        self.assertTrue(speakers <= {"SPEAKER_00", "SPEAKER_01", "SPEAKER_02"})
        self.assertEqual({entry["id"] for entry in payload["speakers"]}, speakers)
        self.assertAlmostEqual(payload["segments"][-1]["end"], payload["duration"], places=3)

    def test_diarization_gives_every_pinned_speaker_a_turn(self):
        for requested in (2, 5, 8):
            status, payload = self.multipart(
                {
                    "model": "whisper-large-v3",
                    "diarization": "true",
                    "num_speakers": str(requested),
                }
            )
            self.assertEqual(status, 200)
            self.assertGreaterEqual(len(payload["segments"]), requested)
            self.assertEqual(len(payload["speakers"]), requested)
            self.assertEqual(
                {segment["speaker"] for segment in payload["segments"]},
                {"SPEAKER_%02d" % index for index in range(requested)},
            )

    def test_diarization_is_deterministic(self):
        first = self.multipart({"model": "voxtral-small-24b", "diarization": "yes"})
        second = self.multipart({"model": "voxtral-small-24b", "diarization": "yes"})
        self.assertEqual(first, second)

    def test_asr_only_model_rejects_diarization(self):
        status, payload = self.multipart({"model": "voxtral-mini-3b", "diarization": "true"})
        self.assertEqual(status, 400)
        self.assertIn("diarization", payload["error"]["message"])


class AcceleratorPoolTest(unittest.TestCase):
    """A replica started for one accelerator class serves only its models."""

    ACCELERATOR = "b300"

    @classmethod
    def setUpClass(cls):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        environment = dict(
            os.environ,
            ACCELERATOR=cls.ACCELERATOR,
            MODEL_NAME="kimi-k3",
            UPSTREAM_ID="pool-b300",
            PORT=str(cls.port),
        )
        cls.process = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(__file__), "server.py")],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                cls.request("/health")
                return
            except OSError:
                time.sleep(0.1)
        cls.process.terminate()
        raise AssertionError("accelerator pool replica did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=10)

    @classmethod
    def request(cls, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
        if body is None:
            connection.request("GET", path)
        else:
            connection.request(
                "POST", path, body=json.dumps(body),
                headers={"content-type": "application/json"},
            )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_health_reports_the_accelerator_class(self):
        status, payload = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["accelerator"], self.ACCELERATOR)
        self.assertEqual(payload["models"], 3)

    def test_catalog_is_limited_to_the_pool(self):
        status, payload = self.request("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(payload["mock_accelerator"], self.ACCELERATOR)
        self.assertEqual(
            sorted(card["id"] for card in payload["data"]),
            ["deepseek-v4-pro", "glm-5.3", "kimi-k3"],
        )
        self.assertTrue(
            all(card["accelerator"] == self.ACCELERATOR for card in payload["data"])
        )

    def test_pool_serves_its_own_models(self):
        status, payload = self.request(
            "/v1/chat/completions",
            {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["mock_accelerator"], self.ACCELERATOR)

    def test_model_from_another_pool_is_refused(self):
        status, payload = self.request(
            "/v1/chat/completions",
            {"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "model_not_served_here")
        self.assertIn("h100", payload["error"]["message"])

        status, payload = self.request("/v1/models/bge-m3")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "model_not_served_here")
        self.assertIn("l40s", payload["error"]["message"])

    def test_pool_without_a_task_says_so(self):
        status, payload = self.request("/v1/embeddings", {"input": "chunk"})
        self.assertEqual(status, 400)
        self.assertIn("no embedding models", payload["error"]["message"])

    def test_omitted_model_uses_a_model_this_pool_serves(self):
        status, payload = self.request(
            "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "kimi-k3")
        self.assertEqual(payload["mock_accelerator"], self.ACCELERATOR)

    def test_unknown_model_is_still_reported_as_missing(self):
        status, payload = self.request(
            "/v1/chat/completions",
            {"model": "gpt-imaginary", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "model_not_found")


if __name__ == "__main__":
    unittest.main()
