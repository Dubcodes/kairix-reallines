from __future__ import annotations

from collections import deque
from statistics import median
from typing import Any


FILTER_PROFILES = {
    "off": {"window": 1, "label": "Off"},
    "conservative": {"window": 5, "label": "Conservative"},
    "experimental": {"window": 7, "label": "Experimental"},
}


def evaluate_direction_consistency(values: list[int], confirmations: int = 2) -> list[int]:
    """Offline candidate used to compare direction confirmation with medians.

    It is intentionally not a runtime profile: confirming direction creates a
    stepwise response that needs physical rig evaluation before production use.
    """
    if not values:
        return []
    if confirmations < 2:
        raise ValueError("Direction consistency requires at least two confirmations")
    accepted = int(values[0])
    direction = 0
    streak = 0
    output = [accepted]
    for value in values[1:]:
        raw = int(value)
        delta = raw - accepted
        new_direction = (delta > 0) - (delta < 0)
        if new_direction == 0:
            direction = streak = 0
        elif new_direction == direction:
            streak += 1
        else:
            direction, streak = new_direction, 1
        if streak >= confirmations:
            accepted = raw
            direction = streak = 0
        output.append(accepted)
    return output


class PicoCountGlitchFilter:
    """Short median filter over cumulative encoder counts.

    A median introduces a bounded group delay but no positional deadband: once a
    cumulative change persists it is accepted exactly. Short reversible impulses
    disappear without modifying the source packet or its raw cumulative counts.
    """

    AXES = ("pan", "tilt", "zoom")

    def __init__(self, profile: str = "off") -> None:
        self._history = {axis: deque() for axis in self.AXES}
        self._accepted: dict[str, int] = {}
        self._suppressed = {axis: 0 for axis in self.AXES}
        self._pending_impulse: dict[str, tuple[int, int] | None] = {axis: None for axis in self.AXES}
        self.configure(profile)

    def configure(self, profile: str) -> None:
        if profile not in FILTER_PROFILES:
            raise ValueError("Pico glitch filter must be off, conservative or experimental")
        self.profile = profile
        self.window = int(FILTER_PROFILES[profile]["window"])
        self._suppressed = {axis: 0 for axis in self.AXES}
        self.reset()

    def reset(self) -> None:
        self._history = {axis: deque(maxlen=self.window) for axis in self.AXES}
        self._accepted = {}
        self._pending_impulse = {axis: None for axis in self.AXES}

    @property
    def nominal_delay_samples(self) -> int:
        return self.window // 2

    def process(self, counts: dict[str, int]) -> dict[str, int]:
        result: dict[str, int] = {}
        for axis in self.AXES:
            raw = int(counts[axis])
            history = self._history[axis]
            history.append(raw)
            accepted = raw if self.window == 1 else int(median(history))
            previous = self._accepted.get(axis, raw)
            pending = self._pending_impulse[axis]
            if raw != accepted and accepted == previous:
                if pending is None:
                    self._pending_impulse[axis] = (raw, accepted)
            elif pending is not None and raw == accepted == pending[1]:
                self._suppressed[axis] += 1
                self._pending_impulse[axis] = None
            elif accepted != previous:
                self._pending_impulse[axis] = None
            self._accepted[axis] = accepted
            result[axis] = accepted
        return result

    def diagnostics(self, processing_rate_hz: float) -> dict[str, Any]:
        rate = max(1.0, float(processing_rate_hz))
        return {
            "profile": self.profile,
            "window_samples": self.window,
            "nominal_delay_samples": self.nominal_delay_samples,
            "nominal_delay_ms": round(self.nominal_delay_samples * 1000.0 / rate, 3),
            "suppressed_impulses": dict(self._suppressed),
            "accepted_counts": dict(self._accepted),
        }
