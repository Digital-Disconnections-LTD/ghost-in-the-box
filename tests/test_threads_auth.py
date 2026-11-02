from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import contextmanager
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from farm import threads_auth
from farm.threads_auth import (ThreadsAuthError, ThreadsToken, load_inputs, load_token, login,
                               read_verification_code, save_token, verify_bearer_identity)


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



@contextmanager
def without_fchmod():
    saved = os.fchmod
    del os.fchmod
    try:
        yield
    finally:
        os.fchmod = saved


class Response:
    def __init__(self, status_code, payload):
        self.status_code, self.payload = status_code, payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Http:
    def __init__(self, response):
        self.response, self.requests = response, []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class BearerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.token = ThreadsToken(TOKEN, "12345", "android-0123456789abc", "mine",
                                  "2026-10-26T00:00:00+00:00")

    def test_server_identity_must_match_the_token_account(self):
        http = Http(Response(200, {"user": {"pk": 12345, "username": "Mine"}, "status": "ok"}))
        self.assertEqual(verify_bearer_identity(self.token, http=http),
                         {"id": "12345", "username": "Mine"})
        url, kwargs = http.requests[0]
        self.assertEqual(url, threads_auth.IDENTITY_URL)
        self.assertEqual(kwargs["params"], {"edit": "true"})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + TOKEN)

    def test_caller_headers_cannot_swap_the_checked_bearer(self):
        http = Http(Response(200, {"user": {"pk": 12345, "username": "mine"}}))
        verify_bearer_identity(self.token, http=http,
                               headers={"Authorization": "Bearer other", "X-Extra": "1"})
        headers = http.requests[0][1]["headers"]
        self.assertEqual((headers["Authorization"], headers["X-Extra"]), ("Bearer " + TOKEN, "1"))

    def test_failures_are_fixed_codes_without_provider_text(self):
        cases = (
            (Response(200, {"user": {"pk": 999, "username": "mine"}}), "identity_mismatch"),
            (Response(200, {"user": {"pk": 12345, "username": "someone"}}), "identity_mismatch"),
            (Response(401, {}), "bearer_rejected"),
            (Response(200, {"message": "login_required"}), "bearer_rejected"),
            (Response(429, {}), "rate_limited"),
            (Response(500, {}), "identity_unavailable"),
            (Response(200, ValueError("PRIVATE body")), "identity_unavailable"),
            (Response(200, {"user": {"username": "mine"}}), "identity_unavailable"),
            (RuntimeError("PRIVATE transport"), "identity_unavailable"),
        )
        for response, status in cases:
            with self.subTest(status=status), self.assertRaises(ThreadsAuthError) as raised:
                verify_bearer_identity(self.token, http=Http(response))
            self.assertEqual(raised.exception.status, status)
            self.assertNotIn("PRIVATE", str(raised.exception))


class VerificationCodeTests(unittest.TestCase):
    def test_reads_one_line_from_a_pipe(self):
        stdin = io.StringIO("123456\nnext\n")
        self.assertEqual(read_verification_code(stdin=stdin), "123456")
        self.assertEqual(stdin.read(), "next\n")

    def test_invalid_codes_are_rejected(self):
        for value in ("12345\n", "abcdef\n", "１２３４５６\n", "\n"):
            with self.subTest(value=value), self.assertRaises(ThreadsAuthError) as raised:
                read_verification_code(stdin=io.StringIO(value))
            self.assertEqual(raised.exception.status, "invalid_code_format")

    def test_terminal_input_is_hidden(self):
        terminal = io.StringIO()
        terminal.isatty = lambda: True
        with patch("getpass.getpass", return_value="87654321") as hidden:
            self.assertEqual(read_verification_code(stdin=terminal), "87654321")
        hidden.assert_called_once()


class WindowsFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auth_dir = Path(self.temp.name) / "threads-auth"
        self.token = ThreadsToken(TOKEN, "12345", "android-0123456789abc", "mine",
                                  "2026-10-26T00:00:00+00:00")

    def test_token_save_uses_acl_protection_when_fchmod_is_missing(self):
        with without_fchmod(), patch("farm.session_files.protect") as protect:
            path = save_token(self.token, self.auth_dir)
        protect.assert_called_once()
        self.assertTrue(path.is_file())
        self.assertEqual(list(self.auth_dir.glob(".threads-auth-*")), [])

    def test_failed_acl_protection_reports_a_save_failure_and_cleans_up(self):
        import subprocess
        with without_fchmod(), patch("farm.session_files.protect",
                                     side_effect=subprocess.CalledProcessError(1, ["icacls"])):
            with self.assertRaises(ThreadsAuthError) as raised:
                save_token(self.token, self.auth_dir)
        self.assertEqual(raised.exception.status, "token_save_failed")
        self.assertEqual(list(self.auth_dir.glob(".threads-auth-*")), [])

    def test_windows_does_not_reject_files_for_posix_mode_bits(self):
        path = save_token(self.token, self.auth_dir)
        path.chmod(0o644)
        with self.assertRaises(ThreadsAuthError):
            load_token("mine", self.auth_dir)
        with patch.object(threads_auth.os, "name", "nt"):
            self.assertEqual(load_token("mine", self.auth_dir).user_id, "12345")


if __name__ == "__main__":
    unittest.main()
