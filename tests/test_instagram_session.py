import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from farm.instagram_session import InstagramSessionError, read_with_session


class LoginRequired(Exception):
    pass


class Registry:
    def __init__(self, path):
        self.path = path

    def status(self, platform, account):
        return {"kind": "instagrapi_settings", "session_exists": True,
                "session_private": True, "session_file": str(self.path)}


class Client:
    def __init__(self, wrong_account=False):
        self.wrong_account = wrong_account
        self.user_id = "123"
        self.version = ""

    def load_settings(self, path):
        self.version = json.loads(path.read_text())["version"]

    def account_info(self):
        if self.version == "old":
            raise LoginRequired("private provider body")
        return SimpleNamespace(pk="123", username="someone_else" if
                               self.wrong_account else "writer")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "writer.json"
        self.path.write_text('{"version":"old"}')
        self.path.chmod(0o600)
        self.registry = Registry(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_rejected_session_refreshes_once_then_reads_with_latest_settings(self):
        refreshes = []
        def refresh():
            refreshes.append(1)
            self.path.write_text('{"version":"new"}')
        result = read_with_session("writer", lambda client: client.version,
            registry=self.registry, client_factory=Client, refresh=refresh)
        self.assertEqual(result, "new")
        self.assertEqual(refreshes, [1])
        self.assertFalse(self.path.with_name("writer.json.recovery.json").exists())

    def test_failed_recovery_cools_down_retries_without_leaking_provider_text(self):
        refreshes = []
        def refresh():
            refreshes.append(1)
            raise LoginRequired("private provider body")
        for expected in ("LoginRequired", "auth_cooldown"):
            with self.assertRaises(InstagramSessionError) as caught:
                read_with_session("writer", lambda client: None,
                    registry=self.registry, client_factory=Client, refresh=refresh)
            self.assertEqual(str(caught.exception), expected)
            self.assertNotIn("private provider body", str(caught.exception))
        self.assertEqual(refreshes, [1])
        cooldown = self.path.with_name("writer.json.recovery.json")
        self.assertEqual(cooldown.stat().st_mode & 0o077, 0)

    def test_wrong_saved_account_is_not_refreshed(self):
        self.path.write_text('{"version":"new"}')
        refreshes = []
        with self.assertRaisesRegex(InstagramSessionError, "account_mismatch"):
            read_with_session("writer", lambda client: None,
                registry=self.registry, client_factory=lambda: Client(True),
                refresh=lambda: refreshes.append(1))
        self.assertEqual(refreshes, [])

    def test_successful_read_persists_a_rotated_cookie_for_next_process(self):
        self.path.write_text('{"version":"new"}')
        class RotatingClient(Client):
            def get_settings(self):
                return {"version": "new", "cookie": "rotated"}
        read_with_session("writer", lambda client: client.version,
            registry=self.registry, client_factory=RotatingClient,
            refresh=lambda: self.fail("valid login should not refresh"))
        self.assertEqual(json.loads(self.path.read_text())["cookie"], "rotated")

    def test_rotated_authorization_updates_existing_linked_threads_bearer(self):
        self.path.write_text('{"version":"new"}')
        token = "IGT:2:" + "A" * 32
        class RotatingClient(Client):
            authorization = "Bearer " + token
            def get_settings(self):
                return {"version": "new", "authorization_data": {"ds_user_id": "123"},
                        "uuids": {"android_device_id": "android-0123456789abcdef"}}
        old = SimpleNamespace(token="IGT:2:" + "B" * 32, user_id="123",
                              device_id="android-0123456789abcdef")
        with patch("farm.instagram_session.threads_auth.load_token", return_value=old), \
             patch("farm.instagram_session.threads_auth.save_token") as save:
            read_with_session("writer", lambda client: client.version,
                registry=self.registry, client_factory=RotatingClient,
                refresh=lambda: self.fail("valid login should not refresh"))
        self.assertEqual(save.call_count, 1)
        replacement = save.call_args.args[0]
        self.assertEqual((replacement.token, replacement.user_id, replacement.username),
                         (token, "123", "writer"))


if __name__ == "__main__":
    unittest.main()
