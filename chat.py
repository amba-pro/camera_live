"""Small thread-safe, in-memory LAN chat for the StarCraft stream page.

No external services, database, cookies or persistent storage.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import threading
import time
import unicodedata


class ChatError(ValueError):
    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.status = status


def clean_text(value: object, maximum: int, field: str) -> str:
    if not isinstance(value, str):
        raise ChatError(f"Поле {field} должно быть строкой.")
    # Filter format controls (including bidi overrides), tabs, C0/C1 controls;
    # collapse whitespace. Rendering also uses textContent, never innerHTML.
    sanitized = "".join(
        char for char in value
        if unicodedata.category(char) not in ("Cc", "Cf", "Cs")
        or char in "\t\n\r"
    )
    sanitized = " ".join(sanitized.split())
    if not sanitized:
        raise ChatError(f"Введите {field}.")
    if len(sanitized) > maximum:
        raise ChatError(f"Слишком длинное поле {field} (максимум {maximum} символов).")
    return sanitized


class ChatStore:
    """Latest 200 messages; shared by HTTP request handler threads."""

    def __init__(self, capacity: int = 200, min_interval: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._messages: deque[dict] = deque(maxlen=capacity)
        self._next_id = 1
        self._last_post: dict[str, float] = {}
        self._min_interval = min_interval

    def history(self, after: int = 0) -> dict:
        if not isinstance(after, int) or not 0 <= after <= 2**53 - 1:
            raise ChatError("Неверный параметр after.")
        with self._lock:
            records = [message.copy() for message in self._messages if message["id"] > after]
            return {"messages": records, "latest_id": self._next_id - 1}

    def post(self, body: object, client_ip: str) -> dict:
        if not isinstance(body, dict):
            raise ChatError("Тело запроса должно быть JSON-объектом.")
        name = clean_text(body.get("nickname"), 24, "ник")
        text = clean_text(body.get("text"), 300, "сообщение")
        now = time.monotonic()
        with self._lock:
            previous = self._last_post.get(client_ip)
            if previous is not None and now - previous < self._min_interval:
                raise ChatError("Подождите секунду перед следующим сообщением.", 429)
            message = {
                "id": self._next_id,
                "nickname": name,
                "text": text,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._next_id += 1
            self._messages.append(message)
            self._last_post[client_ip] = now
            if len(self._last_post) > 1024:
                cutoff = now - 60
                self._last_post = {
                    ip: ts for ip, ts in self._last_post.items() if ts >= cutoff
                }
            return message.copy()
