from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import ig_probe, threads_browser_setup


class PublicSetupTests(unittest.TestCase):
    def test_instagram_rejects_public_credentials_before_constructing_a_client(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "login.json"
            path.write_text(json.dumps({"username": "example_creator", "password": "TEST_ONLY"}))
            path.chmod(0o644)
            with patch("sys.argv", ["login", "--credentials", str(path)]), \
                    patch.object(ig_probe, "Client") as client, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(ig_probe.main(), 2)
            client.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["error"], "private_credentials_unavailable")

    def test_instagram_rejects_unsafe_account_paths(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "login.json"
            path.write_text(json.dumps({"username": "../outside", "password": "TEST_ONLY"}))
            path.chmod(0o600)
            with patch("sys.argv", ["login", "--credentials", str(path)]), \
                    patch.object(ig_probe, "Client") as client, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(ig_probe.main(), 2)
            client.assert_not_called()

    def test_browser_setup_reuses_installed_node_and_npm_on_macos(self):
        with TemporaryDirectory() as folder:
            node = Path(folder) / "node"
            node.touch()
            (Path(folder) / "npm").touch()
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(threads_browser_setup.shutil, "which", return_value=str(node)), \
                    patch.object(threads_browser_setup.sys, "platform", "darwin"), \
                    patch.object(threads_browser_setup, "urlopen") as download:
                self.assertEqual(threads_browser_setup._node(), node)
            download.assert_not_called()

    def test_browser_setup_never_downloads_linux_binaries_on_other_platforms(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(threads_browser_setup.shutil, "which", return_value=None), \
                patch.object(threads_browser_setup.sys, "platform", "darwin"), \
                patch.object(threads_browser_setup, "urlopen") as download:
            with self.assertRaisesRegex(RuntimeError, "install_node_and_npm_for_this_platform"):
                threads_browser_setup._node()
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
