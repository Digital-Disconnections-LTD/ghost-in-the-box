from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts import social_accounts


class SocialAccountsCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.registry = self.root / "accounts.json"
        self.session = self.root / "session.json"
        self.session.write_text("{}")
        self.session.chmod(0o600)

    def run_cli(self, *argv):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            try:
                code = social_accounts.main(["--registry", str(self.registry), *argv])
            except SystemExit as exit:
                code = exit.code
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None), err.getvalue()

    def register(self, platform="instagram", account="mine"):
        return self.run_cli("register", "--platform", platform, "--account", account,
                            "--kind", "instagrapi_settings", "--session-file", str(self.session))

    def test_register_then_show_by_flags_or_positionals(self):
        code, result, _ = self.register()
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertEqual(result["result"]["account_key"], "meta:mine")
        for argv in (("show", "--platform", "instagram", "--account", "mine"),
                     ("show", "instagram", "mine"),
                     ("show", "instagram", "--account", "mine")):
            code, result, _ = self.run_cli(*argv)
            self.assertEqual(code, 0, argv)
            self.assertEqual(result["result"]["session_file"], str(self.session))
        code, result, _ = self.run_cli("list")
        self.assertEqual((code, len(result["result"])), (0, 1))

    def test_show_usage_errors_exit_2(self):
        code, _, err = self.run_cli("show", "--platform", "instagram")
        self.assertEqual(code, 2)
        self.assertIn("--account", err)
        code, _, err = self.run_cli("show", "instagram", "mine", "--account", "other")
        self.assertEqual(code, 2)
        self.assertIn("different values", err)

    def test_unregistered_account_exits_4_with_login_hint(self):
        code, result, _ = self.run_cli("show", "--platform", "threads", "--account", "nobody")
        self.assertEqual(code, 4)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "account_not_registered")
        self.assertIn(str(self.registry), result["error"]["message"])
        self.assertEqual(result["error"]["hint"], "ghost-in-the-box login start --platform threads --account nobody")
        self.assertEqual(result["error_type"], "account_not_registered")

    def test_missing_session_file_is_a_usage_error_naming_the_path(self):
        missing = self.root / "missing.json"
        code, result, _ = self.run_cli("register", "--platform", "linkedin", "--account", "mine",
                                       "--kind", "cookie_jar", "--session-file", str(missing))
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "session_missing")
        self.assertIn(str(missing), result["error"]["message"])
        self.assertIn("login start --platform linkedin --account mine", result["error"]["hint"])

    def test_corrupt_registry_is_a_runtime_failure(self):
        self.registry.write_text("{broken")
        code, result, _ = self.run_cli("list")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "registry_unreadable")
        self.assertTrue(result["error"]["hint"])

    def test_unreadable_registry_path_is_a_runtime_failure(self):
        self.registry.mkdir()
        code, result, _ = self.run_cli("list")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "local_state_unavailable")
        self.assertIn(str(self.registry), result["error"]["message"])

    def test_bad_kind_is_rejected_by_argparse(self):
        code, result, err = self.run_cli("register", "--platform", "x", "--account", "y", "--kind", "cookie",
                                         "--session-file", str(self.session))
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertIn("cookie_jar", err)


if __name__ == "__main__":
    unittest.main()
