import unittest
from unittest.mock import Mock

from app.pico_owner import Owner
from app.pico_proxy import PicoProxy


class OwnerIPCTests(unittest.TestCase):
    def test_unconfigured_owner_rejects_snapshot_and_reference(self):
        owner = Owner()
        with self.assertRaisesRegex(ValueError, 'not been configured'):
            owner.execute({'action': 'snapshot'})
        with self.assertRaisesRegex(ValueError, 'not been configured'):
            owner.execute({'action': 'reference', 'axis': 'pan', 'angle': 0})

    def test_reference_commands_are_explicit_and_validated(self):
        owner = Owner()
        fake = Mock()
        fake.set_reference.return_value = 'reference-test'
        owner.manager = fake
        owner.configured = True
        self.assertEqual(owner.execute({'action': 'reference', 'axis': 'pan', 'angle': 0})['reference_id'], 'reference-test')
        with self.assertRaises(ValueError):
            owner.execute({'action': 'reference', 'axis': 'focus', 'angle': 0})
        owner.execute({'action': 'clear', 'axis': 'tilt'})
        fake.clear_reference.assert_called_once_with('tilt')

    def test_mixed_hardware_mode_fails_closed(self):
        proxy = PicoProxy('/nonexistent/reallines.sock')
        with self.assertRaisesRegex(ValueError, 'mixed GPIO'):
            proxy.configure('profile', {'axes': {'pan': {'source': 'linux_rotary'},
                                               'tilt': {'source': 'pico_uart'}}})

    def test_ipc_outage_cannot_reuse_valid_cached_position(self):
        proxy = PicoProxy('/nonexistent/reallines.sock')
        proxy._axes = ('pan',)
        proxy._samples = {'pan': {'raw_count': 123, 'driver_alive': True, 'referenced': True, 'reference_id': 'old'}}
        result = proxy.snapshots()['pan']
        self.assertFalse(result['driver_alive'])
        self.assertFalse(result['referenced'])
        self.assertIsNone(result['reference_id'])
        self.assertEqual(result['raw_count'], 123)
        self.assertFalse(proxy.pico_status()['connected'])


if __name__ == '__main__':
    unittest.main()

# Integration smoke test for the actual line-oriented IPC framing.
class LocalSocketTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(__import__('socket'), 'AF_UNIX'), 'Unix sockets required')
    def test_rpc_round_trip_and_command_rejection(self):
        import tempfile
        import threading
        from pathlib import Path
        from app.pico_owner import Handler, Server
        from app.pico_proxy import rpc
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'test.sock')
            with Server(path, Handler) as server:
                server.owner = Owner()
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with self.assertRaisesRegex(ValueError, 'not been configured'):
                        rpc(path, {'action': 'snapshot'})
                    with self.assertRaisesRegex(ValueError, 'Unknown Pico IPC command'):
                        server.owner.configured = True
                        rpc(path, {'action': 'bad-command'})
                finally:
                    server.shutdown()
                    thread.join(1.0)
