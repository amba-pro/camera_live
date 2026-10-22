"""Chat API integration tests, no hardware webcam needed."""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from camera_live import LiveHandler
from chat import ChatStore


class FakeCamera:
    def status(self):
        return {"online": False, "camera_index": 1, "error": "fake"}

    def latest(self):
        return None


class ChatHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LiveHandler)
        self.server.daemon_threads = True
        self.server.camera = FakeCamera()
        self.server.chat = ChatStore(min_interval=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request_json(self, path, body=None, headers=None):
        if body is None:
            with urlopen(self.base + path, timeout=3) as response:
                return response.status, json.loads(response.read())
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(self.base + path, payload, method="POST",
                      headers={"Content-Type": "application/json", **(headers or {})})
        with urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_empty_history(self):
        status, data = self.request_json("/api/chat")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"messages": [], "latest_id": 0})

    def test_post_and_incremental_history(self):
        status, data = self.request_json(
            "/api/chat", {"nickname": "Raynor", "text": "GG WP"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(data["message"]["id"], 1)
        self.assertTrue(data["message"]["timestamp"].endswith("+00:00"))
        self.assertEqual(data["message"]["text"], "GG WP")
        _, history = self.request_json("/api/chat?after=0")
        self.assertEqual(len(history["messages"]), 1)
        _, history = self.request_json("/api/chat?after=1")
        self.assertEqual(history["messages"], [])
        self.request_json("/api/chat", {"nickname": "Kerrigan", "text": "For the Swarm"})
        _, new = self.request_json("/api/chat?after=1")
        self.assertEqual([m["id"] for m in new["messages"]], [2])

    def test_blank_or_invalid_messages(self):
        for data in [
            {"nickname": "", "text": "ok"},
            {"nickname": "a", "text": "   "},
            {"nickname": 123, "text": "ok"},
            {"nickname": "x", "text": ["bad"]},
            {"nickname": "X" * 25, "text": "ok"},
            {"nickname": "ok", "text": "x" * 301},
        ]:
            with self.subTest(data=data), self.assertRaises(HTTPError) as result:
                self.request_json("/api/chat", data)
            self.assertEqual(result.exception.code, 400)

    def test_html_is_stored_but_never_interpreted_by_page(self):
        text = '<img src=x onerror="alert(1)">'
        _, response = self.request_json(
            "/api/chat", {"nickname": "<script>", "text": text}
        )
        self.assertEqual(response["message"]["text"], text)
        self.assertEqual(response["message"]["nickname"], "<script>")
        with urlopen(self.base + "/app.js") as response:
            js = response.read().decode("utf-8")
        self.assertIn("message.textContent = record.text", js)
        self.assertIn("nick.textContent = record.nickname", js)
        self.assertNotIn("innerHTML =", js)

    def test_max_200(self):
        for index in range(205):
            self.server.chat.post({"nickname": "Tester", "text": str(index)}, "127.0.0.1")
        _, history = self.request_json("/api/chat")
        self.assertEqual(len(history["messages"]), 200)
        self.assertEqual(history["messages"][0]["id"], 6)
        self.assertEqual(history["latest_id"], 205)

    def test_invalid_cursor(self):
        for cursor in ["-1", "abc", "1.3", ""]:
            with self.subTest(cursor=cursor), self.assertRaises(HTTPError) as result:
                self.request_json("/api/chat?after=" + cursor)
            self.assertEqual(result.exception.code, 400)

    def test_reject_non_json(self):
        req = Request(self.base + "/api/chat", b"hello", method="POST",
                      headers={"Content-Type": "text/plain"})
        with self.assertRaises(HTTPError) as result:
            urlopen(req, timeout=3)
        self.assertEqual(result.exception.code, 415)

    def test_reject_oversize_payload(self):
        req = Request(self.base + "/api/chat", b"x" * 4097, method="POST",
                      headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as result:
            urlopen(req, timeout=3)
        self.assertEqual(result.exception.code, 413)

    def test_reject_cross_origin_post(self):
        with self.assertRaises(HTTPError) as result:
            self.request_json("/api/chat", {"nickname": "a", "text": "hi"},
                              headers={"Origin": "https://evil.invalid"})
        self.assertEqual(result.exception.code, 403)

    def test_rate_limit(self):
        self.server.chat = ChatStore(min_interval=100.0)
        self.request_json("/api/chat", {"nickname": "a", "text": "first"})
        with self.assertRaises(HTTPError) as result:
            self.request_json("/api/chat", {"nickname": "a", "text": "second"})
        self.assertEqual(result.exception.code, 429)

    def test_hero_assets(self):
        for name in ("raynor", "kerrigan", "zeratul", "artanis"):
            with self.subTest(name=name):
                with urlopen(self.base + "/art/" + name + ".svg") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
                    self.assertIn(b"<svg", response.read())
        with self.assertRaises(HTTPError) as result:
            urlopen(self.base + "/art/../../camera_live.py")
        self.assertEqual(result.exception.code, 404)

    def test_page_contains_all_stream_and_chat_nodes(self):
        with urlopen(self.base + "/") as response:
            html = response.read().decode("utf-8")
        for key in (
            'id="stream"', 'id="notice"', 'id="lan"', 'id="snapshot"',
            'id="chat-form"', 'id="chat-messages"', 'id="chat-nickname"',
            'id="chat-text"', "Джим Рейнор", "Сара Керриган",
            "Зератул", "Артанис",
        ):
            self.assertIn(key, html)


if __name__ == "__main__":
    unittest.main()
