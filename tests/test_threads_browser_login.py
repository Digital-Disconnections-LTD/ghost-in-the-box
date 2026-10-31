from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import threads_browser_login


class BrowserLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.root / "browser"
        (self.base / "node-v24.22.1-linux-x64/bin").mkdir(parents=True)
        (self.base / "node-v24.22.1-linux-x64/bin/node").touch()
        (self.base / "source/build/browser").mkdir(parents=True)
        (self.base / "source/build/browser/session.js").touch()
        self.active = self.base / "profile/mine"
        self.active.mkdir(parents=True)
        (self.active / "old-marker").touch()
        self.creds = self.root / "ig.json"
        self.creds.write_text(json.dumps({"username": "mine", "password": "private-password"}))
        os.chmod(self.creds, 0o600)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, status, *, desktop=False):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if status == "saved":
                profile = Path(kwargs["env"]["THREADS_PROFILE_DIR"])
                (profile / "old-marker").unlink()
                (profile / "new-marker").touch()
            return subprocess.CompletedProcess(argv, 0,
                json.dumps({"status": status, "needs_code": status == "two_factor_required"}) + "\n", "")

        output = io.StringIO()
        with patch.object(threads_browser_login, "BASE", self.base), \
                patch.object(threads_browser_login, "CREDS", self.creds), \
                patch.object(threads_browser_login.shutil, "which",
                             side_effect=lambda name: "xvfb-run" if name == "xvfb-run" and not desktop else None), \
                patch.object(threads_browser_login.sys, "platform", "darwin" if desktop else "linux"), \
                patch.object(threads_browser_login.subprocess, "run", side_effect=runner), \
                redirect_stdout(output):
            code = threads_browser_login.main(["--account", "mine"])
        return code, json.loads(output.getvalue()), calls

    def test_success_swaps_profile_only_after_verified_login(self):
        code, result, calls = self.invoke("saved")
        self.assertEqual((code, result["status"]), (0, "saved"))
        self.assertTrue((self.active / "new-marker").is_file())
        self.assertFalse((self.active / "old-marker").exists())
        self.assertNotIn("private-password", " ".join(calls[0][0]))
        self.assertEqual(json.loads(calls[0][1]["input"])["password"], "private-password")
        self.assertNotIn("private-password", json.dumps(result))

    def test_otp_request_keeps_existing_profile(self):
        code, result, _ = self.invoke("two_factor_required")
        self.assertEqual((code, result["status"], result["needs_code"]),
                         (2, "two_factor_required", True))
        self.assertTrue((self.active / "old-marker").is_file())

    def test_provider_throttle_keeps_existing_profile(self):
        code, result, _ = self.invoke("provider_throttled")
        self.assertEqual((code, result["status"], result["needs_code"]),
                         (2, "provider_throttled", False))
        self.assertTrue((self.active / "old-marker").is_file())

    def test_macos_login_uses_the_same_native_display_path_as_publishing(self):
        code, result, calls = self.invoke("saved", desktop=True)
        self.assertEqual((code, result["status"]), (0, "saved"))
        self.assertNotIn("xvfb-run", calls[0][0])
        self.assertNotIn("-a", calls[0][0])


if __name__ == "__main__":
    unittest.main()
