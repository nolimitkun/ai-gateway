import http.client
import json
import threading
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

    def post_json(self, path, payload):
        return self.request(path, json.dumps(payload))

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
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "bge-reranker-v2-m3")
        self.assertEqual(payload["results"][0]["index"], 1)

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

    def test_diarization_is_deterministic(self):
        first = self.multipart({"model": "voxtral-small-24b", "diarization": "yes"})
        second = self.multipart({"model": "voxtral-small-24b", "diarization": "yes"})
        self.assertEqual(first, second)

    def test_asr_only_model_rejects_diarization(self):
        status, payload = self.multipart({"model": "voxtral-mini-3b", "diarization": "true"})
        self.assertEqual(status, 400)
        self.assertIn("diarization", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
