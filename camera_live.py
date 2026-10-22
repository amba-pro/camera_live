"""Windows USB camera -> MJPEG over HTTP, without cloud dependencies."""
from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import cv2

from chat import ChatError, ChatStore


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


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _VariantValue(ctypes.Union):
    _fields_ = [("bstr", ctypes.c_void_p), ("ll", ctypes.c_ulonglong)]


class _VARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("r1", ctypes.c_ushort),
        ("r2", ctypes.c_ushort),
        ("r3", ctypes.c_ushort),
        ("value", _VariantValue),
        ("pad", ctypes.c_void_p),
    ]


def _guid(text: str) -> _GUID:
    part1, part2, part3, part4, part5 = text.split("-")
    value = _GUID()
    value.Data1 = int(part1, 16)
    value.Data2 = int(part2, 16)
    value.Data3 = int(part3, 16)
    value.Data4[:] = bytes.fromhex(part4 + part5)
    return value


def _com_method(ptr: ctypes.c_void_p, index: int, restype, argtypes):
    table = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))
    functions = ctypes.cast(table[0], ctypes.POINTER(ctypes.c_void_p))
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(functions[index])


def directshow_device_names() -> list[str]:
    """Friendly names in the same order as OpenCV CAP_DSHOW indexes."""
    if os.name != "nt":
        return []
    ole32 = ctypes.WinDLL("ole32")
    oleaut = ctypes.WinDLL("oleaut32")
    oleaut.SysFreeString.argtypes = [ctypes.c_void_p]
    oleaut.SysFreeString.restype = None
    if ole32.CoInitialize(None) < 0:
        return []
    device = ctypes.c_void_p()
    enumerator = ctypes.c_void_p()
    names: list[str] = []
    try:
        created = ole32.CoCreateInstance(
            ctypes.byref(_guid("62BE5D10-60EB-11d0-BD3B-00A0C911CE86")),
            None,
            1,
            ctypes.byref(_guid("29840822-5B84-11D0-BD3B-00A0C911CE86")),
            ctypes.byref(device),
        )
        if created != 0 or not device:
            return []
        listed = _com_method(
            device, 3, ctypes.HRESULT, [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong]
        )(device, ctypes.byref(_guid("860BB310-5D01-11d0-BD3B-00A0C911CE86")), ctypes.byref(enumerator), 0)
        if listed != 0 or not enumerator:
            return []
        while True:
            moniker = ctypes.c_void_p()
            fetched = ctypes.c_ulong()
            next_hr = _com_method(
                enumerator, 3, ctypes.HRESULT,
                [ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_ulong)],
            )(enumerator, 1, ctypes.byref(moniker), ctypes.byref(fetched))
            if next_hr != 0 or not moniker:
                break
            bag = ctypes.c_void_p()
            variant = _VARIANT()
            try:
                bound = _com_method(
                    moniker, 9, ctypes.HRESULT,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                )(moniker, None, None, ctypes.byref(_guid("55272A00-42CB-11CE-8135-00AA004BB851")), ctypes.byref(bag))
                if bound != 0 or not bag:
                    names.append("")
                    continue
                read = _com_method(
                    bag, 3, ctypes.HRESULT, [ctypes.c_wchar_p, ctypes.POINTER(_VARIANT), ctypes.c_void_p]
                )(bag, "FriendlyName", ctypes.byref(variant), None)
                if read == 0 and variant.value.bstr:
                    names.append(ctypes.wstring_at(variant.value.bstr))
                else:
                    names.append("")
            finally:
                if variant.vt == 8 and variant.value.bstr:
                    oleaut.SysFreeString(variant.value.bstr)
                if bag:
                    _com_method(bag, 2, ctypes.c_ulong, [])(bag)
                _com_method(moniker, 2, ctypes.c_ulong, [])(moniker)
    except (OSError, ctypes.ArgumentError):
        return names
    return names


VIRTUAL_ADAPTER_MARKERS = (
    "tap-windows", "tailscale", "virtualbox", "hyper-v", "vethernet", "wsl",
    "bluetooth", "wireguard", "openvpn", "tunnel", "loopback", "vpn",
    "nordlynx", "hamachi", "zerotier", "radmin", "isatap", "teredo",
    "wan miniport", "outline", "pseudo", "virtual",
)


def adapter_is_virtual(description: str) -> bool:
    text = description.casefold()
    return any(marker in text for marker in VIRTUAL_ADAPTER_MARKERS)


def virtual_adapter_ips() -> set[str]:
    """IPv4 on VPN, TAP and other non-hardware adapters. A phone cannot open those."""
    if os.name != "nt":
        return set()
    try:
        return _windows_virtual_adapter_ips()
    except (OSError, AttributeError):
        return set()


def _windows_virtual_adapter_ips() -> set[str]:
    class SockAddr(ctypes.Structure):
        _fields_ = [("family", ctypes.c_ushort), ("data", ctypes.c_ubyte * 14)]

    class SocketAddress(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.POINTER(SockAddr)), ("length", ctypes.c_int)]

    class Unicast(ctypes.Structure):
        pass

    Unicast._fields_ = [
        ("length", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("next", ctypes.POINTER(Unicast)),
        ("address", SocketAddress),
    ]

    class Addresses(ctypes.Structure):
        pass

    Addresses._fields_ = [
        ("length", ctypes.c_ulong),
        ("ifindex", ctypes.c_ulong),
        ("next", ctypes.POINTER(Addresses)),
        ("adapter_name", ctypes.c_char_p),
        ("first_unicast", ctypes.POINTER(Unicast)),
        ("first_anycast", ctypes.c_void_p),
        ("first_multicast", ctypes.c_void_p),
        ("first_dns", ctypes.c_void_p),
        ("dns_suffix", ctypes.c_wchar_p),
        ("description", ctypes.c_wchar_p),
        ("friendly", ctypes.c_wchar_p),
    ]

    iphlpapi = ctypes.WinDLL("iphlpapi")
    get_adapters = iphlpapi.GetAdaptersAddresses
    get_adapters.argtypes = [
        ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
    ]
    get_adapters.restype = ctypes.c_ulong
    size = ctypes.c_ulong(16 * 1024)
    buf = None
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        result = get_adapters(
            2, 0x2 | 0x4 | 0x8, None, ctypes.cast(buf, ctypes.POINTER(Addresses)), ctypes.byref(size)
        )
        if result == 0:
            break
        if result != 111:
            return set()
    else:
        return set()
    skipped: set[str] = set()
    current = ctypes.cast(buf, ctypes.POINTER(Addresses))
    while current:
        adapter = current.contents
        label = f"{adapter.description or ''} {adapter.friendly or ''}"
        if adapter_is_virtual(label):
            unicast = adapter.first_unicast
            while unicast:
                sockaddr = unicast.contents.address.lpSockaddr
                if sockaddr and sockaddr.contents.family == 2:
                    raw = bytes(sockaddr.contents.data[2:6])
                    skipped.add(".".join(str(part) for part in raw))
                unicast = unicast.contents.next
        current = adapter.next
    return skipped


def wifi_ssid() -> str:
    """Name of the connected Wi-Fi, so the phone joins that network and not mobile data."""
    cached = getattr(wifi_ssid, "_cache", None)
    now = time.monotonic()
    if cached and now - cached[0] < 15:
        return cached[1]
    ssid = ""
    if os.name == "nt":
        try:
            output = subprocess.check_output(
                ["netsh", "wlan", "show", "interfaces"],
                text=True, errors="replace", timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            output = ""
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("ssid") and "bssid" not in stripped.lower():
                ssid = stripped.split(":", 1)[1].strip()
                break
    wifi_ssid._cache = (now, ssid)  # type: ignore[attr-defined]
    return ssid


def is_phone_lan_ip(ip: str) -> bool:
    """Home-LAN addresses a phone can open. Skips Tailscale, VirtualBox and the Windows hotspot."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if not address.is_private or address.is_loopback or address.is_link_local:
        return False
    if address in ipaddress.ip_network("100.64.0.0/10"):
        return False
    if address in ipaddress.ip_network("192.168.56.0/24"):
        return False
    if address in ipaddress.ip_network("192.168.137.0/24"):
        return False
    return True


def phone_lan_urls(port: int) -> list[str]:
    urls: list[str] = []
    skip = virtual_adapter_ips()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return urls
    for info in infos:
        ip = info[4][0]
        url = f"http://{ip}:{port}"
        if ip not in skip and is_phone_lan_ip(ip) and url not in urls:
            urls.append(url)
    home = [url for url in urls if "://192.168." in url or "://172." in url]
    return home or urls


def select_camera_index(names: list[str], explicit: int | None, name_query: str) -> tuple[int, str]:
    """Pick an explicit index, a name query, or a connected Logitech before the laptop camera."""
    if name_query:
        query = name_query.casefold()
        for index, name in enumerate(names):
            if query in name.casefold():
                return index, name
        known = ", ".join(f"{index}: {name}" for index, name in enumerate(names)) or "нет устройств"
        raise ValueError(f"Камера '{name_query}' не найдена. {known}")
    if explicit is not None:
        name = names[explicit] if 0 <= explicit < len(names) else ""
        return explicit, name
    for hint in ("c920", "logitech"):
        for index, name in enumerate(names):
            if hint in name.casefold():
                return index, name
    return 0, names[0] if names else ""


class CameraWorker:
    """One camera reader shared by all HTTP clients (not one capture per viewer)."""

    def __init__(self, index: int, width: int, height: int,
                 fps: int, jpeg_quality: int, backend: str, camera_name: str = "") -> None:
        self.index = index
        self.camera_name = camera_name
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
                "camera_name": self.camera_name,
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

    @property
    def chat(self) -> ChatStore:
        return self.server.chat  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._respond(status, "application/json; charset=utf-8", body)

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
        elif path in (
            "/art/raynor.svg", "/art/kerrigan.svg",
            "/art/zeratul.svg", "/art/artanis.svg",
        ):
            art_name = path.rsplit("/", 1)[1]
            self._respond(200, "image/svg+xml; charset=utf-8",
                          (WEB / "art" / art_name).read_bytes())
        elif path == "/api/chat":
            try:
                values = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                cursors = values.get("after", ["0"])
                if len(cursors) != 1 or not cursors[0].isdigit():
                    raise ChatError("Неверный параметр after.")
                self._json(200, self.chat.history(int(cursors[0])))
            except ChatError as exc:
                self._json(exc.status, {"error": str(exc)})
        elif path == "/api/status":
            payload = self.camera.status()
            host, port = self.server.server_address[:2]
            on_lan = host not in ("127.0.0.1", "localhost", "::1")
            payload["lan_urls"] = phone_lan_urls(port) if on_lan else []
            payload["lan_network"] = wifi_ssid() if on_lan else ""
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/chat":
            self._json(404, {"error": "Not found"})
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != self.headers.get("Host", "").lower():
            self._json(403, {"error": "Недопустимый Origin."})
            return
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._json(415, {"error": "Используйте application/json."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, {"error": "Пустой или неверный JSON."})
            return
        if length > 4096:
            self._json(413, {"error": "Сообщение слишком большое."})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "Неверный JSON."})
            return
        try:
            record = self.chat.post(payload, self.client_address[0])
        except ChatError as exc:
            self._json(exc.status, {"error": str(exc)})
        else:
            self._json(201, {"message": record})

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
    names = directshow_device_names()
    print("Камеры DirectShow (закройте Zoom/Teams/Камеру Windows):", flush=True)
    for index, name in enumerate(names):
        print(f"  [{index}] {name}", flush=True)
    found = 0
    previous_logs = _quiet_opencv_logs()
    try:
        count = max(6, len(names))
        for index in range(count):
            label = names[index] if index < len(names) else ""
            for backend_name, backend in backend_choices("auto"):
                cap = cv2.VideoCapture(index, backend)
                try:
                    if not cap.isOpened():
                        continue
                    frame = None
                    for _ in range(4):
                        ok, candidate = cap.read()
                        if ok and candidate is not None:
                            frame = candidate
                            break
                    if frame is not None:
                        height, width = frame.shape[:2]
                        print(
                            f"  открыта camera={index} name={label!r} "
                            f"backend={backend_name} {width}x{height}",
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
            "Ни одной камеры не открылось. Проверьте USB/драйвер/разрешения Windows.",
            flush=True,
        )
        return
    index, name = select_camera_index(names, None, "")
    print(f"Автовыбор без --camera: {index} {name}", flush=True)


def main() -> None:
    enable_console_interrupt()
    configure_stdio()
    parser = argparse.ArgumentParser(description="USB webcam to browser (MJPEG)")
    parser.add_argument("--camera", type=int, default=None,
                        help="OpenCV index. По умолчанию Logitech/C920, если она подключена, иначе 0.")
    parser.add_argument("--camera-name", default="",
                        help="Часть имени DirectShow, например C920. Важнее индекса.")
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
    try:
        camera_index, camera_name = select_camera_index(
            directshow_device_names(), args.camera, args.camera_name
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not (0 <= camera_index <= 50 and 1 <= args.port <= 65535 and
            1 <= args.fps <= 60 and 1 <= args.width <= 7680 and
            1 <= args.height <= 4320 and 40 <= args.jpeg_quality <= 95):
        parser.error("Проверьте camera/port/fps/width/height/jpeg-quality.")
    print(f"Камера: {camera_index} {camera_name}".rstrip(), flush=True)
    camera = CameraWorker(
        camera_index, args.width, args.height, args.fps, args.jpeg_quality,
        args.backend, camera_name,
    )
    server = ThreadingHTTPServer((args.host, args.port), LiveHandler)
    server.daemon_threads = True
    server.camera = camera  # type: ignore[attr-defined]
    server.chat = ChatStore()  # type: ignore[attr-defined]
    camera.start()
    print(f"Camera Live: http://127.0.0.1:{args.port}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("ВНИМАНИЕ: сервер без пароля и HTTPS. Только доверенная локальная сеть.")
        ssid = wifi_ssid()
        if ssid:
            print(f"Телефон должен быть в Wi-Fi «{ssid}», не в мобильном интернете.", flush=True)
        for url in phone_lan_urls(args.port):
            print(f"С телефона: {url}", flush=True)
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
