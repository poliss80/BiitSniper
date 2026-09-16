"""Focused tests for Trade Ideas Edge driver compatibility decisions."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine.ti.capture_tradeideas as capture_tradeideas


class TradeIdeasDriverVersionTests(unittest.TestCase):

    def test_major_version_parser_accepts_driver_and_edge_output(self):
        self.assertEqual(
            capture_tradeideas._major_version_from_text(
                "MSEdgeDriver 153.0.1234.5 (abc)"
            ),
            153,
        )
        self.assertEqual(
            capture_tradeideas._major_version_from_text(
                "Microsoft Edge 151.0.1901.2"
            ),
            151,
        )
        self.assertIsNone(capture_tradeideas._major_version_from_text("unknown"))

    def test_select_compatible_driver_ignores_mismatched_and_unknown_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [str(Path(directory) / name) for name in ("old.exe", "new.exe")]
            for path in paths:
                Path(path).touch()
            with patch.object(
                capture_tradeideas,
                "_executable_major_version",
                side_effect=lambda path: {paths[0]: 151, paths[1]: 153}[path],
            ):
                self.assertEqual(
                    capture_tradeideas._select_compatible_edgedriver(paths, 153),
                    paths[1],
                )
                self.assertIsNone(
                    capture_tradeideas._select_compatible_edgedriver(paths, 152)
                )
                self.assertIsNone(
                    capture_tradeideas._select_compatible_edgedriver(paths, None)
                )

    def test_find_existing_driver_does_not_return_mismatched_repo_driver(self):
        repo_driver = capture_tradeideas.REPO_ROOT / ".drivers" / "msedgedriver.exe"
        with patch.object(capture_tradeideas, "_installed_edge_major_version", return_value=153), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(capture_tradeideas, "_executable_major_version", return_value=151), \
             patch("glob.glob", return_value=[]):
            self.assertIsNone(capture_tradeideas._find_existing_edgedriver())
            self.assertTrue(str(repo_driver).endswith("msedgedriver.exe"))


if __name__ == "__main__":
    unittest.main()