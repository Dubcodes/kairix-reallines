import asyncio
import json
import unittest

from app.performance import RuntimeProfiler
from app.websocket_output import ClientOutput


class FakeSocket:
    def __init__(self, blocked=False):
        self.messages = []
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.active_writers = 0
        self.max_writers = 0

    async def send_json(self, message):
        self.active_writers += 1
        self.max_writers = max(self.max_writers, self.active_writers)
        await self.release.wait()
        self.messages.append(message)
        self.active_writers -= 1

    async def send_text(self, payload):
        await self.send_json(json.loads(payload))


class WebSocketOutputCase(unittest.IsolatedAsyncioTestCase):
    async def test_slow_client_isolated_pose_coalesced_and_single_writer(self):
        failures = []
        async def failed(exc):
            failures.append(exc)

        slow_socket, fast_socket = FakeSocket(blocked=True), FakeSocket()
        slow, fast = ClientOutput(slow_socket, failed), ClientOutput(fast_socket, failed)
        slow.start(); fast.start()
        try:
            state = {"type": "state", "data": {"generation": 1}}
            slow.enqueue(state); fast.enqueue(state)
            await asyncio.sleep(0)
            for sequence in range(1, 6):
                message = {"type": "pose", "data": {"sequence": sequence}}
                slow.enqueue(message); fast.enqueue(message)
            await asyncio.sleep(.01)
            self.assertEqual(fast_socket.messages[-1]["data"]["sequence"], 5)
            self.assertEqual(slow_socket.messages, [])
            self.assertGreaterEqual(slow.coalesced_poses, 4)
            slow_socket.release.set()
            await asyncio.sleep(.01)
            self.assertEqual([message["type"] for message in slow_socket.messages], ["state", "pose"])
            self.assertEqual(slow_socket.messages[-1]["data"]["sequence"], 5)
            self.assertEqual(slow_socket.max_writers, 1)
            self.assertFalse(failures)
        finally:
            await slow.stop(); await fast.stop()

    async def test_critical_queue_is_bounded(self):
        socket = FakeSocket(blocked=True)
        async def failed(_exc):
            return None
        output = ClientOutput(socket, failed, critical_capacity=2)
        output.start()
        try:
            self.assertTrue(output.enqueue({"type": "state", "data": 1}))
            self.assertTrue(output.enqueue({"type": "state", "data": 2}))
            self.assertFalse(output.enqueue({"type": "state", "data": 3}))
            self.assertEqual(output.critical_overflows, 1)
        finally:
            socket.release.set()
            await output.stop()

    async def test_calibration_overlay_edits_coalesce_to_latest_payload(self):
        socket = FakeSocket(blocked=True)
        async def failed(_exc):
            return None
        output = ClientOutput(socket, failed, critical_capacity=1)
        output.start()
        try:
            for marker in range(20):
                self.assertTrue(output.enqueue({"type": "calibration_overlay", "data": {"marker": marker}}))
            self.assertEqual(output.stats()["overlay_queued"], 1)
            self.assertEqual(output.critical_overflows, 0)
            socket.release.set()
            await asyncio.sleep(.01)
            self.assertEqual(socket.messages[-1]["data"]["marker"], 19)
        finally:
            await output.stop()

    async def test_production_path_measures_json_serialization_separately(self):
        socket = FakeSocket()
        profiler = RuntimeProfiler(capacity=32)
        async def failed(_exc):
            return None
        output = ClientOutput(socket, failed, profiler=profiler)
        output.start()
        try:
            output.enqueue({"type": "state", "data": {"value": "✓"}})
            await asyncio.sleep(.01)
            self.assertEqual(socket.messages[-1]["data"]["value"], "✓")
            timing = profiler.snapshot()["timings_ms"]["websocket_state_serialization"]
            self.assertEqual(timing["wall"]["samples"], 1)
        finally:
            await output.stop()
