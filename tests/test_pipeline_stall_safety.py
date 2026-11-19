"""Regression checks for off-event-loop diagnostics and pre-trigger capture."""
import asyncio
import inspect
import unittest
from unittest.mock import patch

from app.stability import TrackingStabilityRecorder


class PipelineStallSafetyTests(unittest.TestCase):
    def test_automatic_trigger_keeps_history_without_deepcopy(self):
        clock = [10.0]
        recorder = TrackingStabilityRecorder(clock=lambda: clock[0])
        for number in range(350):
            clock[0] = 10.0 + number * .005
            samples = {axis: {"raw_count": number, "filtered_count": number}
                       for axis in ("pan", "tilt", "zoom")}
            tracking = {"valid": True, "axes": {
                axis: {"value": 0.0, "reference_id": "stable"}
                for axis in ("pan", "tilt", "zoom")}}
            camera = {"pan": 0.0, "tilt": 0.0, "valid": True}
            recorder.observe_acquisition(clock[0], samples, tracking, camera)
        with patch('app.stability.deepcopy', side_effect=AssertionError('deepcopy on critical path')):
            recorder.observe_scheduling('acquisition', 90.0, clock[0])
        self.assertTrue(recorder.status()['active'])
        history = list(recorder._events)
        self.assertEqual(len(history), 351)
        self.assertEqual(history[0]['stage'], 'pre_event')
        self.assertEqual(history[-1]['stage'], 'scheduling_gap')
        self.assertEqual(history[-1]['gap_ms'], 90.0)

    def test_status_endpoints_are_async_and_offloaded(self):
        # Source-level shape check avoids importing Uvicorn and Pi-specific
        # system services in a Windows-only unittest environment.
        import ast
        from pathlib import Path
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'app' / 'main.py').read_text(encoding='utf-8'))
        endpoints = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
        for name in ('performance_status', 'tracking_stability_status'):
            self.assertIn(name, endpoints)
            code = ast.unparse(endpoints[name])
            self.assertIn('await asyncio.to_thread(', code)
        code = ast.unparse(endpoints['performance_status'])
        self.assertIn('runtime_profiler.snapshot', code)
        self.assertIn('source_manager.pico_status', code)


if __name__ == '__main__':
    unittest.main()
