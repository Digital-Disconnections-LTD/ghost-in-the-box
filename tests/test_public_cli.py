import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from farm import cli, config
from farm.accounts import AccountRegistry


@contextlib.contextmanager
def isolated_state(root, **extra_env):
    """Point every doctor/config lookup at a private temporary tree."""
    root.mkdir(mode=0o700, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("SOCIAL_FARM_")}
    env.update({"SOCIAL_FARM_STATE_DIR": str(root), "SOCIAL_FARM_ACCOUNTS_FILE": str(root / "accounts.json"),
                "SOCIAL_FARM_OUTBOX_DB": str(root / "outbox.sqlite3"),
                "SOCIAL_FARM_IG_CREDENTIALS": str(root / "credentials" / "ig.json")}, **extra_env)
    with patch.dict(os.environ, env, clear=True), patch.object(cli, "STATE_ROOT", root), \
            patch.object(config, "STATE_ROOT", root), \
            patch.object(cli, "bundled_chromium", return_value=root / "chromium-1"), \
            patch.object(cli, "system_browsers", return_value=[]):
        yield root


def private_file(path, text="{}"):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


class PublicCLITests(unittest.TestCase):
    def test_installed_instagram_client_cannot_consume_protocol_input(self):
        from farm.instagram_mcp import _new_client
        from farm.instagram_session import InstagramSessionError
        client = _new_client()
        try:
            with self.assertRaises(InstagramSessionError):
                client.challenge_code_handler("example_creator", "email")
            with self.assertRaises(InstagramSessionError):
                client.change_password_handler("example_creator")
        finally:
            client.private.close()
            client.public.close()

    def test_help_and_doctor_do_not_run_a_provider(self):
        with TemporaryDirectory() as folder, isolated_state(Path(folder)), \
                patch.object(cli.runpy, "run_module", side_effect=AssertionError("provider launched")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertIn(cli.main(["doctor", "--json"]), (0, 1))
            self.assertEqual(json.loads(output.getvalue())["network_requests"], 0)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["--help"]), 0)
            self.assertIn("instagram-mcp", output.getvalue())
            self.assertIn("usage: ghost-in-the-box", output.getvalue())

    def test_existing_command_receives_arguments_and_exit_status(self):
        seen = []
        def run(module, *, run_name):
            seen.append((module, list(sys.argv), run_name))
            raise SystemExit(2)
        previous = sys.argv
        with patch.object(cli.runpy, "run_module", side_effect=run):
            with self.assertRaises(SystemExit) as error:
                cli.main(["outbox", "--db", "/tmp/example.sqlite3", "list"])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(seen[0][0], "scripts.social_outbox")
        self.assertEqual(seen[0][1][1:], ["--db", "/tmp/example.sqlite3", "list"])
        self.assertIs(sys.argv, previous)

    def test_state_override_connects_registry_inbox_and_outbox(self):
        with TemporaryDirectory() as folder:
            env = dict(os.environ, SOCIAL_FARM_STATE_DIR=folder)
            program = "from farm.config import STATE_ROOT; from farm.accounts import DEFAULT_REGISTRY; from farm.inbox import DEFAULT_DB as inbox; from farm.outbox import DEFAULT_DB as outbox; import json; print(json.dumps([str(x) for x in (STATE_ROOT,DEFAULT_REGISTRY,inbox,outbox)]))"
            result = subprocess.run([sys.executable, "-c", program], env=env, capture_output=True, text=True, check=True)
            paths = [Path(p) for p in json.loads(result.stdout)]
            self.assertEqual(paths[0], Path(folder))
            self.assertTrue(all(p.parent == Path(folder) for p in paths[1:]))


class DispatcherTests(unittest.TestCase):
    def run_main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_help_is_grouped_and_lists_each_command_once(self):
        code, out, _ = self.run_main(["--help"])
        self.assertEqual(code, 0)
        for name in [*cli.BUILTINS, *cli.COMMANDS]:
            self.assertEqual(len([line for line in out.splitlines() if line.strip().split(" ")[0] == name]), 1, name)
        for title, _ in cli.GROUPS:
            self.assertIn(f"{title}:", out)
        self.assertNotIn("{", out)
        grouped = [name for _, names in cli.GROUPS for name in names]
        self.assertCountEqual(grouped, [*cli.BUILTINS, *cli.COMMANDS])

    def test_version_comes_from_package_metadata(self):
        code, out, _ = self.run_main(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"ghost-in-the-box {config.package_version()}")
        self.assertRegex(out, r"ghost-in-the-box \d+\.\d+")

    def test_version_falls_back_to_pyproject_without_metadata(self):
        import importlib.metadata
        with patch.object(config.importlib.metadata, "version",
                          side_effect=importlib.metadata.PackageNotFoundError):
            self.assertRegex(config.package_version(), r"^\d+\.\d+")

    def test_typo_suggests_the_closest_command_without_usage_blob(self):
        with patch.object(cli.runpy, "run_module", side_effect=AssertionError("ran")):
            code, out, err = self.run_main(["outbx", "list"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Did you mean: ghost-in-the-box outbox", err)
        self.assertNotIn("usage:", err)
        code, _, err = self.run_main(["doctr"])
        self.assertIn("ghost-in-the-box doctor", err)
        code, _, err = self.run_main(["zzzzzz"])
        self.assertEqual(code, 2)
        self.assertNotIn("Did you mean", err)

    def test_unknown_global_option_is_a_usage_error(self):
        code, _, err = self.run_main(["--state-dirr"])
        self.assertEqual(code, 2)
        self.assertIn("unknown option", err)

    def test_missing_extra_names_the_install_command_and_exits_4(self):
        with patch.object(cli, "missing_modules", return_value=["instagrapi"]), \
                patch.object(cli.runpy, "run_module", side_effect=AssertionError("ran")):
            code, out, err = self.run_main(["instagram-login"])
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertIn("'instagram' extra", err)
        self.assertIn("-m pip install", err)
        self.assertIn("[instagram]", err)
        self.assertNotIn("usage:", err)

    def test_substack_gate_and_doctor_check_the_same_modules(self):
        self.assertEqual(cli.COMMAND_FEATURES["substack-mcp"], "substack")
        self.assertIn("substack_mcp", cli.FEATURES["substack"]["modules"])
        with patch.object(cli.importlib.util, "find_spec",
                          side_effect=lambda name: None if name == "substack_mcp" else object()):
            self.assertEqual(cli.missing_modules("substack"), ["substack_mcp"])
            findings, available = cli._check_features({})
        self.assertFalse(available["substack"])

    def test_every_existing_command_name_still_dispatches(self):
        seen = []
        with patch.object(cli, "missing_modules", return_value=[]), \
                patch.object(cli.runpy, "run_module", side_effect=lambda module, run_name: seen.append(module)):
            for name in cli.COMMANDS:
                self.assertEqual(cli.main([name, "--flag"]), 0)
        self.assertEqual(seen, [module for module, _ in cli.COMMANDS.values()])

    def test_help_alias_shows_command_help(self):
        seen = []
        def run(module, *, run_name):
            seen.append(list(sys.argv))
        with patch.object(cli.runpy, "run_module", side_effect=run):
            self.assertEqual(cli.main(["help", "outbox"]), 0)
        self.assertEqual(seen[0][1:], ["--help"])

    def test_help_help_prints_top_level_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["help", "--help"]), 0)
        self.assertIn("COMMAND --help", out.getvalue())


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "state"

    def run_doctor(self, *flags):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.run_doctor(["--json", *flags])
        return code, json.loads(out.getvalue())

    def test_ready_setup_exits_zero_without_touching_state(self):
        with isolated_state(self.root):
            registry = AccountRegistry()
            registry.register(platform="linkedin", account="mine", kind="cookie_jar",
                              session_file=private_file(self.root / "linkedin" / "mine.json"))
            before = sorted(p.name for p in self.root.rglob("*"))
            with patch("socket.socket", side_effect=AssertionError("network")):
                code, report = self.run_doctor()
            after = sorted(p.name for p in self.root.rglob("*"))
        self.assertEqual([f for f in report["checks"] if f["severity"] == "fail"], [])
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(before, after)
        self.assertEqual(report["network_requests"], 0)
        self.assertEqual(report["state_directory"], str(self.root))

    def test_broken_sessions_stuck_jobs_and_public_credentials_fail_with_next_steps(self):
        from farm.outbox import Outbox
        with isolated_state(self.root):
            registry = AccountRegistry()
            ig = private_file(self.root / "ig" / "mine.json")
            registry.register(platform="instagram", account="mine", kind="instagrapi_settings", session_file=ig)
            public = private_file(self.root / "linkedin" / "pub.json")
            registry.register(platform="linkedin", account="pub", kind="cookie_jar", session_file=public)
            ig.unlink()
            public.chmod(0o644)
            body = private_file(self.root / "body.txt", "hello")
            box = Outbox()
            jobs = {}
            for name, account in (("uncertain", "a"), ("stale", "b"), ("overdue", "c"), ("fresh", "d")):
                draft = box.create_draft(platform="linkedin", account=account, body=body.read_text())
                box.queue(draft["id"])
                jobs[name] = draft["id"]
            past = "2020-01-01T00:00:00Z"
            box.db.execute("UPDATE jobs SET status='uncertain' WHERE id=?", (jobs["uncertain"],))
            box.db.execute("UPDATE jobs SET status='in_flight', lease_until=? WHERE id=?", (past, jobs["stale"]))
            box.db.execute("UPDATE jobs SET scheduled_at=? WHERE id=?", (past, jobs["overdue"]))
            box.close()
            outbox_bytes = (self.root / "outbox.sqlite3").read_bytes()
            private_file(self.root / "credentials" / "ig.json").chmod(0o644)
            code, report = self.run_doctor()
            self.assertEqual((self.root / "outbox.sqlite3").read_bytes(), outbox_bytes)
        self.assertEqual(code, 1)
        self.assertEqual(report["verdict"], "problems")
        messages = {f["message"]: f for f in report["problems"]}
        def find(text):
            matches = [f for m, f in messages.items() if text in m]
            self.assertTrue(matches, f"{text!r} not in {list(messages)}")
            return matches[0]
        self.assertIn("ghost-in-the-box login start --platform instagram --account mine",
                      find("instagram:mine session file is missing")["hint"])
        self.assertIn("chmod 600", find("linkedin:pub session is readable")["hint"])
        self.assertIn(f"outbox resolve {jobs['uncertain']}", find(f"job {jobs['uncertain']}")["hint"])
        self.assertEqual(find(f"job {jobs['stale']}")["severity"], "fail")
        overdue = find(f"job {jobs['overdue']}")
        self.assertEqual(overdue["severity"], "warn")
        self.assertIn(f"ghost-in-the-box publish {jobs['overdue']}", overdue["hint"])
        self.assertFalse([m for m in messages if jobs["fresh"] in m])
        self.assertIn("chmod 600", find("Instagram credentials")["hint"])
        self.assertTrue(all(f["hint"] for f in report["problems"]))

    def test_warnings_pass_unless_strict(self):
        missing = self.root.parent / "not-created"
        with isolated_state(self.root), patch.object(cli, "STATE_ROOT", missing):
            code, report = self.run_doctor()
            strict, _ = self.run_doctor("--strict")
        self.assertEqual(report["verdict"], "warnings")
        self.assertIn("first run", " ".join(f["message"] for f in report["problems"]))
        self.assertEqual((code, strict), (0, 1))

    def test_registered_account_needs_its_extra(self):
        with isolated_state(self.root), \
                patch.dict(cli.FEATURES, {"instagram": {"modules": ("no_such_module_for_test",), "purpose": "x"}}):
            AccountRegistry().register(platform="instagram", account="mine", kind="instagrapi_settings",
                                       session_file=private_file(self.root / "ig" / "mine.json"))
            code, report = self.run_doctor()
        failure = next(f for f in report["problems"] if f["check"] == "extra:instagram")
        self.assertEqual((code, failure["severity"]), (1, "fail"))
        self.assertIn("instagram:mine", failure["message"])
        self.assertIn("[instagram]", failure["hint"])

    def test_portable_threads_account_needs_bundled_chromium(self):
        with isolated_state(self.root), patch.object(cli, "bundled_chromium", return_value=None), \
                patch.object(cli, "missing_modules", return_value=[]):
            AccountRegistry().register(platform="threads", account="mine", kind="cookie_jar",
                                       session_file=private_file(self.root / "threads" / "mine.json"))
            code, report = self.run_doctor()
        failure = next(f for f in report["problems"] if f["check"] == "browser")
        self.assertEqual((code, failure["severity"]), (1, "fail"))
        self.assertIn("playwright install chromium", failure["hint"])

    def test_a_crashing_check_is_reported_not_swallowed(self):
        with isolated_state(self.root), patch.object(cli, "_check_node", side_effect=RuntimeError("boom")):
            code, report = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("node check crashed", " ".join(f["message"] for f in report["problems"]))

    def test_unreadable_outbox_and_registry_are_problems(self):
        with isolated_state(self.root):
            private_file(self.root / "accounts.json", "not json")
            private_file(self.root / "outbox.sqlite3", "not a database")
            code, report = self.run_doctor()
        checks = {f["check"] for f in report["problems"] if f["severity"] == "fail"}
        self.assertEqual(code, 1)
        self.assertTrue({"accounts", "outbox"} <= checks)

    def test_misspelled_environment_variable_is_flagged(self):
        with isolated_state(self.root, SOCIAL_FARM_STATE_DIRR="/tmp/x"):
            _, report = self.run_doctor()
        warning = next(f for f in report["problems"] if f["check"] == "environment")
        self.assertIn("SOCIAL_FARM_STATE_DIR", warning["hint"])

    def test_text_report_has_verdict_and_next_steps(self):
        with isolated_state(self.root):
            AccountRegistry().register(platform="linkedin", account="gone", kind="cookie_jar",
                                       session_file=private_file(self.root / "linkedin" / "gone.json"))
            (self.root / "linkedin" / "gone.json").unlink()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["doctor"])
        self.assertEqual(code, 1)
        text = out.getvalue()
        self.assertRegex(text.splitlines()[0], r"doctor .*: \d+ problem")
        self.assertIn("FAIL", text)
        self.assertIn("next: ghost-in-the-box login start --platform linkedin --account gone", text)


class ConfigTests(unittest.TestCase):
    def test_config_show_reports_paths_and_every_setting(self):
        with TemporaryDirectory() as folder:
            env = {k: v for k, v in os.environ.items() if not k.startswith("SOCIAL_FARM_")}
            env.update(SOCIAL_FARM_STATE_DIR=folder, SOCIAL_FARM_VISION_URL="https://user:secret@example.test/v1",
                       SOCIAL_FARM_TYPO="1")
            result = subprocess.run([sys.executable, "-m", "farm.cli", "config", "show", "--json"], env=env,
                                    capture_output=True, text=True, check=True)
            report = json.loads(result.stdout)
            self.assertEqual(report["state_dir"], {"path": folder, "source": "SOCIAL_FARM_STATE_DIR", "exists": True})
            for row in report["paths"]:
                if row["source"] != "legacy":
                    self.assertTrue(Path(row["path"]).is_relative_to(folder), row)
            names = {row["name"]: row for row in report["environment"]}
            self.assertTrue(config.KNOWN_ENVIRONMENT <= set(names))
            self.assertTrue(names["SOCIAL_FARM_STATE_DIR"]["set"])
            self.assertFalse(names["SOCIAL_FARM_NODE"]["set"])
            self.assertNotIn("secret", names["SOCIAL_FARM_VISION_URL"]["value"])
            self.assertTrue(names["SOCIAL_FARM_TYPO"]["unknown"])
            text = subprocess.run([sys.executable, "-m", "farm.cli", "config"], env=env,
                                  capture_output=True, text=True, check=True).stdout
            self.assertIn(f"state directory: {folder}", text)
            self.assertIn("SOCIAL_FARM_OUTBOX_DB", text)

    def test_private_files_follow_a_custom_state_dir_and_keep_legacy_files(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            legacy_creds, legacy_memes = base / "legacy" / "ig.json", base / "legacy" / "memes"
            custom = base / "custom"
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SOCIAL_FARM_IG_CREDENTIALS", "SOCIAL_FARM_MEMES_DIR", "SOCIAL_FARM_THREADS_WORK_DIR")}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(config, "LEGACY_CREDENTIALS", legacy_creds), \
                    patch.object(config, "LEGACY_MEMES", legacy_memes), \
                    patch.object(config, "DEFAULT_STATE_ROOT", base / "default"):
                self.assertEqual(config.credentials_path(base / "default"), legacy_creds)
                self.assertEqual(config.credentials_path(custom), custom / "credentials" / "ig.json")
                self.assertEqual(config.memes_root(custom), custom / "media-reference" / "tech-memes")
                legacy_creds.parent.mkdir(parents=True)
                legacy_creds.write_text("{}")
                legacy_memes.mkdir()
                self.assertEqual(config.credentials_path(custom), legacy_creds)
                self.assertEqual(config.memes_root(custom), legacy_memes)
                with patch.dict(os.environ, {"SOCIAL_FARM_IG_CREDENTIALS": str(base / "env.json")}):
                    self.assertEqual(config.credentials_path(custom), base / "env.json")
                self.assertEqual(config.threads_work_dir(custom), custom / "threads-work")
                self.assertEqual(config.threads_post_marker(custom), custom / "threads-work" / "threadspost")
                with patch.dict(os.environ, {"SOCIAL_FARM_THREADS_WORK_DIR": str(base / "work")}):
                    self.assertEqual(config.threads_post_marker(custom), base / "work" / "threadspost")

    def test_meme_library_root_comes_from_config(self):
        from scripts import meme_library
        self.assertEqual(meme_library.ROOT, config.memes_root())

    def test_install_command_matches_how_the_package_was_installed(self):
        with patch.object(config, "_editable_checkout", return_value=Path("/src/ghost")):
            self.assertIn("-m pip install -e", config.install_command("recommended"))
            self.assertIn("/src/ghost[recommended]", config.install_command("recommended"))
        with patch.object(config, "_editable_checkout", return_value=None):
            command = config.install_command("login")
            self.assertTrue(command.endswith("-m pip install 'ghost-in-the-box[login]'")
                            or command.endswith('-m pip install "ghost-in-the-box[login]"'), command)

    def test_recommended_extra_bundles_login_instagram_and_media(self):
        import tomllib
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        extras = tomllib.loads(pyproject.read_text())["project"]["optional-dependencies"]
        for name in ("login", "instagram", "media"):
            self.assertTrue(set(extras[name]) <= set(extras["recommended"]), name)


if __name__ == "__main__":
    unittest.main()
