from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import social_outbox, social_publish_job
from _env import IsolatedStateTestCase
from test_linkedin_adapter import png_bytes


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

    def image_draft(self, *extra):
        image = self.root / "photo.png"
        image.write_bytes(png_bytes())
        return image, self.cli("draft", "--platform", "linkedin", "--account", "mine",
                               "--body-file", str(self.body), "--media", str(image), *extra)

    def test_linkedin_draft_accepts_one_image_with_alt_text(self):
        image, (code, draft) = self.image_draft("--alt", "A red square")
        self.assertEqual(code, 0)
        code, shown = self.cli("show", draft["result"]["id"])
        self.assertEqual((shown["result"]["media_path"], shown["result"]["media_alt"]),
                         (str(image.resolve()), "A red square"))

    def test_linkedin_draft_refuses_a_second_image_and_a_fake_image(self):
        image = self.root / "photo.png"
        image.write_bytes(png_bytes())
        code, result = self.cli("draft", "--platform", "linkedin", "--account", "mine",
                                "--body-file", str(self.body), "--media", str(image), "--media", str(image))
        self.assertEqual(code, 2)
        self.assertEnvelope(result, "linkedin_single_image_only")
        code, result = self.cli("draft", "--platform", "instagram", "--account", "mine",
                                "--body-file", str(self.body), "--media", str(image), "--media", str(image))
        self.assertEqual(code, 2)
        self.assertEnvelope(result, "multiple_media")
        fake = self.root / "photo.jpg"
        fake.write_text("hello", encoding="utf-8")
        code, result = self.cli("draft", "--platform", "linkedin", "--account", "mine",
                                "--body-file", str(self.body), "--media", str(fake))
        self.assertEqual(code, 2)
        self.assertEnvelope(result, "invalid_media")
        code, result = self.cli("draft", "--platform", "linkedin", "--account", "mine",
                                "--body-file", str(self.body), "--alt", "no image")
        self.assertEqual(code, 2)
        self.assertEnvelope(result, "alt_requires_media")
        code, result = self.cli("list", "--status", "all")
        self.assertEqual(result["result"], [])

    def test_publish_dry_run_summarises_the_linkedin_image_without_network(self):
        image, (_, draft) = self.image_draft("--alt", "A red square")
        job_id = draft["result"]["id"]
        self.cli("queue", job_id)
        output = io.StringIO()
        with redirect_stdout(output), patch.object(social_publish_job, "LinkedInAdapter",
                                                   side_effect=AssertionError("no adapter")):
            code = social_publish_job.main(["--db", self.db, "--dry-run", job_id])
        result = json.loads(output.getvalue())
        self.assertEqual((code, result["status"], result["network_requests"]), (0, "dry_run", 0))
        self.assertEqual(result["media"], {"path": str(image.resolve()), "mime": "image/png",
                                           "size_bytes": len(png_bytes()), "alt_text": "A red square",
                                           "valid": True})
        self.assertEqual(result["payload"]["media_alt"], "A red square")
        self.assertNotIn("media_invalid", result["blockers"])
        image.write_text("replaced by text", encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            social_publish_job.main(["--db", self.db, "--dry-run", job_id])
        result = json.loads(output.getvalue())
        self.assertFalse(result["media"]["valid"])
        self.assertIn("JPEG, PNG, GIF or WebP", result["media"]["problem"])
        self.assertIn("media_invalid", result["blockers"])
        self.assertIn("payload_changed", result["blockers"])


if __name__ == "__main__":
    unittest.main()
