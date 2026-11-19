from __future__ import annotations

import asyncio
import faulthandler
import os
import signal
import threading
import time
from pathlib import Path
from typing import Callable, TextIO


class EventLoopWatchdog:
    """Independent, bounded evidence capture for a live but stalled event loop."""

    def __init__(self, directory: Path, *, stall_seconds: float = 5.0,
                 check_seconds: float = 1.0, cooldown_seconds: float = 30.0,
                 max_bytes: int = 1_000_000, clock: Callable[[], float] = time.monotonic,
                 short_stall_seconds: float | None = None,
                 short_cooldown_seconds: float = 15.0) -> None:
        self.directory = Path(directory)
        self.stall_seconds = max(1.0, float(stall_seconds))
        self.check_seconds = max(0.1, float(check_seconds))
        self.cooldown_seconds = max(self.stall_seconds, float(cooldown_seconds))
        self.max_bytes = max(64_000, int(max_bytes))
        self.clock = clock
        self._heartbeat = clock()
        self._last_dump = float("-inf")
        self.short_stall_seconds = (None if short_stall_seconds is None
                                    else max(0.02, float(short_stall_seconds)))
        self.short_cooldown_seconds = max(1.0, float(short_cooldown_seconds))
        self._last_short_dump = float("-inf")
        self.short_dump_count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle: TextIO | None = None
        self._signal_registered = False
        self.dump_count = 0

    @property
    def path(self) -> Path:
        return self.directory / "hang-stacks.log"

    def beat(self, timestamp: float | None = None) -> None:
        self._heartbeat = self.clock() if timestamp is None else float(timestamp)

    async def heartbeat_loop(self, interval_seconds: float = 0.25) -> None:
        while True:
            self.beat()
            await asyncio.sleep(interval_seconds)

    def _open_handle(self) -> TextIO:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self._handle is not None:
            self._handle.flush()
            if os.fstat(self._handle.fileno()).st_size >= self.max_bytes:
                # Preserve the descriptor registered with faulthandler while
                # bounding automatic evidence.  Never swap a live signal FD.
                self._handle.seek(0)
                self._handle.truncate(0)
            return self._handle
        if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
            previous = self.path.with_suffix(".log.1")
            previous.unlink(missing_ok=True)
            self.path.replace(previous)
        self._handle = self.path.open("a", encoding="utf-8")
        return self._handle

    def _dump(self, reason: str, now: float) -> None:
        handle = self._open_handle()
        handle.write(f"\n=== {reason} mono={now:.6f} pid={os.getpid()} ===\n")
        handle.flush()
        faulthandler.dump_traceback(file=handle, all_threads=True)
        handle.flush()
        self.dump_count += 1

    def check_once(self, now: float | None = None) -> bool:
        current = self.clock() if now is None else float(now)
        heartbeat_age = current - self._heartbeat
        if heartbeat_age >= self.stall_seconds:
            if current - self._last_dump < self.cooldown_seconds:
                return False
            self._last_dump = current
            self._dump("event-loop heartbeat stalled; process still alive", current)
            return True
        if (self.short_stall_seconds is not None
                and heartbeat_age >= self.short_stall_seconds
                and current - self._last_short_dump >= self.short_cooldown_seconds):
            self._last_short_dump = current
            self._dump(f"short event-loop stall age_ms={heartbeat_age * 1000.0:.1f}", current)
            self.short_dump_count += 1
            return True
        return False

    def _run(self) -> None:
        while not self._stop.wait(self.check_seconds):
            self.check_once()

    def start(self) -> None:
        if self._thread is not None:
            return
        self.beat()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="reallines-loop-watchdog", daemon=True)
        self._thread.start()
        if os.name == "posix" and hasattr(signal, "SIGUSR2"):
            handle = self._open_handle()
            try:
                faulthandler.register(signal.SIGUSR2, file=handle, all_threads=True, chain=False)
                self._signal_registered = True
            except (OSError, RuntimeError, ValueError):
                self._signal_registered = False

    def stop(self) -> None:
        if self._signal_registered and hasattr(signal, "SIGUSR2"):
            try:
                faulthandler.unregister(signal.SIGUSR2)
            except (OSError, RuntimeError, ValueError):
                pass
            self._signal_registered = False
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.check_seconds + 1.0)
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def status(self) -> dict[str, object]:
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "heartbeat_age_seconds": round(max(0.0, self.clock() - self._heartbeat), 3),
            "stall_seconds": self.stall_seconds,
            "dump_count": self.dump_count,
            "short_stall_seconds": self.short_stall_seconds,
            "short_dump_count": self.short_dump_count,
            "diagnostic_path": str(self.path),
            "manual_signal": "SIGUSR2" if self._signal_registered else None,
        }
