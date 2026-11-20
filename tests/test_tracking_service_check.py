"""Readiness gate tests; no Unix domain sockets or hardware required."""
import unittest
from unittest.mock import patch

from app.tracking_service_check import probe


class OwnerReadinessTests(unittest.TestCase):
    @patch('app.tracking_service_check.rpc')
    def test_listening_owner_is_ready_before_web_configuration(self, rpc):
        rpc.return_value = {'configured': False}
        self.assertEqual(probe('/run/test.sock', deadline=0, now=lambda: 0), {'configured': False})

    @patch('app.tracking_service_check.rpc')
    def test_configured_gate_waits_for_ack(self, rpc):
        rpc.side_effect = [{'configured': False}, {'configured': True, 'pose_ticks': 42}]
        clock = iter([0.0, 0.0])
        value = probe('/run/test.sock', deadline=1.0, require_configured=True,
                      now=lambda: next(clock), pause=lambda _: None)
        self.assertEqual(value['pose_ticks'], 42)

    @patch('app.tracking_service_check.rpc')
    def test_health_response_must_expose_configured(self, rpc):
        rpc.return_value = {'something_else': True}
        with self.assertRaisesRegex(RuntimeError, 'configured flag'):
            probe('/run/test.sock', deadline=0, now=lambda: 0)

    @patch('app.tracking_service_check.rpc')
    def test_unconfigured_does_not_falsely_claim_ready(self, rpc):
        rpc.return_value = {'configured': False}
        with self.assertRaisesRegex(RuntimeError, 'not configured'):
            probe('/run/test.sock', deadline=0, require_configured=True, now=lambda: 0)


if __name__ == '__main__':
    unittest.main()
