from __future__ import annotations

from typing import Any


DEFAULT_PICO_PROCESSING = {
    "mode": "latest",
    "latest_rate_hz": 200,
    "every_rate_hz": 500,
    "buffer_capacity": 256,
    "glitch_filter": "off",
}


def normalise_pico_processing(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    mode = source.get("mode", "latest")
    if mode not in {"latest", "every"}:
        raise ValueError("Pico processing mode must be latest or every")
    try:
        latest = int(source.get("latest_rate_hz", 200))
        every = int(source.get("every_rate_hz", 500))
        capacity = int(source.get("buffer_capacity", 256))
    except (TypeError, ValueError) as exc:
        raise ValueError("Pico processing rates and capacity must be integers") from exc
    if not 20 <= latest <= 500:
        raise ValueError("Latest Snapshot rate must be between 20 and 500 Hz")
    if not 20 <= every <= 1000:
        raise ValueError("Every Sample rate must be between 20 and 1000 Hz")
    if not 16 <= capacity <= 4096:
        raise ValueError("Pico buffer capacity must be between 16 and 4096 samples")
    glitch_filter = str(source.get("glitch_filter", "off"))
    if glitch_filter not in {"off", "conservative", "experimental"}:
        raise ValueError("Pico glitch filter must be off, conservative or experimental")
    return {
        "mode": mode,
        "latest_rate_hz": latest,
        "every_rate_hz": every,
        "buffer_capacity": capacity,
        "glitch_filter": glitch_filter,
    }


def active_processing_rate(settings: dict[str, Any]) -> int:
    return int(settings["every_rate_hz"] if settings["mode"] == "every" else settings["latest_rate_hz"])
