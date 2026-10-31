from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from farm.threads_auth import ThreadsAuthError, load_inputs, login, save_token


TOKEN = "IGT:2:" + "a" * 80


class TwoFactorRequired(Exception):
    pass


class ChallengeRequired(Exception):
    pass


class LoginRequired(Exception):
    pass


class FakeClient:
    def __init__(self, *, user_id="12345", username="mine", device_id="android-0123456789abc",
                 authorization="Bearer " + TOKEN, login_error=None, account_error=None):
        self.android_device_id = device_id
        self.user_id = user_id
        self.authorization_data = {"ds_user_id": user_id}
        self.authorization = authorization
        self.result_username = username
        self.login_error = login_error
        self.account_error = account_error
        self.calls = []

    def load_settings(self, path, *, override_app_version):
        self.calls.append(("load_settings", path, override_app_version))
        Path(path).read_text()

    def login(self, username, password, *, relogin, verification_code):
        self.calls.append(("login", username, relogin, verification_code))
        print("secret-password")
        if self.login_error:
            raise self.login_error
        return True

    def account_info(self):
        self.calls.append(("account_info",))
        if self.account_error:
            raise self.account_error
        return SimpleNamespace(username=self.result_username)

    def get_settings(self):
        self.calls.append(("get_settings",))
        return {"uuids": {"android_device_id": self.android_device_id},
                "authorization_data": {"ds_user_id": self.user_id,
                                       "sessionid": "new-session-private"}}


class ThreadsAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.creds = self.root / "ig.json"
        self.creds.write_text(json.dumps({"username": "mine", "password": "secret-password"}))
        self.creds.chmod(0o600)
        self.ig_dir = self.root / "ig"
        self.ig_dir.mkdir()
        self.session = self.ig_dir / "mine.json"
        self.session.write_text(json.dumps({"uuids": {
            "android_device_id": "android-0123456789abc"},
            "authorization_data": {"ds_user_id": "12345"}}))
        self.session.chmod(0o600)
        self.inputs = load_inputs(self.creds, self.ig_dir)

    def tearDown(self):
        self.temp.cleanup()

    def test_loads_private_existing_inputs_and_stable_device(self):
        self.assertEqual(self.inputs.username, "mine")
        self.assertEqual(self.inputs.password, "secret-password")
        self.assertEqual(self.inputs.device_id, "android-0123456789abc")
        self.assertEqual(self.inputs.user_id, "12345")
        self.assertEqual(self.inputs.session_path, self.session)
        self.session.chmod(0o644)
        with self.assertRaises(ThreadsAuthError) as caught:
            load_inputs(self.creds, self.ig_dir)
        self.assertEqual(caught.exception.status, "local_inputs_invalid")

    def test_reuses_saved_ig_authorization_without_a_second_login(self):
        fake = FakeClient()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            token = login(self.inputs, client_factory=lambda: fake,
                          auth_dir=self.root / "threads-auth")
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertEqual(fake.calls[0], ("load_settings", self.session, True))
        self.assertEqual(fake.calls[1], ("account_info",))
        self.assertFalse(any(call[0] == "login" for call in fake.calls))
        self.assertEqual(token.token, TOKEN)
        self.assertNotIn("verification_code", token.as_dict())
        path = self.root / "threads-auth/mine.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(path.read_text()), token.as_dict())
        self.assertNotIn("secret-password", path.read_text())
        self.assertEqual(json.loads(self.session.read_text())["authorization_data"]["ds_user_id"],
                         "12345")
        self.assertEqual(self.session.stat().st_mode & 0o777, 0o600)

    def test_rejected_session_refreshes_both_private_files_under_one_lock(self):
        fake = FakeClient(account_error=LoginRequired("raw provider body"))
        token = login(self.inputs, client_factory=lambda: fake,
                      auth_dir=self.root / "threads-auth")
        self.assertIn(("login", "mine", True, ""), fake.calls)
        self.assertEqual(json.loads(self.session.read_text())["authorization_data"]["sessionid"],
                         "new-session-private")
        self.assertEqual(self.session.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.root / "threads-auth/mine.json").read_text()),
                         token.as_dict())
        self.assertEqual((self.ig_dir / "mine.json.lock").stat().st_mode & 0o777, 0o600)

    def test_force_refresh_rotates_both_files_even_if_saved_session_reads(self):
        fake = FakeClient()
        login(self.inputs, force_refresh=True, client_factory=lambda: fake,
              auth_dir=self.root / "threads-auth")
        self.assertIn(("login", "mine", True, ""), fake.calls)
        self.assertFalse(any(call[0] == "account_info" for call in fake.calls))
        self.assertTrue((self.root / "threads-auth/mine.json").is_file())

    def test_two_factor_and_checkpoint_are_sanitized(self):
        for error, expected_status, needs_code in (
            (TwoFactorRequired("secret-password raw provider body"), "two_factor_required", True),
            (ChallengeRequired("secret-password raw provider body"), "checkpoint_required", False),
        ):
            with self.subTest(expected_status=expected_status):
                fake = FakeClient(login_error=error,
                                  account_error=LoginRequired("session rejected"))
                with self.assertRaises(ThreadsAuthError) as caught:
                    login(self.inputs, client_factory=lambda: fake,
                          auth_dir=self.root / "threads-auth")
                self.assertEqual(caught.exception.status, expected_status)
                self.assertEqual(caught.exception.needs_code, needs_code)
                self.assertNotIn("secret-password", str(caught.exception))

    def test_identity_mismatch_and_missing_bearer_fail_closed(self):
        for fake, expected in (
            (FakeClient(user_id="99999"), "identity_mismatch"),
            (FakeClient(username="other"), "identity_mismatch"),
            (FakeClient(device_id="android-deadbeefdead1"), "identity_mismatch"),
            (FakeClient(authorization=""), "token_missing"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaises(ThreadsAuthError) as caught:
                    login(self.inputs, client_factory=lambda fake=fake: fake)
                self.assertEqual(caught.exception.status, expected)

    def test_invalid_code_is_rejected_before_client_creation(self):
        with self.assertRaises(ThreadsAuthError) as caught:
            login(self.inputs, verification_code="12-not-a-code",
                  client_factory=lambda: self.fail("client should not be created"))
        self.assertEqual(caught.exception.status, "invalid_code_format")


if __name__ == "__main__":
    unittest.main()
