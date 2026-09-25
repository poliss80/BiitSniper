"""Trade Ideas discovery scheduling tests."""

import contextlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import engine.equity.discovery as discovery
import engine.ti.capture_tradeideas as ct


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


class ScrapeTradeideasRecoveryTests(unittest.TestCase):
    """Per-scan-key browser recovery in scrape_tradeideas (mocks only — no browser/network)."""

    def setUp(self):
        ct._edge_driver = None
        self.addCleanup(setattr, ct, "_edge_driver", None)
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        patch.object(ct, "TI_PRIMARY_FILE", tmp / "ti_primary.json").start()
        patch.object(ct, "TI_UNUSUAL_OPTIONS_FILE", tmp / "ti_unusual_options.json").start()
        patch.object(ct, "RENDER_GRACE_SEC", 0).start()
        patch.object(ct, "DROPDOWN_REFRESH_SEC", 0).start()
        patch.object(ct, "_try_select_timeframe", return_value=True).start()
        self.addCleanup(patch.stopall)

    def _run(self, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            results = ct.scrape_tradeideas(**kwargs)
        return results, buf.getvalue()

    def test_nosuchwindow_on_navigation_retries_once_and_captures(self):
        driver = MagicMock(name="edge-driver")
        driver.execute_cdp_cmd.side_effect = [ct.NoSuchWindowException("window gone"), None, None]
        get_driver = MagicMock(side_effect=[driver, driver])

        with patch.object(ct, "_get_driver", get_driver), patch.object(
            ct, "_extract_tickers", return_value=["AAPL", "MSFT"]
        ):
            results, out = self._run(scan_keys=["highshortfloat"])

        self.assertEqual(results["highshortfloat"], ["AAPL", "MSFT"])
        self.assertEqual(get_driver.call_count, 2)  # initial attach + one recovery
        driver.get.assert_not_called()  # CDP path used throughout; no blocking get()
        self.assertIn("browser session lost", out)
        self.assertIn("retrying this scan once", out)

    def test_repeated_failure_records_empty_and_next_key_processed(self):
        dead1 = MagicMock(name="dead-driver-1")
        dead1.execute_cdp_cmd.side_effect = ct.NoSuchWindowException("gone")
        dead2 = MagicMock(name="dead-driver-2")
        dead2.execute_cdp_cmd.side_effect = ct.NoSuchWindowException("still gone")
        good = MagicMock(name="good-driver")
        get_driver = MagicMock(side_effect=[dead1, dead2, good])

        with patch.object(ct, "_get_driver", get_driver), patch.object(
            ct, "_extract_tickers", return_value=["TSLA"]
        ):
            results, out = self._run(scan_keys=["highshortfloat", "marketscope360"])

        # First key exhausted its single retry → explicit empty result, loop continues.
        self.assertEqual(results["highshortfloat"], [])
        # Later key still processed: its first attempt uses the dead driver, the
        # per-key retry recovers to the good driver and captures.
        self.assertEqual(results["marketscope360"], ["TSLA"])
        self.assertEqual(get_driver.call_count, 3)  # initial + retry key1 + retry key2
        self.assertIn("recording empty result", out)

    def test_zero_tickers_logs_warning_not_exception(self):
        driver = MagicMock(name="edge-driver")

        with patch.object(ct, "_get_driver", return_value=driver), patch.object(
            ct, "_extract_tickers", return_value=[]
        ):
            results, out = self._run(scan_keys=["highshortfloat"])

        self.assertEqual(results["highshortfloat"], [])
        self.assertIn("0 valid tickers captured", out)

    def test_navigation_timeout_stops_page_load_and_continues(self):
        driver = MagicMock(name="edge-driver")
        # CDP unsupported on this driver → fallback to blocking driver.get().
        driver.execute_cdp_cmd.side_effect = AttributeError("no cdp")
        # First scan key's driver.get(url) times out; later navigations succeed
        # (remaining entries cover key 2's get(url) and the about:blank navigations).
        driver.get.side_effect = [ct.TimeoutException("slow page"), None, None, None]

        with patch.object(ct, "_get_driver", return_value=driver), patch.object(
            ct, "_extract_tickers", return_value=["AAPL"]
        ):
            results, out = self._run(scan_keys=["highshortfloat", "marketscope360"])

        # Timed-out key still captured via partial DOM; later key still processed.
        self.assertEqual(results["highshortfloat"], ["AAPL"])
        self.assertEqual(results["marketscope360"], ["AAPL"])
        # window.stop() issued best-effort after the navigation timeout
        # (CDP Page.stopLoading also unavailable here, so JS fallback is used).
        driver.execute_script.assert_any_call("window.stop();")
        # Page-load timeout was set defensively before navigation.
        driver.set_page_load_timeout.assert_called_with(ct.PAGE_LOAD_TIMEOUT_SEC)
        self.assertIn("Page load timeout", out)

    def test_page_load_timeout_setting_failure_is_ignored(self):
        driver = MagicMock(name="edge-driver")
        driver.set_page_load_timeout.side_effect = AttributeError("no such method")

        with patch.object(ct, "_get_driver", return_value=driver), patch.object(
            ct, "_extract_tickers", return_value=["MSFT"]
        ):
            results, out = self._run(scan_keys=["highshortfloat"])

        self.assertEqual(results["highshortfloat"], ["MSFT"])
        self.assertIn("highshortfloat", out)
        self.assertIn(ct.SCANS["highshortfloat"]["url"], out)


class NavigateToScanUrlTests(unittest.TestCase):
    """_navigate_to_scan_url: CDP-first navigation with get() fallback (mocks only)."""

    def test_cdp_navigation_used_instead_of_driver_get(self):
        driver = MagicMock(name="edge-driver")

        used_cdp = ct._navigate_to_scan_url(driver, "https://example.com/scan")

        self.assertTrue(used_cdp)
        driver.execute_cdp_cmd.assert_called_once_with(
            "Page.navigate", {"url": "https://example.com/scan"}
        )
        driver.get.assert_not_called()

    def test_fallback_invokes_driver_get_when_cdp_unsupported(self):
        for exc in (AttributeError("no cdp"), ct.WebDriverException("unsupported")):
            with self.subTest(exc=type(exc).__name__):
                driver = MagicMock(name="edge-driver")
                driver.execute_cdp_cmd.side_effect = exc

                used_cdp = ct._navigate_to_scan_url(driver, "https://example.com/scan")

                self.assertFalse(used_cdp)
                driver.get.assert_called_once_with("https://example.com/scan")

    def test_cdp_nosuchwindow_is_surfaced_not_swallowed(self):
        driver = MagicMock(name="edge-driver")
        driver.execute_cdp_cmd.side_effect = ct.NoSuchWindowException("window gone")

        with self.assertRaises(ct.NoSuchWindowException):
            ct._navigate_to_scan_url(driver, "https://example.com/scan")
        driver.get.assert_not_called()

    def test_scrape_loop_uses_cdp_navigation_for_scan_and_cleanup(self):
        ct._edge_driver = None
        self.addCleanup(setattr, ct, "_edge_driver", None)
        with TemporaryDirectory() as tmp:
            driver = MagicMock(name="edge-driver")
            with patch.object(ct, "TI_PRIMARY_FILE", Path(tmp) / "ti_primary.json"), patch.object(
                ct, "TI_UNUSUAL_OPTIONS_FILE", Path(tmp) / "ti_unusual_options.json"
            ), patch.object(ct, "RENDER_GRACE_SEC", 0), patch.object(
                ct, "DROPDOWN_REFRESH_SEC", 0
            ), patch.object(ct, "_try_select_timeframe", return_value=True), patch.object(
                ct, "_get_driver", return_value=driver
            ), patch.object(ct, "_extract_tickers", return_value=["AAPL"]):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    results = ct.scrape_tradeideas(scan_keys=["highshortfloat"])

        self.assertEqual(results["highshortfloat"], ["AAPL"])
        driver.get.assert_not_called()
        cdp_urls = [c.args[1]["url"] for c in driver.execute_cdp_cmd.call_args_list]
        self.assertIn(ct.SCANS["highshortfloat"]["url"], cdp_urls)
        self.assertIn("about:blank", cdp_urls)


if __name__ == "__main__":
    unittest.main()