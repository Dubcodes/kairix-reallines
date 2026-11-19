import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tracking_mmap import TrackingMmapError
from app.tracking_proxy import DedicatedTrackingProxy, configuration, token


def store():
    profile = {'axes': {name: {'source': 'pico_uart' if name in ('pan', 'tilt', 'zoom') else 'disabled'}
                        for name in ('pan', 'tilt', 'zoom', 'focus')}}
    return SimpleNamespace(
        profile=profile, profile_index={'active_id': 'test'},
        world_calibration={}, world_calibration_index={'active_id': 'world'},
        zoom_fov_mapping={'active': False}, zoom_lens_profiles={},
    )


class DedicatedProxyTests(unittest.TestCase):
    def test_initial_config_ack_does_not_launch_high_frequency_rpc_poller(self):
        s = store()
        proxy = DedicatedTrackingProxy('/fake.sock', '/fake-mmap', s)
        expected = token(configuration(s))
        with patch('app.tracking_proxy.rpc', return_value={'config_token': expected}) as rpc:
            proxy.configure('test', s.profile)
        self.assertEqual(proxy._token, expected)
        self.assertIsNone(proxy._thread)
        self.assertEqual(rpc.call_count, 1)

    def test_calibration_change_requires_matching_ack(self):
        s = store()
        proxy = DedicatedTrackingProxy('/fake.sock', '/fake-mmap', s)
        with patch('app.tracking_proxy.rpc', return_value={'config_token': 'wrong'}):
            with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                proxy.sync_configuration(configuration(s))
        self.assertIsNone(proxy._token)

    def test_processing_settings_still_reach_owner(self):
        s = store()
        proxy = DedicatedTrackingProxy('/fake.sock', '/fake-mmap', s)
        proxy._token = 'test'
        settings = {'mode': 'latest', 'buffer_capacity': 256}
        with patch('app.tracking_proxy.rpc', return_value={'processing': settings}) as rpc:
            proxy.configure_pico_processing(settings)
        self.assertEqual(rpc.call_args.args[1]['action'], 'processing')

    def test_stale_configuration_is_not_accepted(self):
        s = store()
        proxy = DedicatedTrackingProxy('/fake.sock', '/fake-mmap', s)
        proxy._token = 'current'
        proxy.reader.read = lambda: {'config_token': 'old'}
        with self.assertRaisesRegex(TrackingMmapError, 'mismatch'):
            proxy.latest()

if __name__ == '__main__':
    unittest.main()
