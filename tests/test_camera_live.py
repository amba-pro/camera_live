"""HTTP smoke tests: no physical webcam required."""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

from camera_live import LiveHandler, backend_choices


class FakeCamera:
    def __init__(self, online=True):
        self.online = online
        self.stop_event = threading.Event()

    def status(self):
        return {
            "online": self.online, "camera_index": 0, "width": 640,
            "height": 480, "fps": 12.5, "backend": "fake", "frames": 10,
            "last_frame_seconds": 0.1, "error": "" if self.online else "offline",
        }

    def latest(self):
        return b"\xff\xd8test-jpeg\xff\xd9" if self.online else None

    def wait_next(self, after_seq, timeout=2.0):
        if self.online and after_seq < 1:
            return 1, self.latest()
        self.stop_event.wait(0.01)
        return None


class CameraLiveHttpTests(unittest.TestCase):
    def setUp(self):
        self.fake_camera = FakeCamera()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LiveHandler)
        self.server.daemon_threads = True
        self.server.camera = self.fake_camera
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.fake_camera.stop_event.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_index(self):
        with urlopen(self.base + "/") as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"stream.mjpg", response.read())

    def test_static_assets(self):
        for path in ["/app.js", "/styles.css"]:
            with urlopen(self.base + path) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 10)

    def test_status(self):
        with urlopen(self.base + "/api/status") as response:
            payload = json.loads(response.read())
            self.assertTrue(payload["online"])
            self.assertEqual(payload["width"], 640)

    def test_snapshot(self):
        with urlopen(self.base + "/snapshot.jpg") as response:
            self.assertEqual(response.headers.get_content_type(), "image/jpeg")
            self.assertTrue(response.read().startswith(b"\xff\xd8"))

    def test_snapshot_unavailable(self):
        self.fake_camera.online = False
        with self.assertRaises(HTTPError) as context:
            urlopen(self.base + "/snapshot.jpg")
        self.assertEqual(context.exception.code, 503)

    def test_stream_multipart_frame(self):
        with urlopen(self.base + "/stream.mjpg", timeout=4) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("multipart/x-mixed-replace", response.headers["Content-Type"])
            data = response.read(74)
            self.assertIn(b"image/jpeg", data)
            self.assertIn(b"--frame", data)

    def test_not_found(self):
        with self.assertRaises(HTTPError) as context:
            urlopen(self.base + "/unknown")
        self.assertEqual(context.exception.code, 404)

    def test_backends(self):
        self.assertEqual(backend_choices("any")[0][0], "any")


if __name__ == "__main__":
    unittest.main()
