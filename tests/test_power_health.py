import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.power_health import PowerHealthService, parse_throttled


class PowerHealthCase(unittest.TestCase):
    def test_current_and_historical_flags_are_distinct(self):
        result = parse_throttled("throttled=0x50005\n")
        self.assertTrue(result["under_voltage_now"])
        self.assertTrue(result["throttled_now"])
        self.assertTrue(result["under_voltage_since_boot"])
        self.assertTrue(result["throttled_since_boot"])
        self.assertFalse(result["frequency_capped_now"])

    def test_read_only_status_is_cached(self):
        calls = []
        def runner(args, **_kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="throttled=0x10000\n", stderr="")
        with patch("app.power_health.shutil.which", return_value="/usr/bin/vcgencmd"):
            service = PowerHealthService(runner=runner, ttl=30)
            first = service.status()
            second = service.status()
        self.assertEqual(len(calls), 1)
        self.assertFalse(first["under_voltage_now"])
        self.assertTrue(first["under_voltage_since_boot"])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
