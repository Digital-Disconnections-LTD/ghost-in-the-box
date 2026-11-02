from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from farm import threads_auth, threads_native_auth as native
from farm.instagram_session import _persist_rotated_settings
from farm.accounts import AccountRegistry
from scripts import threads_agent


TOKEN = "IGT:2:" + "A" * 40 + "/+="
OTHER_TOKEN = "IGT:2:" + "B" * 40


def bloks(fields, *, depth=2):
    body = json.dumps(fields)
    for _ in range(depth):
        body = json.dumps({"layout": {"bloks_payload": {"action": body}}})
    return body


def success(**changes):
    fields = {"authorization": "Bearer " + TOKEN, "user_id": "12345", "user_name": "mine"}
    fields.update(changes)
    return bloks(fields)


CHALLENGE = bloks({"status": "fail", "two_factor_required": True,
                   "two_factor_identifier": "private-challenge-identifier"})


class Response:
    def __init__(self, body, status=200):
        self.body = body.encode("utf-8")
        self.status_code = status
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), 41):
            yield self.body[start:start + 41]

    def close(self):
        self.closed = True


class Http:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        print("private-provider-body")
        logging.warning("private-password")
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class NativeAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.creds = self.root / "credentials.json"
        self.creds.write_text(json.dumps({"username": "mine", "password": "private-password"}))
        self.creds.chmod(0o600)
        self.ig_dir = self.root / "ig"
        self.ig_dir.mkdir()
        self.session = self.ig_dir / "mine.json"
        self.session.write_text(json.dumps({"uuids": {
            "android_device_id": "android-0123456789abc"},
            "authorization_data": {"ds_user_id": "12345"}}))
        self.session.chmod(0o600)
        self.inputs = threads_auth.load_inputs(self.creds, self.ig_dir)
        self.auth_dir = self.root / "threads-auth"
        self.destination = self.auth_dir / "mine.json"

    def tearDown(self):
        self.temp.cleanup()

    def login(self, http, **kwargs):
        return native.login(self.inputs, http=http, auth_dir=self.auth_dir, **kwargs)

    def test_login_matches_upstream_threads_headers_form_and_identity(self):
        before = self.session.read_bytes()
        response = Response(success())
        http = Http(response)
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            token = self.login(http)
        self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))
        self.assertEqual(len(http.calls), 1)
        url, request = http.calls[0]
        self.assertEqual(url, "https://i.instagram.com/api/v1/bloks/apps/" + native.LOGIN_APP + "/")
        self.assertEqual(request["headers"], {
            "User-Agent": "Barcelona 289.0.0.77.109 Android",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept-Language": "en-US,en;q=0.9", "X-IG-App-ID": "238260118697367",
            "X-IG-Capabilities": "3brTvx0=", "X-IG-Connection-Type": "WIFI"})
        form = request["data"]
        self.assertEqual(json.loads(form["params"]), {
            "client_input_params": {"password": "private-password", "contact_point": "mine",
                                    "device_id": self.inputs.device_id},
            "server_params": {"credential_type": "password", "device_id": self.inputs.device_id}})
        self.assertEqual(form["bloks_versioning_id"],
                         "00ba6fa565c3c707243ad976fa30a071a625f2a3d158d9412091176fe35027d8")
        self.assertEqual(json.loads(form["bk_client_context"]),
                         {"bloks_version": native.BLOKS_VERSION, "styles_id": "instagram"})
        self.assertEqual(request["timeout"], (10, 30))
        self.assertFalse(request["allow_redirects"])
        self.assertTrue(request["stream"])
        self.assertTrue(response.closed)
        self.assertEqual(token.token, TOKEN)
        self.assertEqual(token.source, threads_auth.THREADS_TOKEN_SOURCE)
        self.assertEqual(threads_auth.load_token("mine", self.auth_dir), token)
        self.assertEqual(json.loads(self.destination.read_text()), token.as_dict())
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.auth_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.session.read_bytes(), before)
        self.assertNotIn("private-password", self.destination.read_text())

    def test_plain_numeric_and_nested_escaped_fields_are_supported(self):
        for depth in (0, 1, 3):
            with self.subTest(depth=depth):
                body = bloks({"authorization": "Bearer " + TOKEN,
                              "ig_user_id": 12345, "user_name": "MiNe"}, depth=depth)
                body = body.replace("/", r"\/")
                token = self.login(Http(Response(body)))
                self.assertEqual(token.token, TOKEN)

    def test_current_six_or_eight_digit_code_is_read_only_after_challenge(self):
        for code in ("123456", "12345678"):
            with self.subTest(length=len(code)):
                http = Http(Response(CHALLENGE), Response(success()))
                reads = []
                def read_code():
                    reads.append(len(http.calls))
                    return code
                token = self.login(http, code_provider=read_code)
                self.assertEqual(reads, [1])
                self.assertEqual(len(http.calls), 2)
                url, request = http.calls[1]
                self.assertTrue(url.endswith(native.TWO_FACTOR_APP + "/"))
                self.assertEqual(json.loads(request["data"]["params"]), {
                    "client_input_params": {"two_factor_identifier": "private-challenge-identifier",
                        "verification_code": code, "verification_method": "3",
                        "device_id": self.inputs.device_id, "username": "mine"},
                    "server_params": {"device_id": self.inputs.device_id,
                                      "two_factor_identifier": "private-challenge-identifier"}})
                self.assertEqual(token.source, threads_auth.THREADS_TOKEN_SOURCE)
                saved = json.loads(self.destination.read_text())
                self.assertNotIn(code, saved.values())
                self.assertNotIn("verification_code", saved)
                self.assertNotIn("private-challenge-identifier", self.destination.read_text())

    def test_success_does_not_consume_stdin_without_a_challenge(self):
        self.login(Http(Response(success(two_factor_required=False, two_factor_identifier=""))),
                   code_provider=lambda: self.fail("code must not be read"))

    def test_successful_second_factor_can_echo_the_consumed_identifier(self):
        http = Http(Response(CHALLENGE), Response(success(two_factor_required=False,
                            two_factor_identifier="private-challenge-identifier")))
        token = self.login(http, verification_code="123456")
        self.assertEqual(token.token, TOKEN)
        self.assertEqual(len(http.calls), 2)

    def test_missing_code_returns_fixed_challenge_status_without_saving(self):
        http = Http(Response(CHALLENGE))
        with self.assertRaises(threads_auth.ThreadsAuthError) as caught:
            self.login(http)
        self.assertEqual((caught.exception.status, caught.exception.needs_code),
                         ("two_factor_required", True))
        self.assertEqual(len(http.calls), 1)
        self.assertFalse(self.destination.exists())

    def test_code_rejected_never_causes_a_third_request_or_save(self):
        for body in (CHALLENGE, '{"status":"fail","error_type":"invalid_code"}'):
            with self.subTest(body_kind=len(body)):
                http = Http(Response(CHALLENGE), Response(body))
                with self.assertRaises(threads_auth.ThreadsAuthError) as caught:
                    self.login(http, verification_code="123456")
                self.assertEqual((caught.exception.status, caught.exception.needs_code),
                                 ("code_rejected", True))
                self.assertEqual(len(http.calls), 2)
                self.assertFalse(self.destination.exists())

    def test_invalid_code_is_rejected_before_any_request(self):
        for code in ("12345", "1234567", "123456789", "１２３４５６", "private-code"):
            http = Http()
            with self.subTest(code_length=len(code)), self.assertRaisesRegex(
                    threads_auth.ThreadsAuthError, "invalid_code_format"):
                self.login(http, verification_code=code)
            self.assertEqual(http.calls, [])

    def test_invalid_code_from_stdin_never_reaches_provider(self):
        http = Http(Response(CHALLENGE))
        with self.assertRaisesRegex(threads_auth.ThreadsAuthError, "invalid_code_format"):
            self.login(http, code_provider=lambda: "private-invalid-code")
        self.assertEqual(len(http.calls), 1)

    def test_provider_failure_statuses_hide_bodies_and_never_retry(self):
        cases = (
            (Response('{"message":"checkpoint_required","secret":"private-password"}'),
             "checkpoint_required"),
            (Response(CHALLENGE + "checkpoint_required"), "checkpoint_required"),
            (Response("private-provider-body", 429), "rate_limited"),
            (Response('{"message":"login_required","logout_reason":8}', 403), "session_suspended"),
            (Response("private-provider-body", 503), "provider_unavailable"),
            (Response("private-provider-body", 302), "login_redirected"),
            (Response(success(), 401), "login_rejected"),
            (Response('{"status":"fail","secret":"private-password"}'), "login_rejected"),
            (Response('{"status":"ok"}'), "token_missing"),
            (Response("private-provider-body"), "token_missing"),
            (RuntimeError("private-password private-provider-body"), "request_failed"),
        )
        for response, expected in cases:
            with self.subTest(expected=expected):
                http = Http(response)
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors), self.assertRaises(
                        threads_auth.ThreadsAuthError) as caught:
                    self.login(http, code_provider=lambda: self.fail("code must not be read"))
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))
                self.assertEqual(len(http.calls), 1)
                self.assertFalse(self.destination.exists())

    def test_response_bound_closes_transport_without_saving(self):
        response = Response("A" * 128)
        with patch.object(native, "_MAX_RESPONSE_BYTES", 80), self.assertRaisesRegex(
                threads_auth.ThreadsAuthError, "response_too_large"):
            self.login(Http(response))
        self.assertTrue(response.closed)
        self.assertFalse(self.destination.exists())

    def test_identity_mismatch_missing_identity_or_multiple_tokens_fail_closed(self):
        bodies = [success(user_id="99999"), success(user_id=""), success(user_name="someone_else"),
                  success(ig_user_id="99999"), success(another_token="Bearer " + OTHER_TOKEN)]
        for body in bodies:
            with self.subTest(size=len(body)), self.assertRaisesRegex(
                    threads_auth.ThreadsAuthError, "identity_mismatch"):
                self.login(Http(Response(body)))
            self.assertFalse(self.destination.exists())

    def test_changed_local_identity_and_nonprivate_session_fail_before_network(self):
        self.session.write_text('{"uuids":{"android_device_id":"android-fffffffffffff"},'
                                '"authorization_data":{"ds_user_id":"12345"}}')
        http = Http()
        with self.assertRaisesRegex(threads_auth.ThreadsAuthError, "identity_mismatch"):
            self.login(http)
        self.session.chmod(0o644)
        with self.assertRaisesRegex(threads_auth.ThreadsAuthError, "local_inputs_invalid"):
            self.login(http)
        self.assertEqual(http.calls, [])

    def test_owned_transport_closes_even_after_provider_failure(self):
        http = Http(Response("private-body", 429))
        with patch("requests.Session", return_value=http), self.assertRaisesRegex(
                threads_auth.ThreadsAuthError, "rate_limited"):
            native.login(self.inputs, auth_dir=self.auth_dir)
        self.assertTrue(http.closed)

    def test_native_token_survives_legacy_save_and_instagram_refresh(self):
        native_token = self.login(Http(Response(success())))
        before = self.destination.read_bytes()
        old_ig_token = replace(native_token, token=OTHER_TOKEN,
                               source=threads_auth.INSTAGRAM_TOKEN_SOURCE)
        threads_auth.save_token(old_ig_token, self.auth_dir)
        self.assertEqual(self.destination.read_bytes(), before)

        settings = {"uuids": {"android_device_id": self.inputs.device_id},
                    "authorization_data": {"ds_user_id": self.inputs.user_id,
                                           "sessionid": "rotated-private-session"}}
        class Client:
            android_device_id = self.inputs.device_id
            user_id = self.inputs.user_id
            authorization = "Bearer " + OTHER_TOKEN
            authorization_data = settings["authorization_data"]
            def load_settings(self, path, **kwargs):
                pass
            def login(self, *args, **kwargs):
                return True
            def get_settings(self):
                return settings
        threads_auth.login(self.inputs, force_refresh=True,
                           client_factory=Client, auth_dir=self.auth_dir)
        self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual(json.loads(self.session.read_text()), settings)

    def test_instagram_read_rotates_settings_without_replacing_native_bearer(self):
        native_token = self.login(Http(Response(success())))
        loaded_mtime_ns = self.session.stat().st_mtime_ns
        settings = {"uuids": {"android_device_id": self.inputs.device_id},
                    "authorization_data": {"ds_user_id": self.inputs.user_id,
                                           "sessionid": "rotated-private-session"}}
        client = SimpleNamespace(authorization="Bearer " + OTHER_TOKEN,
                                 get_settings=lambda: settings)
        with patch("farm.instagram_session.threads_auth.load_token", return_value=native_token), \
                patch("farm.instagram_session.threads_auth.save_token") as save:
            _persist_rotated_settings(client, self.session, loaded_mtime_ns, "mine")
        save.assert_not_called()
        self.assertEqual(json.loads(self.session.read_text()), settings)

    def test_old_bearer_file_defaults_to_instagram_source_and_can_rotate(self):
        token = self.login(Http(Response(success())))
        old = token.as_dict()
        del old["source"]
        self.destination.write_text(json.dumps(old))
        loaded = threads_auth.load_token("mine", self.auth_dir)
        self.assertEqual(loaded.source, threads_auth.INSTAGRAM_TOKEN_SOURCE)
        threads_auth.save_token(replace(loaded, token=OTHER_TOKEN), self.auth_dir)
        self.assertEqual(threads_auth.load_token("mine", self.auth_dir).token, OTHER_TOKEN)

    def test_failed_atomic_replace_preserves_old_token_and_removes_temporary_file(self):
        self.login(Http(Response(success())))
        before = self.destination.read_bytes()
        with patch("farm.threads_auth.os.replace", side_effect=OSError("private-error")), \
                self.assertRaisesRegex(threads_auth.ThreadsAuthError, "token_save_failed"):
            self.login(Http(Response(success(authorization="Bearer " + OTHER_TOKEN))))
        self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual(list(self.auth_dir.iterdir()), [self.destination])

    def mobile_cli(self, argv, run, stdin=None):
        registry = AccountRegistry(self.root / "accounts.json")
        for platform in ("instagram", "threads"):
            registry.register(platform=platform, account="mine", kind="instagrapi_settings",
                              session_file=self.session, account_key="meta:mine")
        agent = threads_agent.ThreadsAgent(registry=registry, db=self.root / "outbox.sqlite3",
                                           auth_dir=self.auth_dir, reader=object())
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(threads_agent, "ThreadsAgent", return_value=agent), \
                patch.object(threads_agent, "load_inputs", return_value=self.inputs), \
                patch.object(threads_agent, "native_mobile_login", side_effect=run), \
                patch.object(threads_agent.sys, "stdin", stdin or io.StringIO("")), \
                redirect_stdout(output), redirect_stderr(errors):
            status = threads_agent.main(["--account", "mine", "mobile-login", "--native", *argv])
        return status, json.loads(output.getvalue()), errors.getvalue()

    def test_cli_reads_one_code_only_when_challenged_and_prints_no_secrets(self):
        http = Http(Response(CHALLENGE), Response(success()))
        def run(inputs, **kwargs):
            return native.login(inputs, http=http, **kwargs)
        stdin = io.StringIO("123456\nunused\n")
        status, result, errors = self.mobile_cli(["--verification-code-stdin"], run, stdin)
        self.assertEqual(status, 0)
        self.assertEqual((result["ok"], result["status"], result["needs_code"]),
                         (True, "saved", False))
        self.assertEqual(errors, "")
        self.assertEqual(stdin.read(), "unused\n")
        self.assertTrue(self.destination.is_file())
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_cli_failed_request_has_only_a_fixed_public_status(self):
        def run(inputs, **kwargs):
            return self.login(Http(RuntimeError("private-password private-provider-body")),
                              code_provider=kwargs.get("code_provider"))
        status, result, _ = self.mobile_cli([], run)
        self.assertEqual(status, 1)
        self.assertEqual((result["ok"], result["status"], result["needs_code"]),
                         (False, "request_failed", False))
        self.assertEqual(result["error"]["code"], "request_failed")
        self.assertNotIn("private", json.dumps(result))

    def test_cli_rejects_credentials_for_another_account(self):
        other = replace(self.inputs, username="someone")
        output = io.StringIO()
        registry = AccountRegistry(self.root / "accounts.json")
        for platform in ("instagram", "threads"):
            registry.register(platform=platform, account="mine", kind="instagrapi_settings",
                              session_file=self.session, account_key="meta:mine")
        agent = threads_agent.ThreadsAgent(registry=registry, auth_dir=self.auth_dir, reader=object())
        with patch.object(threads_agent, "ThreadsAgent", return_value=agent), \
                patch.object(threads_agent, "load_inputs", return_value=other), \
                patch.object(threads_agent, "native_mobile_login") as login, redirect_stdout(output):
            status = threads_agent.main(["--account", "mine", "mobile-login", "--native"])
        self.assertEqual(status, 4)
        self.assertEqual(json.loads(output.getvalue())["status"], "credentials_account_mismatch")
        login.assert_not_called()

if __name__ == "__main__":
    unittest.main()
