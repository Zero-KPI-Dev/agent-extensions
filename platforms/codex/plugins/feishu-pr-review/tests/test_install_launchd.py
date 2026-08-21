from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import install_launchd


class InstallLaunchdTests(unittest.TestCase):
    def test_shared_app_server_gets_a_safe_file_descriptor_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "app-server.plist"
            config = SimpleNamespace(
                app_server_launchd_label="com.example.app-server",
                codex_binary="codex",
                log_dir=root / "logs",
                ensure_directories=lambda: (root / "logs").mkdir(parents=True, exist_ok=True),
            )

            with (
                patch.object(install_launchd.sys, "platform", "darwin"),
                patch.object(install_launchd, "app_server_target_path", return_value=target),
                patch.object(install_launchd, "resolve_app_server_executable", return_value="/bin/echo"),
            ):
                install_launchd.install_shared_app_server(config, load=False)

            with target.open("rb") as handle:
                plist = plistlib.load(handle)

        self.assertEqual(
            plist["SoftResourceLimits"]["NumberOfFiles"],
            install_launchd.APP_SERVER_NOFILE_SOFT_LIMIT,
        )
        self.assertGreaterEqual(install_launchd.APP_SERVER_NOFILE_SOFT_LIMIT, 4096)


if __name__ == "__main__":
    unittest.main()
