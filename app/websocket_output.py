from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Awaitable, Callable


class ClientOutput:
    """Bounded single-writer WebSocket output with latest-pose coalescing."""

    def __init__(self, websocket: Any, on_error: Callable[[Exception], Awaitable[None]], critical_capacity: int = 8) -> None:
        self.websocket = websocket
        self.on_error = on_error
        self.critical_capacity = critical_capacity
        self.critical: deque[dict[str, Any]] = deque()
        self.latest_pose: dict[str, Any] | None = None
        self.event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.closed = False
        self.coalesced_poses = 0
        self.sent_messages = 0
        self.critical_overflows = 0

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="websocket-client-output")

    def enqueue(self, message: dict[str, Any]) -> bool:
        if self.closed:
            return False
        if message.get("type") == "pose":
            if self.latest_pose is not None:
                self.coalesced_poses += 1
            self.latest_pose = message
        else:
            if len(self.critical) >= self.critical_capacity:
                self.critical_overflows += 1
                self.closed = True
                self.event.set()
                return False
            self.critical.append(message)
        self.event.set()
        return True

    async def _run(self) -> None:
        try:
            while not self.closed:
                await self.event.wait()
                while self.critical and not self.closed:
                    await self.websocket.send_json(self.critical.popleft())
                    self.sent_messages += 1
                if self.latest_pose is not None and not self.closed:
                    pose, self.latest_pose = self.latest_pose, None
                    await self.websocket.send_json(pose)
                    self.sent_messages += 1
                if not self.critical and self.latest_pose is None:
                    self.event.clear()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.closed = True
            await self.on_error(exc)

    async def stop(self) -> None:
        self.closed = True
        self.event.set()
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    def stats(self) -> dict[str, int]:
        return {
            "critical_queued": len(self.critical),
            "pose_queued": int(self.latest_pose is not None),
            "coalesced_poses": self.coalesced_poses,
            "sent_messages": self.sent_messages,
            "critical_overflows": self.critical_overflows,
        }
