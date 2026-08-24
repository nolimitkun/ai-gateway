import http.client
import json
import threading
import unittest

from server import Handler, ThreadingHTTPServer, deterministic_embedding


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


if __name__ == "__main__":
    unittest.main()
