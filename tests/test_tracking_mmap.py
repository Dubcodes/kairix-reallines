import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.tracking_mmap import TrackingMmapError, TrackingReader, TrackingWriter, HEADER, HEADER_BYTES, ENTRY, SLOT_BYTES, SLOTS
from app.tracking_consumer import adopt, mark_unavailable


class TrackingMmapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'tracking.bin'
        self.now = [100.0]
        self.writer = TrackingWriter(self.path, clock=lambda: self.now[0])
        self.writer.open()
        self.reader = TrackingReader(self.path, clock=lambda: self.now[0])

    def tearDown(self):
        self.reader.close()
        self.writer.close()
        self.temp.cleanup()

    def test_coherent_latest_generation_and_pose(self):
        self.writer.write({'camera': {'pan': 1.5}, 'config_token': 'a'})
        self.assertEqual(self.reader.read()['camera']['pan'], 1.5)
        self.writer.write({'camera': {'pan': 3.5}, 'config_token': 'a'})
        self.assertEqual(self.reader.read()['generation'], 2)
        self.assertEqual(self.reader.read()['camera']['pan'], 3.5)

    def test_producer_stale_is_rejected(self):
        self.writer.write({'camera': {'pan': 1.5}})
        self.now[0] += .11
        with self.assertRaisesRegex(TrackingMmapError, 'stale'):
            self.reader.read()

    def test_corruption_is_rejected(self):
        self.writer.write({'camera': {'pan': 1.5}})
        offset = HEADER_BYTES + SLOT_BYTES
        self.writer.mm[offset + ENTRY.size + 2] = 0x55
        with self.assertRaises(TrackingMmapError):
            self.reader.read()

    def test_producer_restart_is_explicit(self):
        self.writer.write({'camera': {'pan': 1.5}})
        original = self.reader.read()['producer_id']
        self.writer.close()
        new_writer = TrackingWriter(self.path, clock=lambda: self.now[0])
        try:
            new_writer.open()
            new_writer.write({'camera': {'pan': 4.5}})
            self.assertNotEqual(self.reader.read()['producer_id'], original)
        finally:
            new_writer.close()

    def test_unavailable_does_not_keep_valid_camera(self):
        store = Mock()
        store.tracking = {'valid': True, 'available': True, 'integrity': 'TRUSTED',
                          'axes': {'pan': {'source': 'pico_uart', 'valid': True, 'reference_id': 'keep'}}}
        store.camera = {'valid': True, 'world_valid': True}
        mark_unavailable(store)
        self.assertFalse(store.camera['valid'])
        self.assertFalse(store.tracking['axes']['pan']['valid'])
        self.assertEqual(store.tracking['axes']['pan']['reference_id'], 'keep')

    def test_producer_id_change_resets_history(self):
        store = Mock()
        store.tracking = {}
        store.camera = {}
        store.zoom_fov_mapping = {}
        value = {'producer_id': 2, 'generation': 1, 'tracking': {'valid': True},
                 'camera': {'pan': 1.5}, 'zoom_fov': {}}
        self.assertEqual(adopt(store, value, (1, 99)), (2, 1))
        store.pose_history.reset.assert_called_once()
        store._refresh_pose_reference_and_sample.assert_called_once()

if __name__ == '__main__':
    unittest.main()
