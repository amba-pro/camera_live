"""HTTP and state tests for shared video overlay reactions and annotations."""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from camera_live import LiveHandler
from chat import ChatStore
from overlays import OverlayStore, OverlayError


def stroke(client_id="pen1", color="#6be7ff", width=6, points=None):
    return {
        "type": "stroke", "client_id": client_id,
        "color": color, "width": width,
        "points": [[0.1, 0.3], [0.5, 0.7]] if points is None else points,
    }


def reaction(client_id="r1", emoji="🔥", x=0.5, y=0.65):
    return {"type": "reaction", "client_id": client_id, "emoji": emoji, "x": x, "y": y}


class FakeCamera:
    def status(self):
        return {"online": False, "camera_index": 1, "error": "camera not needed"}

    def latest(self):
        return None


class OverlaysHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LiveHandler)
        self.server.daemon_threads = True
        self.server.camera = FakeCamera()
        self.server.chat = ChatStore()
        self.server.overlays = OverlayStore()
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def request(self, path, payload=None, headers=None):
        if payload is None:
            with urlopen(self.base + path, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        request = Request(
            self.base + path, json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_two_viewers_receive_same_reaction_and_pen_stroke(self):
        _, initial = self.request("/api/overlays")
        self.assertTrue(initial["reset"])
        self.assertEqual(initial["events"], [])
        first_session = initial["session"]
        self.assertTrue(first_session)
        status, added = self.request("/api/overlays", stroke())
        self.assertEqual(status, 201)
        self.assertEqual(added["event"]["id"], 1)
        _, laptop = self.request("/api/overlays?after=0")
        _, phone = self.request("/api/overlays?after=0")
        self.assertEqual(phone["events"], laptop["events"])
        self.assertEqual(laptop["session"], first_session)
        self.assertEqual(laptop["events"][0]["points"], [[0.1, 0.3], [0.5, 0.7]])

        self.request("/api/overlays", reaction())
        _, updates = self.request("/api/overlays?after=1")
        self.assertEqual(len(updates["events"]), 1)
        self.assertEqual(updates["events"][0]["emoji"], "🔥")
        self.assertEqual(updates["latest_id"], 2)
        self.assertFalse(updates["reset"])
        self.assertTrue(isinstance(updates["server_time"], float))

    def test_clear_everyone_and_reset_cursor(self):
        self.request("/api/overlays", stroke())
        _, response = self.request("/api/overlays", {"type": "clear", "client_id": "clear1"})
        self.assertEqual(response["event"]["type"], "clear")
        _, state = self.request("/api/overlays?after=1")
        self.assertTrue(state["reset"])
        self.assertEqual([event["type"] for event in state["events"]], ["clear"])

    def test_reactions_are_whitelisted(self):
        for emoji in ("🔥", "❤️", "😂", "👏", "⚡", "👾"):
            self.server.overlays.post(reaction(client_id="id" + str(ord(emoji[0])), emoji=emoji), "192.168.1.2" + str(len(emoji)))
        for bad in ("<script>", "😈", "😺", 4, [], None):
            with self.subTest(bad=bad), self.assertRaises(HTTPError) as error:
                self.request("/api/overlays", reaction(emoji=bad))
            self.assertEqual(error.exception.code, 400)

    def test_validation_rejects_nonfinite_wrong_shape_and_unhashable(self):
        invalid = [
            stroke(points=[]), stroke(points=[[0.5]]),
            stroke(points=[[0.1, 0.2]] * 33),
            stroke(points=[[2, 0.5]]),
            stroke(points=[[True, 0.5]]),
            stroke(points=[["0.2", 0.1]]),
            stroke(points=[[0.5, None]]),
            stroke(color=[]), stroke(width={"a": 1}),
            stroke(color="#000000"), stroke(width=2),
            stroke(client_id="bad id"), stroke(client_id=""),
            {"type": "stroke", "client_id": "abc"},
            reaction(x=-0.1), reaction(y=3), reaction(x=None),
            reaction(client_id="bad space"),
            {"type": "clear", "client_id": "ok", "extra": "x"},
            {"type": "omg", "client_id": "test"},
        ]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(HTTPError) as error:
                self.request("/api/overlays", item)
            self.assertEqual(error.exception.code, 400)

    def test_cursor_validation(self):
        for cursor in ("-1", "NaN", "1.5", "", "999999999999999999999999"):
            with self.subTest(cursor=cursor), self.assertRaises(HTTPError) as error:
                self.request("/api/overlays?after=" + cursor)
            self.assertEqual(error.exception.code, 400)

    def test_rate_limiting(self):
        for i in range(6):
            self.request("/api/overlays", reaction(client_id=f"react_{i}"))
        with self.assertRaises(HTTPError) as error:
            self.request("/api/overlays", reaction(client_id="react_7"))
        self.assertEqual(error.exception.code, 429)

    def test_oversized_and_cross_origin_requests(self):
        req = Request(
            self.base + "/api/overlays", b"x" * 4097, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(req, timeout=3)
        self.assertEqual(error.exception.code, 413)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/overlays", reaction(), headers={"Origin": "https://other.invalid"})
        self.assertEqual(error.exception.code, 403)

    def test_static_assets_and_player_controls(self):
        with urlopen(self.base + "/") as response:
            html = response.read().decode("utf-8")
        for needle in (
            'src="/overlays.js"', 'id="draw-layer"', 'id="reaction-layer"',
            'id="draw-toggle"', 'id="draw-clear"', 'id="draw-color"',
            'id="draw-width"', 'data-emoji="🔥"', 'id="stream"',
            'id="chat-form"', 'id="snapshot"',
        ):
            self.assertIn(needle, html)
        with urlopen(self.base + "/overlays.js") as response:
            js = response.read().decode("utf-8")
        self.assertIn("setPointerCapture", js)
        self.assertIn("pointercancel", js)
        self.assertIn("/api/overlays", js)
        self.assertNotIn("innerHTML =", js)
        with urlopen(self.base + "/styles.css") as response:
            css = response.read().decode("utf-8")
        self.assertIn(".draw-layer.is-drawing", css)
        self.assertIn(".reaction-layer", css)


class OverlayStoreUnitTests(unittest.TestCase):
    def test_history_expiration_and_server_reconnect(self):
        t = [20.0]
        store = OverlayStore(clock=lambda: t[0], monotonic=lambda: t[0])
        one = store.post(stroke(), "host")
        self.assertEqual(one["id"], 1)
        t[0] += 121
        state = store.history(after=1)
        self.assertTrue(state["reset"])
        self.assertEqual(state["events"], [])
        self.assertEqual(state["latest_id"], 1)
        self.assertEqual(store.history(after=999)["reset"], True)
        self.assertNotEqual(store.session, OverlayStore().session)

    def test_normalized_coordinates_and_safe_payload(self):
        store = OverlayStore()
        event = store.post(stroke(points=[[0.123456789, 0.5]]), "x")
        self.assertEqual(event["points"], [[0.12346, 0.5]])
        self.assertNotIn("html", event)
        self.assertLessEqual(len(store.history()["events"]), 800)

    def test_clear_limit(self):
        store = OverlayStore()
        store.post({"type": "clear", "client_id": "clear1"}, "same")
        with self.assertRaises(OverlayError) as err:
            store.post({"type": "clear", "client_id": "clear2"}, "same")
        self.assertEqual(err.exception.status, 429)


if __name__ == "__main__":
    unittest.main()
