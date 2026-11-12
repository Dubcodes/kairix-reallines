from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from app.pico_protocol import (FRAME_BYTES, PicoPacket, PicoStreamDecoder,
                               crc16_ccitt, encode_packet)
from app.pico_source import PicoUartWorker, STALE_SECONDS
from app.tracking import default_axis_config, merge_axis_update, validate_axis_config, tracking_fingerprint, default_profile


def packet(seq=1, session=10, pan=120, tilt=-34, zoom=5, presses=(0, 0, 0)):
    return PicoPacket(sequence=seq, session_id=session, pico_us=seq*2000,
                      pan=pan, tilt=tilt, zoom=zoom, button_bits=0,
                      button_presses=presses, fault_flags=0, uptime_ms=seq*2)


class PicoProtocolTests(unittest.TestCase):
    def test_binary_frame_matches_fixed_58_byte_wire_contract(self):
        raw = encode_packet(packet())
        self.assertEqual(len(raw), 58)
        self.assertEqual(FRAME_BYTES, 58)
        self.assertEqual(raw[:4], b'KRP1')
        self.assertEqual(int.from_bytes(raw[-2:], 'little'), crc16_ccitt(raw[:-2]))
        self.assertEqual(PicoStreamDecoder().feed(raw), [packet()])

    def test_partial_reads_noise_corruption_and_resynchronisation(self):
        decoder = PicoStreamDecoder()
        good1 = encode_packet(packet(1))
        corrupt = bytearray(encode_packet(packet(2)))
        corrupt[27] ^= 0x01
        good3 = encode_packet(packet(3))
        self.assertEqual(decoder.feed(b'noise'+good1[:31]), [])
        self.assertEqual(decoder.feed(good1[31:]+bytes(corrupt)+good3), [packet(1), packet(3)])
        self.assertEqual(decoder.crc_errors, 1)

    def test_count_is_cumulative_after_skipped_packet(self):
        worker = PicoUartWorker(device='/dev/null')
        t = time.monotonic()
        worker.feed(encode_packet(packet(1, pan=120)), now=t)
        worker.feed(encode_packet(packet(4, pan=150)), now=t+.006)
        self.assertEqual(worker.snapshot('pan', now=t+.007)['raw_count'], 150)
        self.assertEqual(worker.status(now=t+.007)['packet_gaps'], 2)
        self.assertEqual(worker.snapshot('pan', now=t+.007)['integrity'], 'TRUSTED')

    def test_pico_reset_drops_all_references_without_erasing_count(self):
        worker = PicoUartWorker('/dev/null')
        t = time.monotonic()
        worker.feed(encode_packet(packet(1, session=41, pan=17, zoom=9)), now=t)
        worker.set_reference('pan', 0)
        worker.set_reference('zoom', 0)
        self.assertTrue(worker.snapshot('pan', now=t)['referenced'])
        worker.feed(encode_packet(packet(1, session=42, pan=0, zoom=0)), now=t+.003)
        for axis in ('pan', 'zoom'):
            snap = worker.snapshot(axis, now=t+.004)
            self.assertFalse(snap['referenced'])
            self.assertIsNone(snap['reference_id'])
        self.assertEqual(worker.status(now=t+.004)['restarts'], 1)

    def test_stale_link_is_invalid_even_when_encoders_are_stationary(self):
        worker = PicoUartWorker('/dev/null')
        t = time.monotonic()
        worker.feed(encode_packet(packet(1)), now=t)
        self.assertTrue(worker.snapshot('tilt', now=t+.01)['driver_alive'])
        self.assertFalse(worker.snapshot('tilt', now=t+STALE_SECONDS+.01)['driver_alive'])

    def test_button_counters_recover_intervening_press_events(self):
        worker = PicoUartWorker('/dev/null')
        t = time.monotonic()
        worker.feed(encode_packet(packet(1, presses=(65535, 0, 3))), now=t)
        worker.feed(encode_packet(packet(5, presses=(1, 2, 3))), now=t+.008)
        self.assertEqual(worker.status(now=t+.009)['button_presses'], [2, 2, 0])

    def test_pico_source_keeps_quad_4_and_invalidates_old_tracking_identity(self):
        original = default_axis_config('pan', source='quadrature_gpio')
        pico = merge_axis_update('pan', original, {'source': 'pico_uart'})
        self.assertEqual(pico['mapping']['quadrature_multiplier'], 4)
        validate_axis_config('pan', pico)
        profile = default_profile()
        before = tracking_fingerprint(profile)
        profile['axes']['pan'] = pico
        self.assertNotEqual(before, tracking_fingerprint(profile))


if __name__ == '__main__':
    unittest.main()
