from __future__ import annotations

import math
import re
import time
from collections import OrderedDict
from copy import deepcopy
from typing import Any


CLIENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PAGES = {"gfx", "preview", "control"}
METRIC_FIELDS = {
    "incoming_pose_hz", "pose_interarrival_mean_ms", "pose_interarrival_p95_ms",
    "animation_frame_hz", "frame_interval_p95_ms", "render_mean_ms", "render_p95_ms",
    "held_poses", "missing_poses", "stale_poses", "invalid_tracking_frames",
    "duplicate_pose_messages", "canvas_width", "canvas_height", "device_pixel_ratio",
}


class BrowserDiagnostics:
    """Small, rate-limited in-memory summaries; never receives per-frame telemetry."""

    def __init__(self, *, capacity: int = 32, minimum_interval: float = 1.0,
                 ttl: float = 30.0, clock=time.monotonic) -> None:
        self.capacity = capacity
        self.minimum_interval = minimum_interval
        self.ttl = ttl
        self.clock = clock
        self._reports: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()

    def _purge(self, now: float) -> None:
        for key in [key for key, value in self._reports.items() if now - value["received_mono"] > self.ttl]:
            self._reports.pop(key, None)

    def report(self, client_id: str, page: str, label: str | None,
               metrics: dict[str, Any]) -> bool:
        if not CLIENT_RE.fullmatch(str(client_id)):
            raise ValueError("Browser diagnostic client ID is invalid")
        if page not in PAGES:
            raise ValueError("Browser diagnostic page is invalid")
        if not isinstance(metrics, dict):
            raise ValueError("Browser diagnostic metrics must be an object")
        now = self.clock()
        self._purge(now)
        key = (str(client_id), page)
        previous = self._reports.get(key)
        if previous and now - previous["received_mono"] < self.minimum_interval:
            return False
        clean: dict[str, float | int | None] = {}
        for field in METRIC_FIELDS:
            value = metrics.get(field)
            if value is None:
                clean[field] = None
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"Browser diagnostic metric {field} must be finite")
            clean[field] = max(0, value)
        if key not in self._reports and len(self._reports) >= self.capacity:
            self._reports.popitem(last=False)
        self._reports[key] = {
            "client_id": key[0], "page": page, "label": str(label or "")[:80],
            "metrics": clean, "received_mono": now,
        }
        self._reports.move_to_end(key)
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        now = self.clock()
        self._purge(now)
        return [
            {"client_id": value["client_id"], "page": value["page"], "label": value["label"],
             "age_ms": round((now - value["received_mono"]) * 1000, 1),
             "metrics": deepcopy(value["metrics"])}
            for value in self._reports.values()
        ]
