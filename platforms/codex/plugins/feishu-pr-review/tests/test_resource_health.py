from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server import resource_health


class ResourceHealthTests(unittest.TestCase):
    def test_launchd_pid_parser_does_not_return_other_fields(self) -> None:
        result = subprocess.CompletedProcess(
            ["launchctl"],
            0,
            stdout="environment = {\n  SECRET => hidden\n}\n  pid = 4321\n",
            stderr="",
        )
        with patch("server.resource_health.subprocess.run", return_value=result):
            self.assertEqual(resource_health._launchd_pid("com.example"), 4321)

    def test_numeric_fd_count_ignores_memory_mappings(self) -> None:
        result = subprocess.CompletedProcess(
            ["lsof"],
            0,
            stdout="p4321\nfcwd\nftxt\nf0r\nf1w\nf19u\n",
            stderr="",
        )
        with (
            patch("server.resource_health.shutil.which", return_value="/usr/sbin/lsof"),
            patch("server.resource_health.subprocess.run", return_value=result),
        ):
            self.assertEqual(resource_health._numeric_fd_count(4321), 3)

    def test_newer_plist_requires_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            plist = home / "Library" / "LaunchAgents" / "com.example.plist"
            plist.parent.mkdir(parents=True)
            plist.write_text("new", encoding="utf-8")
            result = subprocess.CompletedProcess(
                ["ps"],
                0,
                stdout="Fri Aug 21 13:35:53 2026\n",
                stderr="",
            )
            config = SimpleNamespace(app_server_launchd_label="com.example")
            with (
                patch("server.resource_health.Path.home", return_value=home),
                patch("server.resource_health.subprocess.run", return_value=result),
                patch("server.resource_health.datetime") as datetime_mock,
            ):
                datetime_mock.strptime.return_value.timestamp.return_value = plist.stat().st_mtime - 10
                self.assertTrue(resource_health._restart_required(config, 4321))


if __name__ == "__main__":
    unittest.main()
