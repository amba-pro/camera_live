"""Shared ephemeral video overlays for a trusted LAN.

No image/frame mutation. Clients paint reactions and strokes over their local
video player after polling this thread-safe in-memory event log.
"""
from __future__ import annotations

from collections import deque
import math
import re
import secrets
import threading
import time

REACTIONS = frozenset(("🔥", "❤️", "😂", "👏", "⚡", "👾"))
COLORS = frozenset(("#6be7ff", "#ffda70", "#ff5d8f", "#9effa0", "#ffffff"))
WIDTHS = frozenset((3, 6, 10))
STROKE_TTL_SECONDS = 120.0
REACTION_TTL_SECONDS = 5.0
EVENT_CAPACITY = 800
_CLIENT_ID = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")


class OverlayError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _coordinate(number: object) -> float:
    # Reject bool, NaN, Infinity, strings, and out-of-bounds values.
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        raise OverlayError("Координаты должны быть числами от 0 до 1.")
    x = float(number)
    if not math.isfinite(x) or not 0.0 <= x <= 1.0:
        raise OverlayError("Координаты должны быть числами от 0 до 1.")
    return round(x, 5)


class OverlayStore:
    """Memory-capped and time-capped shared overlay events."""

    def __init__(self, clock=None, monotonic=None) -> None:
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self.session = secrets.token_hex(9)
        self._events: deque[dict] = deque()
        self._latest_id = 0
        self._dropped_through = 0
        self._rates: dict[tuple[str, str], deque[float]] = {}

    def _prune(self, now: float) -> None:
        while self._events:
            event = self._events[0]
            ttl = REACTION_TTL_SECONDS if event["type"] == "reaction" else STROKE_TTL_SECONDS
            if now - event["time"] <= ttl and len(self._events) <= EVENT_CAPACITY:
                break
            removed = self._events.popleft()
            self._dropped_through = max(self._dropped_through, removed["id"])

    def history(self, after: int = 0) -> dict:
        if isinstance(after, bool) or not isinstance(after, int) or not 0 <= after <= 2**53 - 1:
            raise OverlayError("Некорректный курсор событий.")
        with self._lock:
            self._prune(self._clock())
            reset = after == 0 or after <= self._dropped_through or after > self._latest_id
            # Empty logs may have evicted records; returning reset clears old client drawings.
            return {
                "session": self.session,
                "latest_id": self._latest_id,
                "reset": reset,
                "events": [e.copy() for e in self._events if reset or e["id"] > after],
                "server_time": self._clock(),
                "stroke_ttl": STROKE_TTL_SECONDS,
                "reaction_ttl": REACTION_TTL_SECONDS,
            }

    def _rate(self, ip: str, kind: str, now: float) -> None:
        # Limits are intentionally generous for continuous pen movement.
        window, maximum = {
            "stroke": (1.0, 24),
            "reaction": (3.0, 6),
            "clear": (10.0, 1),
        }[kind]
        key = (ip, kind)
        entries = self._rates.setdefault(key, deque())
        while entries and entries[0] <= now - window:
            entries.popleft()
        if len(entries) >= maximum:
            raise OverlayError("Слишком часто. Попробуйте чуть позже.", 429)
        entries.append(now)
        if len(self._rates) > 1024:
            for key, values in list(self._rates.items()):
                if not values or values[-1] < now - 20:
                    self._rates.pop(key, None)

    def post(self, payload: object, ip: str) -> dict:
        if not isinstance(payload, dict):
            raise OverlayError("Требуется JSON-объект.")
        kind = payload.get("type")
        if kind not in ("stroke", "reaction", "clear"):
            raise OverlayError("Неизвестный тип события.")

        client_id = payload.get("client_id", "")
        if not isinstance(client_id, str) or not _CLIENT_ID.fullmatch(client_id):
            # Client event ID is required to deduplicate local optimistic strokes.
            raise OverlayError("Неверный идентификатор события.")

        event: dict = {"type": kind, "client_id": client_id}
        if kind == "stroke":
            if set(payload) != {"type", "client_id", "points", "color", "width"}:
                raise OverlayError("Неверные поля линии.")
            points = payload.get("points")
            if not isinstance(points, list) or not 1 <= len(points) <= 32:
                raise OverlayError("Линия должна содержать от 1 до 32 точек.")
            cleaned = []
            for point in points:
                if not isinstance(point, list) or len(point) != 2:
                    raise OverlayError("Каждая точка должна иметь x и y.")
                cleaned.append([_coordinate(point[0]), _coordinate(point[1])])
            color = payload.get("color")
            width = payload.get("width")
            if color not in COLORS or isinstance(width, bool) or width not in WIDTHS:
                raise OverlayError("Неизвестный цвет или толщина пера.")
            event.update(points=cleaned, color=color, width=width)
        elif kind == "reaction":
            if set(payload) != {"type", "client_id", "emoji", "x", "y"}:
                raise OverlayError("Неверные поля реакции.")
            emoji = payload.get("emoji")
            if not isinstance(emoji, str) or emoji not in REACTIONS:
                raise OverlayError("Такая реакция не разрешена.")
            event.update(emoji=emoji, x=_coordinate(payload["x"]), y=_coordinate(payload["y"]))
        else:
            if set(payload) != {"type", "client_id"}:
                raise OverlayError("Неверные поля очистки.")

        with self._lock:
            now = self._clock()
            self._rate(ip, kind, self._monotonic())
            if kind == "clear":
                # Explicit shared action, not a local-only erase.
                if self._events:
                    self._dropped_through = max(self._dropped_through, self._events[-1]["id"])
                self._events.clear()
            self._latest_id += 1
            event["id"] = self._latest_id
            event["time"] = now
            self._events.append(event)
            self._prune(now)
            return event.copy()
