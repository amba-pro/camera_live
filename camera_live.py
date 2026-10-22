"""Windows USB camera -> MJPEG over HTTP, without cloud dependencies."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import cv2


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
STALE_AFTER_SECONDS = 3.0
DARK_FRAME_MEAN = 22.0
DARK_FRAME_STD = 6.0
COVERED_LENS_NOTICE = (
    "Кадры идут, но изображение почти чёрное. "
    "Откройте шторку камеры и проверьте, что объектив не закрыт."
)


def backend_choices(name: str) -> list[tuple[str, int]]:
    if name == "dshow":
        return [("dshow", cv2.CAP_DSHOW)]
    if name == "msmf":
        return [("msmf", cv2.CAP_MSMF)]
    if name == "any":
        return [("any", cv2.CAP_ANY)]
    if os.name == "nt":
        return [
            ("dshow", cv2.CAP_DSHOW),
            ("msmf", cv2.CAP_MSMF),
            ("any", cv2.CAP_ANY),
        ]
    return [("any", cv2.CAP_ANY)]


def enable_console_interrupt() -> None:
    """Let Ctrl+C reach Python on Windows, including a new console process group."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
    except (AttributeError, OSError):
        pass
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)


def configure_stdio() -> None:
    """Show Russian text in a UTF-8 Windows console (see start_windows.bat)."""
    if os.name != "nt":
        return
    try:
        utf8_console = ctypes.windll.kernel32.GetConsoleOutputCP() == 65001
    except (AttributeError, OSError):
        utf8_console = False
    encoding = os.environ.get("PYTHONIOENCODING", "").split(":")[0].lower()
    if not utf8_console and encoding != "utf-8":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def frame_notice(frame: object) -> str:
    """Warn when frames arrive but the lens is covered or the shutter is closed."""
    sample = frame[::16, ::16] if getattr(frame, "shape", (0,))[0] > 16 else frame
    mean = float(sample.mean())
    std = float(sample.std())
    if mean < DARK_FRAME_MEAN and std < DARK_FRAME_STD:
        return COVERED_LENS_NOTICE
    return ""


def _quiet_opencv_logs():
    logging = getattr(getattr(cv2, "utils", None), "logging", None)
    if logging is None or not hasattr(logging, "setLogLevel"):
        return None
    previous = logging.getLogLevel() if hasattr(logging, "getLogLevel") else None
    silent = getattr(logging, "LOG_LEVEL_SILENT", logging.LOG_LEVEL_ERROR)
    logging.setLogLevel(silent)
    return previous


def _restore_opencv_logs(previous) -> None:
    logging = getattr(getattr(cv2, "utils", None), "logging", None)
    if previous is not None and logging is not None:
        logging.setLogLevel(previous)


class CameraWorker:
    """One camera reader shared by all HTTP clients (not one capture per viewer)."""

    def __init__(self, index: int, width: int, height: int,
                 fps: int, jpeg_quality: int, backend: str) -> None:
        self.index = index
        self.width = width
        self.height = height
        self.fps_limit = fps
        self.jpeg_quality = jpeg_quality
        self.backend = backend
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.jpeg: bytes | None = None
        self.seq = 0
        self.frame_time = 0.0
        self.actual_width = 0
        self.actual_height = 0
        self.actual_fps = 0.0
        self.active_backend = ""
        self.connected = False
        self.error = "Открываем камеру…"
        self.notice = ""
        self._fps_counter = 0
        self._fps_start = time.monotonic()

    def start(self) -> None:
        if self.thread is not None:
            raise RuntimeError("Camera worker already started")
        self.thread = threading.Thread(target=self._run, daemon=True, name="camera-reader")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=5)

    def _set_error(self, message: str) -> None:
        with self.condition:
            self.error = message
            self.connected = False
            self.actual_fps = 0.0
            self.notice = ""
            self.condition.notify_all()

    def _open(self) -> tuple[cv2.VideoCapture | None, str, object | None]:
        for name, backend_id in backend_choices(self.backend):
            if self.stop_event.is_set():
                break
            cap = cv2.VideoCapture(self.index, backend_id)
            if not cap.isOpened():
                cap.release()
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps_limit)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # An opened device may still not return images. Try the next backend.
            first_frame = None
            for _ in range(4):
                ok, frame = cap.read()
                if ok and frame is not None:
                    first_frame = frame
                    break
                if self.stop_event.wait(0.1):
                    break
            if first_frame is not None:
                return cap, name, first_frame
            cap.release()
        return None, "", None

    def _publish(self, frame: object, backend_name: str) -> None:
        ok, encoded = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        )
        if not ok:
            self._set_error("Не удалось кодировать JPEG")
            return
        height, width = frame.shape[:2]
        now = time.monotonic()
        with self.condition:
            self.jpeg = encoded.tobytes()
            self.seq += 1
            self.frame_time = now
            self.actual_width = int(width)
            self.actual_height = int(height)
            self.active_backend = backend_name
            self.connected = True
            self.error = ""
            self.notice = frame_notice(frame)
            self._fps_counter += 1
            elapsed = now - self._fps_start
            if elapsed >= 1.0:
                self.actual_fps = round(self._fps_counter / elapsed, 1)
                self._fps_counter = 0
                self._fps_start = now
            self.condition.notify_all()

    def _run(self) -> None:
        interval = 1.0 / self.fps_limit
        while not self.stop_event.is_set():
            cap, backend_name, first_frame = self._open()
            if cap is None:
                self._set_error(
                    f"Камера {self.index} недоступна. Проверьте индекс, "
                    "разрешения Windows и другие приложения."
                )
                self.stop_event.wait(2.0)
                continue
            failures = 0
            self._fps_start = time.monotonic()
            self._fps_counter = 0
            try:
                frame = first_frame
                while not self.stop_event.is_set():
                    loop_start = time.monotonic()
                    if frame is not None:
                        self._publish(frame, backend_name)
                        failures = 0
                    else:
                        failures += 1
                        if failures >= 8:
                            self._set_error("Нет кадров от камеры. Переподключение...")
                            break
                    if self.stop_event.wait(max(0.0, interval - (time.monotonic() - loop_start))):
                        break
                    ok, new_frame = cap.read()
                    frame = new_frame if ok else None
            except Exception as exc:
                self._set_error(f"Ошибка захвата: {type(exc).__name__}: {exc}")
            finally:
                cap.release()
                self._set_error("Переподключение камеры...")
            if not self.stop_event.is_set():
                self.stop_event.wait(1.0)

    def status(self) -> dict:
        with self.condition:
            age = time.monotonic() - self.frame_time if self.frame_time else None
            online = bool(self.connected and self.jpeg and age is not None
                          and age < STALE_AFTER_SECONDS)
            return {
                "online": online,
                "camera_index": self.index,
                "width": self.actual_width if online else 0,
                "height": self.actual_height if online else 0,
                "fps": self.actual_fps if online else 0,
                "backend": self.active_backend if online else "",
                "frames": self.seq,
                "last_frame_seconds": round(age, 1) if age is not None else None,
                "error": "" if online else (self.error or "Нет новых кадров"),
                "notice": self.notice if online else "",
            }

    def latest(self) -> bytes | None:
        with self.condition:
            if (self.connected and self.jpeg and
                    time.monotonic() - self.frame_time < STALE_AFTER_SECONDS):
                return self.jpeg
            return None

    def wait_next(self, after_seq: int, timeout: float = 2.0) -> tuple[int, bytes] | None:
        with self.condition:
            self.condition.wait_for(
                lambda: self.stop_event.is_set() or
                (self.seq > after_seq and self.connected), timeout=timeout
            )
            if self.seq > after_seq and self.connected and self.jpeg:
                return self.seq, self.jpeg
            return None


class LiveHandler(BaseHTTPRequestHandler):
    server_version = "CameraLive/1.0"

    @property
    def camera(self) -> CameraWorker:
        return self.server.camera  # type: ignore[attr-defined]

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._respond(200, "text/html; charset=utf-8",
                          (WEB / "index.html").read_bytes())
        elif path == "/styles.css":
            self._respond(200, "text/css; charset=utf-8",
                          (WEB / "styles.css").read_bytes())
        elif path == "/app.js":
            self._respond(200, "text/javascript; charset=utf-8",
                          (WEB / "app.js").read_bytes())
        elif path == "/api/status":
            body = json.dumps(self.camera.status(), ensure_ascii=False).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body)
        elif path == "/snapshot.jpg":
            frame = self.camera.latest()
            if frame is None:
                self._respond(503, "text/plain; charset=utf-8",
                              "Камера недоступна".encode("utf-8"))
            else:
                self._respond(200, "image/jpeg", frame)
        elif path == "/stream.mjpg":
            self._stream()
        else:
            self._respond(404, "text/plain; charset=utf-8", b"Not found")

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        seq = 0
        try:
            while not self.camera.stop_event.is_set():
                result = self.camera.wait_next(seq)
                if result is None:
                    continue
                seq, frame = result
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                    + frame + b"\r\n"
                )
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # Browser disconnected; keep camera reader running.

    def log_message(self, format: str, *args: object) -> None:
        if not self.path.startswith("/api/status"):
            super().log_message(format, *args)


def list_cameras() -> None:
    configure_stdio()
    print("Проверка индексов камер 0–5 (закройте Zoom/Teams/Камеру Windows):", flush=True)
    found = 0
    previous_logs = _quiet_opencv_logs()
    try:
        for index in range(6):
            for backend_name, backend in backend_choices("auto"):
                cap = cv2.VideoCapture(index, backend)
                try:
                    if cap.isOpened():
                        ok, frame = cap.read()
                        if ok and frame is not None:
                            h, w = frame.shape[:2]
                            print(
                                f"  camera={index} backend={backend_name} {w}x{h}",
                                flush=True,
                            )
                            found += 1
                            break
                finally:
                    cap.release()
    finally:
        _restore_opencv_logs(previous_logs)
    if not found:
        print(
            "Ни одной камеры не найдено. Проверьте USB/драйвер/разрешения Windows.",
            flush=True,
        )


def main() -> None:
    enable_console_interrupt()
    configure_stdio()
    parser = argparse.ArgumentParser(description="USB webcam to browser (MJPEG)")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--list-cameras", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--jpeg-quality", type=int, default=75)
    parser.add_argument("--backend", choices=["auto", "dshow", "msmf", "any"], default="auto")
    args = parser.parse_args()
    if args.list_cameras:
        list_cameras()
        return
    if not (0 <= args.camera <= 50 and 1 <= args.port <= 65535 and
            1 <= args.fps <= 60 and 1 <= args.width <= 7680 and
            1 <= args.height <= 4320 and 40 <= args.jpeg_quality <= 95):
        parser.error("Проверьте camera/port/fps/width/height/jpeg-quality.")
    camera = CameraWorker(
        args.camera, args.width, args.height, args.fps, args.jpeg_quality, args.backend
    )
    server = ThreadingHTTPServer((args.host, args.port), LiveHandler)
    server.daemon_threads = True
    server.camera = camera  # type: ignore[attr-defined]
    camera.start()
    print(f"Camera Live: http://127.0.0.1:{args.port}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("ВНИМАНИЕ: сервер без пароля/HTTPS. Только доверенная LAN или VPN!")
        print(f"Открыть в сети: http://<IP-НОУТБУКА>:{args.port}")
    print("Остановка: Ctrl+C")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        server.shutdown()
        server.server_close()
        camera.stop()


if __name__ == "__main__":
    main()
