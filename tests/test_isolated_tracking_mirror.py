"""Live-UART shadow mirror tests; all tests run without serial hardware."""
import unittest

from app.isolated_tracking_mirror import MirrorSource
from app.pico_protocol import PicoPacket, encode_packet


class RealMirrorTests(unittest.TestCase):
    def packet(self, seq, pan, session=77):
        return encode_packet(PicoPacket(sequence=seq, session_id=session,
                                        pico_us=seq * 2000, pan=pan, tilt=-10, zoom=0,
                                        button_presses=(seq, 0, 0)))

    def test_split_packets_are_reassembled_and_latest_count_wins(self):
        source = MirrorSource()
        first = self.packet(1, 100)
        self.assertEqual(source.feed(first[:11], arrival=10), 0)
        self.assertEqual(source.feed(first[11:], arrival=10), 1)
        source.feed(self.packet(2, 105), arrival=10.002)
        sample = source.snapshots(now=10.003)['pan']
        self.assertEqual(sample['raw_count'], 105)
        self.assertEqual(sample['reference_count'], 100)
        self.assertTrue(sample['referenced'])
        self.assertEqual(source.press_counters, (2, 0, 0))

    def test_gaps_keep_position_and_are_reported(self):
        source = MirrorSource()
        source.feed(self.packet(1, 100), 10)
        source.feed(self.packet(5, 200), 10.01)
        self.assertEqual(source.sequence_gaps, 3)
        self.assertEqual(source.snapshots(10.02)['pan']['raw_count'], 200)

    def test_sessions_reset_shadow_reference_and_late_samples_go_stale(self):
        source = MirrorSource()
        source.feed(self.packet(1, 100), 10)
        old_id = source.snapshots(10)['pan']['reference_id']
        source.feed(self.packet(1, 900, session=88), 10.1)
        now = source.snapshots(10.11)['pan']
        self.assertEqual(now['reference_count'], 900)
        self.assertNotEqual(now['reference_id'], old_id)
        self.assertEqual(source.session_restarts, 1)
        self.assertFalse(source.snapshots(10.6)['pan']['driver_alive'])

    def test_bad_crc_does_not_replace_good_position(self):
        source = MirrorSource()
        source.feed(self.packet(1, 100), 10)
        bad = bytearray(self.packet(2, 999))
        bad[24] ^= 0x40
        source.feed(bytes(bad), 10.002)
        self.assertEqual(source.snapshots(10.003)['pan']['raw_count'], 100)
        self.assertEqual(source.decoder.crc_errors, 1)


if __name__ == '__main__':
    unittest.main()
