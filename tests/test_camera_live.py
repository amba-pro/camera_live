"""HTTP smoke tests: no physical webcam required."""
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

import cv2
import numpy as np

from camera_live import (
    CameraWorker, LiveHandler, backend_choices, frame_notice, is_phone_lan_ip,
    phone_lan_urls, select_camera_index,
)


class FakeCamera:
    def __init__(self, online=True):
        self.online = online
        self.stop_event = threading.Event()

    def status(self):
        return {
            "online": self.online, "camera_index": 0, "camera_name": "Fake", "width": 640,
            "height": 480, "fps": 12.5, "backend": "fake", "frames": 10,
            "last_frame_seconds": 0.1, "error": "" if self.online else "offline",
            "notice": "",
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
        # One part is shorter than a fixed read, and the socket stays open.
        # BufferedReader.read(n) would wait forever for the missing bytes.
        with urlopen(self.base + "/stream.mjpg", timeout=4) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("multipart/x-mixed-replace", response.headers["Content-Type"])
            data = b""
            while b"\xff\xd9" not in data:
                chunk = response.read(1)
                self.assertTrue(chunk)
                data += chunk
                self.assertLess(len(data), 4096)
            self.assertIn(b"--frame", data)
            self.assertIn(b"image/jpeg", data)
            self.assertIn(b"\xff\xd8test-jpeg\xff\xd9", data)

    def test_not_found(self):
        with self.assertRaises(HTTPError) as context:
            urlopen(self.base + "/unknown")
        self.assertEqual(context.exception.code, 404)

    def test_backends(self):
        self.assertEqual(backend_choices("any")[0][0], "any")


class CameraSelectionTests(unittest.TestCase):
    def test_prefers_logitech_over_laptop_camera(self):
        names = ["USB2.0 HD UVC WebCam", "HD Pro Webcam C920", "Meta Quest 3"]
        self.assertEqual(select_camera_index(names, None, ""), (1, "HD Pro Webcam C920"))

    def test_explicit_index_wins(self):
        names = ["USB2.0 HD UVC WebCam", "HD Pro Webcam C920"]
        self.assertEqual(select_camera_index(names, 0, ""), (0, "USB2.0 HD UVC WebCam"))

    def test_camera_name_query(self):
        names = ["USB2.0 HD UVC WebCam", "HD Pro Webcam C920"]
        self.assertEqual(select_camera_index(names, 0, "c920"), (1, "HD Pro Webcam C920"))

    def test_missing_name_raises(self):
        with self.assertRaises(ValueError):
            select_camera_index(["USB2.0 HD UVC WebCam"], None, "C920")

    def test_phone_lan_addresses(self):
        self.assertTrue(is_phone_lan_ip("192.168.50.7"))
        self.assertTrue(is_phone_lan_ip("192.168.186.166"))
        self.assertFalse(is_phone_lan_ip("127.0.0.1"))
        self.assertFalse(is_phone_lan_ip("100.110.93.103"))
        self.assertFalse(is_phone_lan_ip("192.168.56.1"))
        self.assertFalse(is_phone_lan_ip("192.168.137.1"))

    def test_phone_urls_prefer_home_lan(self):
        import unittest.mock
        infos = [
            (None, None, None, None, ("10.110.100.2", 0)),
            (None, None, None, None, ("192.168.50.7", 0)),
            (None, None, None, None, ("192.168.186.166", 0)),
        ]
        with unittest.mock.patch("camera_live.socket.getaddrinfo", return_value=infos):
            self.assertEqual(
                phone_lan_urls(8000),
                ["http://192.168.50.7:8000", "http://192.168.186.166:8000"],
            )


class FrameNoticeTests(unittest.TestCase):
    def test_dark_flat_frame_warns_about_cover(self):
        dark = np.full((48, 64, 3), 12, np.uint8)
        self.assertIn("шторку", frame_notice(dark))

    def test_normal_contrast_frame_has_no_notice(self):
        frame = np.zeros((48, 64, 3), np.uint8)
        frame[:, :32] = 30
        frame[:, 32:] = 210
        self.assertEqual(frame_notice(frame), "")


class ScriptedCapture:
    plan = []

    def __init__(self, index, backend):
        self.reads = list(ScriptedCapture.plan.pop(0))

    def isOpened(self):
        return True

    def set(self, *args):
        return True

    def read(self):
        item = self.reads.pop(0)
        if item is None:
            return False, None
        return True, item

    def release(self):
        return None


class CameraWorkerRecoveryTests(unittest.TestCase):
    def test_reconnects_after_read_failures_and_reports_dark_frame(self):
        good = np.full((32, 32, 3), 180, np.uint8)
        dark = np.full((32, 32, 3), 10, np.uint8)
        ScriptedCapture.plan = [
            [good, good, good, good, None, None, None, None, None, None, None, None],
            [dark] * 20,
        ]
        original = cv2.VideoCapture
        cv2.VideoCapture = ScriptedCapture
        worker = CameraWorker(0, 32, 32, 5, 75, "any")
        try:
            worker.start()
            saw_good = False
            saw_notice = False
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not (saw_good and saw_notice):
                status = worker.status()
                if status["online"] and status["width"] == 32 and not status["notice"]:
                    saw_good = True
                if status["online"] and "шторку" in status["notice"]:
                    saw_notice = True
                time.sleep(0.05)
            self.assertTrue(saw_good)
            self.assertTrue(saw_notice)
            self.assertGreater(worker.status()["frames"], 1)
        finally:
            worker.stop()
            cv2.VideoCapture = original


if __name__ == "__main__":
    unittest.main()
