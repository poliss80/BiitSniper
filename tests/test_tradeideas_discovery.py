"""Trade Ideas discovery scheduling tests."""

import unittest
from unittest.mock import patch

import engine.equity.discovery as discovery


class ImmediateExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, func, **kwargs):
        self.calls.append((func, kwargs))
        return None


class TradeIdeasDiscoveryTests(unittest.TestCase):
    def setUp(self):
        discovery.last_ti_scan = 0.0
        discovery._ti_future = None
        discovery._ti_started_at = 0.0
        discovery._ti_warned_running = False

    def test_core_tradeideas_run_scrapes_all_configured_pages(self):
        executor = ImmediateExecutor()

        with patch.object(discovery, "_ti_executor", executor), patch.object(discovery, "time") as mocked_time:
            mocked_time.time.return_value = 10_000.0
            discovery.scan_tradeideas_universe(
                enabled=True,
                scan_interval_min=15,
                headless=False,
                chrome_profile="Profile 3",
                update_config=True,
                priority_1=[],
                priority_2=[],
                browser="edge",
            )

        self.assertEqual(len(executor.calls), 1)
        _func, kwargs = executor.calls[0]
        self.assertEqual(kwargs["scan_keys"], discovery.TRADEIDEAS_ALL_SCAN_KEYS)
        self.assertTrue(kwargs["include_toplists"])
        self.assertEqual(kwargs["select_minutes"], 15)


if __name__ == "__main__":
    unittest.main()