from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import social_outbox
from _env import IsolatedStateTestCase


class OutboxCommandTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = str(self.root / "outbox.sqlite3")
        self.body = self.root / "body.txt"
        self.body.write_text("Exact text", encoding="utf-8")

    def cli(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = social_outbox.main(["--db", self.db, *argv])
        return code, json.loads(output.getvalue())

    def assertEnvelope(self, result, code):
        self.assertIs(result["ok"], False)
        self.assertEqual(set(result["error"]), {"code", "message", "hint"})
        self.assertEqual(result["error"]["code"], code)

    def test_brief_only_platforms_are_refused_with_a_code_and_hint(self):
        for platform in ("facebook", "tiktok", "youtube"):
            with self.subTest(platform=platform):
                code, result = self.cli("draft", "--platform", platform, "--account", "mine",
                                        "--body-file", str(self.body))
                self.assertEqual(code, 2)
                self.assertEnvelope(result, "brief_only_platform")
                self.assertIn("ghost-in-the-box brief", result["error"]["hint"])

    def test_cancel_moves_draft_and_queued_jobs_only(self):
        code, draft = self.cli("draft", "--platform", "linkedin", "--account", "mine",
                               "--body-file", str(self.body))
        self.assertEqual(code, 0)
        job_id = draft["result"]["id"]
        self.cli("queue", job_id)
        code, result = self.cli("cancel", job_id, "--reason", "wrong day")
        self.assertEqual((code, result["result"]["status"]), (0, "cancelled"))
        code, result = self.cli("claim")
        self.assertEqual((code, result["result"]), (0, None))
        code, result = self.cli("cancel", "missing")
        self.assertEqual(code, 2)
        self.assertEnvelope(result, "not_found")

    def test_claim_skips_brief_only_rows_left_by_older_versions(self):
        code, draft = self.cli("draft", "--platform", "linkedin", "--account", "mine",
                               "--body-file", str(self.body))
        job_id = draft["result"]["id"]
        self.cli("queue", job_id)
        db = sqlite3.connect(self.db)
        db.execute("UPDATE jobs SET platform='youtube' WHERE id=?", (job_id,))
        db.commit()
        db.close()
        self.assertEqual(self.cli("claim")[1]["result"], None)

    def test_database_errors_print_the_envelope(self):
        with patch.object(social_outbox, "Outbox", side_effect=sqlite3.OperationalError("database is locked")):
            code, result = self.cli("list")
        self.assertEqual(code, 1)
        self.assertEnvelope(result, "outbox_database_error")
        self.assertEqual(result["error_type"], "OperationalError")

    def test_missing_body_file_and_bad_request_are_usage_errors(self):
        code, result = self.cli("draft", "--platform", "linkedin", "--account", "mine",
                                "--body-file", str(self.root / "nope.txt"))
        self.assertEqual(code, 2)
        self.assertEnvelope(result, "file_not_found")
        code, result = self.cli("list", "--limit", "500")
        self.assertEqual(code, 2)
        self.assertEnvelope(result, "invalid_request")


if __name__ == "__main__":
    unittest.main()
