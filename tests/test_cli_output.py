import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import requests

from farm import cli_output, threads_cli
from farm.adapters import AuthUnavailable, DependencyUnavailable, PublishUncertain, ReadFailure
from farm.cli_output import (
    BodyInputError, add_account_argument, attach_error, exception_failure, failure, guarded, read_body,
    require_account,
)
from farm.instagram_session import InstagramSessionError
from farm.outbox import OutboxError


class EnvelopeTests(unittest.TestCase):
    def test_failure_keeps_legacy_keys_and_always_has_a_hint(self):
        result = failure("not_found", "No job.", None, status="not_found", job_id="x")
        self.assertEqual(result["ok"], False)
        self.assertEqual((result["status"], result["job_id"]), ("not_found", "x"))
        self.assertEqual(set(result["error"]), {"code", "message", "hint"})
        self.assertEqual(result["error"]["hint"], "ghost-in-the-box doctor")

    def test_legacy_keys_named_message_and_hint_do_not_collide(self):
        result = failure("code", "text", "next", message="text", hint="next")
        self.assertEqual((result["message"], result["hint"]), ("text", "next"))
        self.assertEqual(result["error"], {"code": "code", "message": "text", "hint": "next"})

    def test_attach_error_fills_a_missing_hint_from_the_fallback(self):
        result = {"ok": False, "status": "mystery"}
        code = attach_error(result, {}, fallback_hint="ghost-in-the-box threads --help")
        self.assertEqual(code, cli_output.EXIT_RUNTIME)
        self.assertEqual(result["error"]["hint"], "ghost-in-the-box threads --help")
        result = {"ok": False, "status": "uncertain"}
        self.assertEqual(attach_error(result, {"uncertain": (1, "maybe sent", None)}), 3)
        self.assertTrue(result["error"]["hint"])

    def test_threads_cli_re_exports_the_same_helpers(self):
        for name in ("attach_error", "read_body", "BodyInputError", "EXIT_UNCERTAIN", "MAX_BODY_BYTES"):
            self.assertIs(getattr(threads_cli, name), getattr(cli_output, name))


class ReadBodyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_exactly_one_source_is_required(self):
        for body, body_file in ((None, None), ("a", "b")):
            with self.assertRaisesRegex(BodyInputError, "body_input_required"):
                read_body(body, body_file)

    def test_stdin_dash_and_missing_files_have_their_own_codes(self):
        self.assertEqual(read_body(None, "-", stdin=io.StringIO("from stdin")), "from stdin")
        with self.assertRaises(BodyInputError) as raised:
            read_body(None, str(self.root / "absent.txt"))
        self.assertEqual(str(raised.exception), "file_not_found")
        self.assertEqual(raised.exception.path, str(self.root / "absent.txt"))

    def test_limits_and_blank_text(self):
        with self.assertRaisesRegex(BodyInputError, "text_must_contain_1_to_500_characters"):
            read_body("x" * 501, None)
        self.assertEqual(len(read_body("x" * 3000, None, limit=None)), 3000)
        with self.assertRaisesRegex(BodyInputError, "text_must_not_be_blank"):
            read_body("   ", None, limit=None)
        self.assertEqual(read_body("", None, limit=2200, allow_blank=True), "")


class ExceptionMappingTests(unittest.TestCase):
    def mapped(self, exc, **options):
        code, payload = exception_failure(exc, command="outbox", **options)
        self.assertEqual(set(payload["error"]), {"code", "message", "hint"})
        self.assertTrue(payload["error"]["hint"])
        return code, payload

    def test_exit_codes_follow_the_contract(self):
        cases = [
            (FileNotFoundError(2, "No such file", "/nope.txt"), 2, "file_not_found"),
            (sqlite3.OperationalError("locked"), 1, "database_error"),
            (AuthUnavailable("LinkedIn browser login is required"), 4, "auth_unavailable"),
            (DependencyUnavailable("missing", install_hint="pip install x"), 4, "dependency_unavailable"),
            (PublishUncertain("timeout"), 3, "publish_uncertain"),
            (ReadFailure("HTTP 500"), 1, "read_unavailable"),
            (ValueError("limit must be 1..100"), 2, "invalid_input"),
            (RuntimeError("boom"), 1, "internal_error"),
            (requests.ConnectionError("refused"), 1, "network_unavailable"),
            (BodyInputError("text_must_contain_1_to_500_characters"), 2, "text_must_contain_1_to_500_characters"),
        ]
        for exc, exit_code, error_code in cases:
            with self.subTest(exc=type(exc).__name__):
                code, payload = self.mapped(exc)
                self.assertEqual((code, payload["error"]["code"]), (exit_code, error_code))
                self.assertEqual(payload["error_type"], type(exc).__name__)

    def test_hints_are_specific_where_the_cause_is_known(self):
        self.assertEqual(self.mapped(DependencyUnavailable("m", install_hint="pip install x"))[1]["error"]["hint"],
                         "pip install x")
        login = "ghost-in-the-box login start --platform linkedin --account me"
        self.assertEqual(self.mapped(AuthUnavailable("no"), auth_hint=login)[1]["error"]["hint"], login)
        self.assertIn("/nope.txt", self.mapped(FileNotFoundError(2, "x", "/nope.txt"))[1]["error"]["message"])
        self.assertIn("the outbox database", self.mapped(sqlite3.Error("x"), database="outbox")[1]["error"]["message"])
        self.assertEqual(self.mapped(sqlite3.Error("x"), database="outbox")[1]["error"]["code"],
                         "outbox_database_error")

    def test_own_coded_errors_keep_their_code_and_hint(self):
        code, payload = self.mapped(OutboxError("job not found", code="not_found", hint="outbox list"))
        self.assertEqual((code, payload["error"]["code"], payload["error"]["hint"]), (2, "not_found", "outbox list"))
        code, payload = self.mapped(OutboxError("held", code="lease_lost"))
        self.assertEqual(code, 1)
        code, payload = self.mapped(InstagramSessionError("session_unavailable", account="me"))
        self.assertEqual(code, 4)
        self.assertIn("--account me", payload["error"]["hint"])

    def test_provider_text_is_withheld_when_asked(self):
        secret = ValueError("cookie sessionid=SECRET_SENTINEL")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(self.mapped(secret, reveal=False)[1]))
        self.assertNotIn("boom-detail", json.dumps(self.mapped(RuntimeError("boom-detail"))[1]))


class GuardedTests(unittest.TestCase):
    def run_main(self, main, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(*args)
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_argparse_usage_errors_still_exit_2_on_stderr(self):
        @guarded("demo")
        def main(argv):
            parser = argparse.ArgumentParser(prog="demo")
            parser.add_argument("--n", type=int, required=True)
            parser.parse_args(argv)
            return 0
        code, out, err = self.run_main(main, ["--n", "nope"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("usage: demo", err)

    def test_exceptions_and_interrupts_become_envelopes(self):
        @guarded("demo")
        def broken():
            raise sqlite3.OperationalError("locked")

        @guarded("demo")
        def interrupted():
            raise KeyboardInterrupt
        code, out, _ = self.run_main(broken)
        self.assertEqual((code, json.loads(out)["error"]["code"]), (1, "database_error"))
        code, out, _ = self.run_main(interrupted)
        self.assertEqual((code, json.loads(out)["error"]["code"]), (130, "cancelled"))


class AccountArgumentTests(unittest.TestCase):
    def parser(self):
        parser = argparse.ArgumentParser(prog="demo")
        commands = parser.add_subparsers(dest="command", required=True)
        me = commands.add_parser("me")
        add_account_argument(parser, me, help="handle")
        return parser

    def test_account_works_before_or_after_the_subcommand(self):
        for argv in (["--account", "mine", "me"], ["me", "--account", "mine"]):
            with self.subTest(argv=argv):
                args = self.parser().parse_args(argv)
                self.assertEqual(require_account(self.parser(), args, example="demo --account X me"), "mine")

    def test_missing_account_is_a_usage_error(self):
        parser = self.parser()
        args = parser.parse_args(["me"])
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as stop:
            require_account(parser, args, example="demo --account YOUR_HANDLE me")
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("--account HANDLE is required", err.getvalue())


if __name__ == "__main__":
    unittest.main()
