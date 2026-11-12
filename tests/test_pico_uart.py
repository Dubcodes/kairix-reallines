"""V1.2 Pico wire-level regressions, including real Pi UART capture 2026-11-13."""
from __future__ import annotations

import time
import unittest

from app.pico_protocol import (
    MAGIC, FRAME_BYTES, PicoPacket, PicoStreamDecoder, crc16_ccitt,
    encode_packet,
)
from app.pico_source import PicoUartWorker, STALE_SECONDS

# Exactly the first 63 bytes of the `head | od` capture from the Raspberry Pi.
# The trailing a5 in the 64-byte terminal output starts the *next* packet.
REAL_CAPTURE = bytes.fromhex("""
    a5 5a 01 01 e7 7e 02 00 1a 6c 7f 13 00 00 00 00
    61 b5 77 3a b8 ff ff ff ff ff ff ff 00 00 00 00
    00 00 00 00 0a 00 00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00 00 02 01 f1 b5
""")


def packet(seq=1, session=10, pan=120, tilt=-34, zoom=5,
           presses=(0, 0, 0), flags=0x0102, pico_us=None, focus=0):
    return PicoPacket(sequence=seq, session_id=session,
                      pico_us=seq * 2000 if pico_us is None else pico_us,
                      pan=pan, tilt=tilt, zoom=zoom, focus=focus,
                      button_bits=0, button_presses=presses, flags=flags)


class PicoProtocolTests(unittest.TestCase):
    def test_real_hardware_frame_crc_and_all_fields(self):
        self.assertEqual(len(REAL_CAPTURE), 63)
        self.assertEqual(FRAME_BYTES, 63)
        self.assertEqual(MAGIC, b"\xa5\x5a")
        self.assertEqual(int.from_bytes(REAL_CAPTURE[-2:], "little"), 0xB5F1)
        self.assertEqual(crc16_ccitt(REAL_CAPTURE[:-2]), 0xB5F1)
        decoder = PicoStreamDecoder()
        (decoded,) = decoder.feed(REAL_CAPTURE)
        self.assertEqual((decoded.sequence, decoded.session_id, decoded.pico_us),
                         (163559, 980923745, 327117850))
        self.assertEqual((decoded.pan, decoded.tilt, decoded.zoom, decoded.focus), (-72, 0, 10, 0))
        self.assertEqual(decoded.button_presses, (0, 0, 0))
        self.assertEqual(decoded.flags, 0x0102)
        self.assertEqual(decoded.firmware_version, '1.0')
        self.assertTrue(decoded.focus_reserved)
        self.assertFalse(decoded.watchdog_reset)
        self.assertEqual(decoded.unknown_flags, 0)
        self.assertEqual(encode_packet(decoded), REAL_CAPTURE)
        self.assertEqual(decoder.crc_errors, 0)

    def test_checked_in_firmware_does_not_reintroduce_58_byte_format(self):
        from pathlib import Path
        c = (Path(__file__).resolve().parents[1] / 'firmware/pico/main.c').read_text(encoding='utf-8')
        self.assertIn('#define PACKET_SIZE 63u', c)
        self.assertIn('packet.magic[0] = 0xa5;', c)
        self.assertIn('packet.magic[1] = 0x5a;', c)
        self.assertIn('int64_t counts[AXES]', c)
        self.assertIn('packet.flags = (watchdog_reset ? 1u : 0u) | 2u | (1u << 8)', c)
        self.assertNotIn('KRP1', c)

    def test_roundtrip_with_extreme_signed_64bit_counts(self):
        original = packet(44, pan=-(1<<63), tilt=(1<<63)-1, zoom=-2147483649, focus=1729)
        self.assertEqual(PicoStreamDecoder().feed(encode_packet(original)), [original])

    def test_fragments_noise_and_real_frame_boundary(self):
        decoder = PicoStreamDecoder()
        self.assertEqual(decoder.feed(b'garbage\xa5'), [])
        self.assertEqual(decoder.feed(REAL_CAPTURE[1:10]), [])
        self.assertEqual(decoder.feed(REAL_CAPTURE[10:]), [PicoStreamDecoder().feed(REAL_CAPTURE)[0]])
        self.assertGreaterEqual(decoder.dropped_bytes, 7)
        self.assertEqual(decoder.feed(b'\xa5' + encode_packet(packet(2))[1:]), [packet(2)])

    def test_crc_failure_and_resync(self):
        decoder = PicoStreamDecoder()
        broken = bytearray(REAL_CAPTURE)
        broken[32] ^= 0x80
        result = decoder.feed(bytes(broken) + encode_packet(packet(4)))
        self.assertEqual(result, [packet(4)])
        self.assertEqual(decoder.crc_errors, 1)

    def test_reject_wrong_version_and_type_then_resync(self):
        decoder = PicoStreamDecoder()
        wrong_version = bytearray(REAL_CAPTURE)
        wrong_version[2] = 2
        wrong_type = bytearray(REAL_CAPTURE)
        wrong_type[3] = 7
        self.assertEqual(decoder.feed(bytes(wrong_version)+bytes(wrong_type)+REAL_CAPTURE),
                         PicoStreamDecoder().feed(REAL_CAPTURE))
        self.assertEqual(decoder.protocol_errors, 2)

    def test_bounded_memory_on_garbage(self):
        decoder = PicoStreamDecoder()
        decoder.feed(b'x'*100_000)
        self.assertLess(len(decoder.buffer), FRAME_BYTES)
        self.assertGreater(decoder.dropped_bytes, 90_000)
        self.assertEqual(decoder.feed(REAL_CAPTURE), PicoStreamDecoder().feed(REAL_CAPTURE))

    def test_tracking_source_mapping_remains_fixed_x4(self):
        # Existing tracking pipeline integration must survive wire-format changes.
        try:
            from app.tracking import (default_axis_config, merge_axis_update,
                                      validate_axis_config, tracking_fingerprint, default_profile)
        except ImportError:
            self.skipTest('Full app.tracking module not mounted in isolated packet test')
        original = default_axis_config('pan', source='quadrature_gpio')
        pico = merge_axis_update('pan', original, {'source': 'pico_uart'})
        self.assertEqual(pico['mapping']['quadrature_multiplier'], 4)
        validate_axis_config('pan', pico)
        profile = default_profile()
        before = tracking_fingerprint(profile)
        profile['axes']['pan'] = pico
        self.assertNotEqual(before, tracking_fingerprint(profile))

    def test_normal_flags_are_not_encoder_faults(self):
        worker = PicoUartWorker(device='/dev/null')
        now = time.monotonic()
        worker.feed(REAL_CAPTURE, now=now)
        snap = worker.snapshot('pan', now=now+.001)
        self.assertTrue(snap['driver_alive'])
        self.assertEqual(snap['integrity'], 'TRUSTED')
        self.assertEqual(snap['health'], 'VALID')
        self.assertIsNone(snap['integrity_reason'])
        status = worker.status(now=now+.001)
        self.assertEqual(status['firmware_flags'], 0x0102)
        self.assertIsNone(status['fault_flags'])
        self.assertEqual(status['firmware_version'], '1.0')
        self.assertTrue(status['focus_reserved'])
        self.assertEqual(status['uptime_ms'], 327117)

    def test_watchdog_metadata_also_not_live_encoder_fault(self):
        worker = PicoUartWorker('/dev/null')
        now = time.monotonic()
        worker.feed(encode_packet(packet(1, flags=0x0103)), now=now)
        self.assertTrue(worker.status(now=now)['watchdog_reset'])
        self.assertEqual(worker.snapshot('pan', now=now)['integrity'], 'TRUSTED')

    def test_packet_gaps_do_not_erase_cumulative_movement(self):
        worker = PicoUartWorker('/dev/null')
        now = time.monotonic()
        worker.feed(encode_packet(packet(1, pan=120)), now=now)
        worker.feed(encode_packet(packet(4, pan=150)), now=now+.006)
        self.assertEqual(worker.snapshot('pan', now=now+.007)['raw_count'], 150)
        self.assertEqual(worker.status(now=now+.007)['packet_gaps'], 2)
        self.assertEqual(worker.snapshot('pan', now=now+.007)['integrity'], 'TRUSTED')

    def test_duplicate_and_out_of_order_packets_do_not_rewind_counts(self):
        worker = PicoUartWorker('/dev/null')
        now = time.monotonic()
        worker.feed(encode_packet(packet(5, pan=50)), now=now)
        worker.feed(encode_packet(packet(5, pan=-500)), now=now+.001)
        worker.feed(encode_packet(packet(4, pan=-400)), now=now+.002)
        self.assertEqual(worker.snapshot('pan', now=now+.003)['raw_count'], 50)
        self.assertEqual(worker.status(now=now+.003)['out_of_order_packets'], 2)

    def test_session_change_invalidates_all_references(self):
        worker = PicoUartWorker('/dev/null')
        now = time.monotonic()
        worker.feed(encode_packet(packet(1, session=41, pan=17, zoom=9)), now=now)
        worker.set_reference('pan', 0)
        worker.set_reference('zoom', 0)
        worker.feed(encode_packet(packet(1, session=42, pan=0, zoom=0)), now=now+.003)
        for axis in ('pan', 'zoom'):
            self.assertFalse(worker.snapshot(axis, now=now+.004)['referenced'])
        self.assertEqual(worker.status(now=now+.004)['restarts'], 1)

    def test_long_link_outage_invalidates_reference_and_recovers(self):
        worker = PicoUartWorker('/dev/null')
        now = time.monotonic()
        worker.feed(encode_packet(packet(1)), now=now)
        worker.set_reference('pan', 0)
        self.assertFalse(worker.snapshot('pan', now=now+STALE_SECONDS+.005)['driver_alive'])
        worker.feed(encode_packet(packet(2, pan=100)), now=now+STALE_SECONDS+.020)
        snap = worker.snapshot('pan', now=now+STALE_SECONDS+.021)
        self.assertTrue(snap['driver_alive'])
        self.assertFalse(snap['referenced'])
        self.assertEqual(snap['raw_count'], 100)
        self.assertEqual(worker.status(now=now+STALE_SECONDS+.021)['link_timeouts'], 1)

    def test_button_rollover_and_ambiguity(self):
        worker = PicoUartWorker('/dev/null')
        now = time.monotonic()
        worker.feed(encode_packet(packet(1, presses=(65535, 0, 3))), now=now)
        worker.feed(encode_packet(packet(5, presses=(1, 2, 3))), now=now+.008)
        self.assertEqual(worker.status(now=now+.009)['button_presses'], [2, 2, 0])
        worker.feed(encode_packet(packet(6, presses=(40000, 2, 3))), now=now+.010)
        self.assertEqual(worker.status(now=now+.011)['button_presses'][0], 2)

    def test_same_session_timestamp_rollback_invalidates_references(self):
        worker = PicoUartWorker('/dev/null')
        now = time.monotonic()
        worker.feed(encode_packet(packet(100, pico_us=2_000_000)), now=now)
        worker.set_reference('tilt', 0)
        worker.feed(encode_packet(packet(101, pico_us=1000)), now=now+.005)
        self.assertFalse(worker.snapshot('tilt', now=now+.006)['referenced'])


if __name__ == '__main__':
    unittest.main()
