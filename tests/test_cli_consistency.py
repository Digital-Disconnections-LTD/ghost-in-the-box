"""Every command speaks the same terminal contract: flags, envelopes, hints and exit codes."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import requests

from farm import cli, config
from farm.outbox import Outbox
from scripts import (
    ig_import_own, ig_probe, ig_sync_inbox, instagram_carousel, linkedin, meme_library, social_accounts,
    social_outbox, social_publish_job, vision_inspect,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def invoke(main, argv, stdin=None):
    """Run a command's main() in-process; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), patch("sys.stdin", stdin or io.StringIO("")):
        try:
            code = main(argv)
        except SystemExit as stop:
            code = stop.code
    return code, out.getvalue(), err.getvalue()


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "outbox.sqlite3"
        environment = patch.dict(os.environ, {"SOCIAL_FARM_ACCOUNTS_FILE": str(self.root / "accounts.json"),
                                              "SOCIAL_FARM_OUTBOX_DB": str(self.db)})
        environment.start()
        self.addCleanup(environment.stop)

    def envelope(self, output, code):
        result = json.loads(output)
        self.assertIs(result["ok"], False)
        self.assertEqual(set(result["error"]), {"code", "message", "hint"})
        self.assertEqual(result["error"]["code"], code)
        self.assertTrue(result["error"]["hint"])
        return result


class OutboxFlagTests(StateTestCase):
    def outbox(self, *argv, stdin=None):
        return invoke(social_outbox.main, ["--db", str(self.db), *argv], stdin=stdin)

    def draft(self):
        code, out, _ = self.outbox("draft", "--platform", "threads", "--account", "mine", "--body", "Hello")
        self.assertEqual(code, 0, out)
        return json.loads(out)["result"]["id"]

    def test_unknown_status_is_refused_with_the_valid_values(self):
        code, out, _ = self.outbox("list", "--status", "bogus")
        self.assertEqual(code, 2)
        hint = self.envelope(out, "invalid_status")["error"]["hint"]
        for state in ("active", "all", "draft", "queued", "uncertain", "cancelled"):
            self.assertIn(state, hint)
        for status in ("active", "all", "cancelled"):
            self.assertEqual(self.outbox("list", "--status", status)[0], 0)

    def test_unparseable_schedule_time_explains_the_format(self):
        job = self.draft()
        for value in ("tomorrow", "2026-11-02T09:00:00"):
            with self.subTest(value=value):
                code, out, _ = self.outbox("queue", job, "--at", value)
                self.assertEqual(code, 2)
                hint = self.envelope(out, "invalid_time")["error"]["hint"]
                self.assertIn(f"ghost-in-the-box outbox queue {job} --at 2026-11-02T09:00:00-05:00", hint)
        self.assertEqual(self.outbox("queue", job, "--at", "2026-11-02T09:00:00Z")[0], 0)

    def test_body_can_be_inline_a_file_or_stdin(self):
        body = self.root / "post.txt"
        body.write_text("From a file", encoding="utf-8")
        for argv, stdin, expected in ((["--body", "Inline"], None, "Inline"),
                                      (["--body-file", str(body)], None, "From a file"),
                                      (["--body-file", "-"], io.StringIO("From stdin"), "From stdin")):
            with self.subTest(argv=argv):
                code, out, _ = self.outbox("draft", "--platform", "linkedin", "--account", "mine", *argv,
                                           stdin=stdin)
                self.assertEqual(code, 0, out)
                with Outbox(self.db) as box:
                    self.assertEqual(box.get(json.loads(out)["result"]["id"])["body"], expected)
        code, _, err = self.outbox("draft", "--platform", "linkedin", "--account", "mine")
        self.assertEqual(code, 2)
        self.assertIn("one of the arguments --body --body-file is required", err)

    def test_unknown_platform_is_refused_before_the_body_file_is_read(self):
        code, out, _ = self.outbox("draft", "--platform", "myspace", "--account", "mine",
                                   "--body-file", str(self.root / "absent.txt"))
        self.assertEqual(code, 2)
        self.assertIn("instagram, threads, linkedin", self.envelope(out, "unsupported_platform")["error"]["hint"])

    def test_state_refusals_point_at_the_command_that_shows_the_job(self):
        job = self.draft()
        self.outbox("queue", job)
        code, out, _ = self.outbox("retry-failed", job, "--reason", "fixed")
        self.assertEqual(code, 2)
        self.assertIn(f"ghost-in-the-box outbox show {job}", self.envelope(out, "not_failed")["error"]["hint"])
        code, out, _ = self.outbox("queue", "missing-job")
        self.assertIn("outbox list --status all", self.envelope(out, "not_found")["error"]["hint"])

    def test_database_failures_name_the_outbox(self):
        with patch.object(social_outbox, "Outbox", side_effect=sqlite3.OperationalError("locked")):
            code, out, _ = self.outbox("list")
        self.assertEqual(code, 1)
        self.envelope(out, "outbox_database_error")


class PublishHintTests(StateTestCase):
    def test_every_non_queued_state_has_a_command_hint(self):
        with Outbox(self.db) as box:
            job = box.create_draft(platform="threads", account="mine", body="Hello")
            box.cancel(job["id"])
        code, out, _ = invoke(social_publish_job.main, ["--db", str(self.db), job["id"]])
        self.assertEqual(code, 2)
        self.assertIn("ghost-in-the-box outbox draft", self.envelope(out, "not_queued")["error"]["hint"])

    def test_install_hints_are_the_real_install_commands(self):
        self.assertEqual(social_publish_job.INSTALL_HINTS["instagram"], config.install_command("instagram"))
        self.assertIn(config.install_command("login"), social_publish_job.INSTALL_HINTS["threads"])
        self.assertNotIn("python -m pip install -e '.[", json.dumps(social_publish_job.INSTALL_HINTS))


class LinkedInCommandTests(StateTestCase):
    def test_not_signed_in_exits_4_with_the_login_command(self):
        for argv in (["--account", "mine", "me"], ["me", "--account", "mine"]):
            with self.subTest(argv=argv):
                code, out, _ = invoke(linkedin.main, argv)
                self.assertEqual(code, 4)
                result = self.envelope(out, "auth_unavailable")
                self.assertEqual(result["error"]["hint"],
                                 "ghost-in-the-box login start --platform linkedin --account mine")
                self.assertEqual(result["error_type"], "AuthUnavailable")

    def test_account_is_required_and_bounds_are_checked_before_any_request(self):
        code, out, err = invoke(linkedin.main, ["me"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--account HANDLE is required", err)
        code, _, err = invoke(linkedin.main, ["--account", "mine", "posts", "--limit", "99"])
        self.assertEqual(code, 2)
        self.assertIn("--limit must be 1..25", err)


class InstagramInboxCommandTests(StateTestCase):
    def inbox(self, *argv, stdin=None):
        return invoke(ig_sync_inbox.main, ["--db", str(self.root / "inbox.sqlite3"), *argv], stdin=stdin)

    def test_there_is_no_placeholder_account_default(self):
        code, out, err = self.inbox("peek")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--account HANDLE is required", err)
        self.assertFalse((self.root / "inbox.sqlite3").exists())

    def test_account_works_after_the_subcommand_and_bounds_are_usage_errors(self):
        code, out, _ = self.inbox("peek", "--account", "mine")
        self.assertEqual((code, json.loads(out)), (0, []))
        code, _, err = self.inbox("--account", "mine", "peek", "--limit", "0")
        self.assertEqual(code, 2)
        self.assertIn("--limit must be 1..100", err)

    def test_draft_reply_takes_body_or_body_file_and_explains_refusals(self):
        code, out, _ = self.inbox("--account", "mine", "draft-reply", "--comment-id", "1", "--body-file", "-",
                                  stdin=io.StringIO("Thanks!"))
        self.assertEqual(code, 2)
        self.assertIn("comment not found", self.envelope(out, "invalid_input")["error"]["message"])

    def test_missing_session_on_sync_exits_4_with_the_login_command(self):
        code, out, _ = self.inbox("--account", "mine", "sync")
        self.assertEqual(code, 4)
        result = self.envelope(out, "session_unavailable")
        self.assertIn("ghost-in-the-box login start --platform instagram --account mine", result["error"]["hint"])


class CarouselCommandTests(StateTestCase):
    def test_account_is_required(self):
        code, _, err = invoke(instagram_carousel.main, ["draft", "--body", "caption", "--image", "a.png",
                                                        "--image", "b.png"])
        self.assertEqual(code, 2)
        self.assertIn("--account", err)

    def test_missing_slide_names_the_path_and_drafts_print_the_next_command(self):
        slides = []
        for name in ("one.png", "two.png"):
            path = self.root / name
            path.write_bytes(PNG + name.encode())
            slides += ["--image", str(path)]
        code, out, _ = invoke(instagram_carousel.main, ["draft", "--account", "mine", "--body", "",
                                                        "--image", str(self.root / "gone.png"), *slides[:2]])
        self.assertEqual(code, 2)
        self.assertIn("gone.png", self.envelope(out, "file_not_found")["error"]["message"])
        original = instagram_carousel.create_draft

        def into_temp(**kwargs):
            return original(**kwargs, root=self.root / "carousels", box_factory=lambda: Outbox(self.db))
        with patch.object(instagram_carousel, "create_draft", side_effect=into_temp):
            code, out, _ = invoke(instagram_carousel.main, ["draft", "--account", "mine", "--body", "", *slides])
        self.assertEqual(code, 0, out)
        result = json.loads(out)
        self.assertEqual(result["next"], f"ghost-in-the-box outbox queue {result['job_id']}")


class InstagramImportCommandTests(StateTestCase):
    def test_account_is_required_and_username_still_works(self):
        code, _, err = invoke(ig_import_own.main, ["status", "--state-root", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("--account HANDLE is required", err)
        code, out, _ = invoke(ig_import_own.main, ["status", "--username", "mine", "--state-root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["posts"], 0)
        help_text = invoke(ig_import_own.main, ["--help"])[1]
        self.assertNotIn("--username", help_text)

    def test_out_of_range_numbers_get_a_short_message_not_a_choice_list(self):
        code, _, err = invoke(ig_import_own.main, ["import", "--account", "mine", "--page-size", "99"])
        self.assertEqual(code, 2)
        self.assertIn("must be 1..33", err)
        self.assertNotIn("17, 18", err)

    def test_missing_saved_session_says_how_to_sign_in(self):
        code, out, _ = invoke(ig_import_own.main, ["import", "--account", "mine", "--state-root", str(self.root)])
        self.assertEqual(code, 4)
        result = self.envelope(out, "saved_session_missing")
        self.assertIn("ghost-in-the-box login start --platform instagram --account mine", result["error"]["hint"])
        self.assertEqual(result["error_type"], "ImportFailure")


class AccountsCommandTests(StateTestCase):
    def test_unknown_platform_lists_the_known_ones(self):
        code, out, _ = invoke(social_accounts.main, ["register", "--platform", "myspace", "--account", "mine",
                                                     "--kind", "cookie_jar", "--session-file", str(self.root)])
        self.assertEqual(code, 2)
        hint = self.envelope(out, "unknown_platform")["error"]["hint"]
        self.assertIn("instagram, threads, linkedin, substack", hint)
        self.assertEqual(json.loads(out)["error_type"], "unknown_platform")


class MediaCommandTests(StateTestCase):
    def test_memes_reports_a_missing_extra_with_the_install_command(self):
        with patch.object(meme_library, "missing_media_modules", return_value=["PIL"]):
            code, out, _ = invoke(meme_library.main, ["--root", str(self.root), "organize"])
        self.assertEqual(code, 4)
        self.assertEqual(self.envelope(out, "dependency_unavailable")["error"]["hint"],
                         config.install_command("media"))

    def test_memes_refusals_are_envelopes_and_help_names_the_real_command(self):
        code, out, _ = invoke(meme_library.main, ["--root", str(self.root), "prepare-carousel", "only-one",
                                                  "--out", str(self.root / "slides")])
        self.assertEqual(code, 2)
        self.envelope(out, "invalid_input")
        self.assertIn("ghost-in-the-box memes collect", meme_library.__doc__)
        self.assertNotIn("scripts/", meme_library.__doc__)

    def test_vision_explains_missing_images_and_unreachable_endpoints(self):
        code, out, _ = invoke(vision_inspect.main, ["--image", str(self.root / "gone.png")])
        self.assertEqual(code, 2)
        self.envelope(out, "file_not_found")
        image = self.root / "slide.png"
        image.write_bytes(PNG)
        with patch.object(requests.Session, "post", side_effect=requests.ConnectionError("refused")):
            code, out, _ = invoke(vision_inspect.main, ["--image", str(image)])
        self.assertEqual(code, 1)
        self.assertIn("--endpoint", self.envelope(out, "vision_unavailable")["error"]["hint"])

    def test_instagram_login_without_the_extra_exits_4_with_the_install_command(self):
        with patch.object(ig_probe, "Client", None):
            code, out, _ = invoke(ig_probe.main, ["--credentials", str(self.root / "creds.json")])
        self.assertEqual(code, 4)
        self.assertEqual(self.envelope(out, "dependency_unavailable")["error"]["hint"],
                         config.install_command("instagram"))


class EditableCheckoutTests(unittest.TestCase):
    class Dist:
        def __init__(self, root, direct_url=None):
            self.root, self.direct_url = root, direct_url

        def read_text(self, name):
            return self.direct_url if name == "direct_url.json" else None

        def locate_file(self, name):
            return Path(self.root) / name

    def test_a_stale_checkout_egg_info_does_not_hide_the_editable_install(self):
        with TemporaryDirectory() as folder:
            checkout = Path(folder) / "ghost"
            (checkout / "farm").mkdir(parents=True)
            (checkout / "farm" / "cli.py").write_text("")
            (checkout / "pyproject.toml").write_text("")
            stale = self.Dist(checkout)
            installed = self.Dist(Path(folder) / "site-packages",
                                  json.dumps({"url": checkout.resolve().as_uri(), "dir_info": {"editable": True}}))
            with patch.object(config.importlib.metadata, "distributions", return_value=[stale, installed]):
                self.assertEqual(config._editable_checkout(), checkout.resolve())
            with patch.object(config.importlib.metadata, "distributions", return_value=[stale]):
                self.assertEqual(config._editable_checkout(), checkout.resolve())
            wheel = self.Dist(Path(folder) / "site-packages", json.dumps({"url": "https://example.test/x.whl"}))
            with patch.object(config.importlib.metadata, "distributions", return_value=[wheel]):
                self.assertIsNone(config._editable_checkout())


class HelpTextTests(unittest.TestCase):
    """Every command's --help works offline, names itself correctly and stays readable."""

    def help_text(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main([*argv, "--help"])
            except SystemExit as stop:
                code = stop.code
        self.assertEqual(code, 0, f"{argv}: {err.getvalue()}")
        return out.getvalue()

    def subcommands(self, text):
        block = re.search(r"\{([a-z][a-z0-9,-]+)\}\n", text)
        if block:
            return block.group(1).split(",")
        listing = re.search(r"^  COMMAND\n((?:    \S.*\n|\s{8,}.*\n)+)", text, re.M)
        return re.findall(r"^    ([a-z][a-z0-9-]+)", listing.group(1), re.M) if listing else []

    def test_every_command_and_subcommand_has_clean_help(self):
        pages = 0
        for command in [*cli.BUILTINS, *cli.COMMANDS]:
            with patch.object(cli, "missing_modules", return_value=["missing"]):
                top = self.help_text(command)
            for argv, text in [((command,), top)] + [((command, sub), self.help_text(command, sub))
                                                       for sub in self.subcommands(top)]:
                pages += 1
                with self.subTest(argv=argv):
                    self.assertNotRegex(text, r"python3? +scripts/|scripts/\w+\.py")
                    self.assertNotIn(" ".join(argv) + " --help", text)
                    for choices in re.findall(r"\{([^}]*)\}", text):
                        self.assertLess(choices.count(","), 12, choices)
                    self.assertIn("ghost-in-the-box " + argv[0], text)
        self.assertGreater(pages, 60)

    def test_subcommand_help_strings_are_distinct(self):
        for command in ("threads", "outbox", "instagram-inbox", "threads-notifications", "linkedin", "memes"):
            text = self.help_text(command)
            listing = re.findall(r"^    ([a-z][a-z0-9-]+)\s{2,}(\S.*)$", text, re.M)
            descriptions = [description for _, description in listing]
            self.assertEqual(len(descriptions), len(set(descriptions)), command)

    def test_publish_is_described_with_its_safe_modes(self):
        self.assertIn("--dry-run", cli.COMMANDS["publish"][1])
        self.assertIn("--due", cli.COMMANDS["publish"][1])


if __name__ == "__main__":
    unittest.main()
