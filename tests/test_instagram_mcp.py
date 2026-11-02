from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from farm import config
from farm.accounts import AccountRegistry
from farm.adapters.instagram import AuthUnavailable, PublishUncertain
from farm.inbox import Inbox
from farm import instagram_mcp
from farm.instagram_mcp import (
    LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS, TOOLS, AccountSelectionError, InstagramTools,
    ToolInputError, catalog, catalog_markdown, dispatch, read_message, resolve_account, serve, server_version,
    substack_catalog, tool_catalog, write_message,
)
from farm.instagram_session import InstagramSessionError
from farm.instagram_references import ReferenceError
from farm.outbox import Outbox


class FakeAdapter:
    def __init__(self, error=None):
        self.error = error
        self.sent = []

    def publish(self, job):
        self.sent.append(job["id"])
        if self.error:
            raise self.error
        return {"provider_id": "9000", "permalink": "https://www.instagram.com/p/Published/"}

    def save_session(self):
        pass


class FakeClient:
    user_id = "7"

    def __init__(self):
        self.last_json = {}
        self.page = [self.post()]
        self.hashtag_calls = []
        self.comment_page = [SimpleNamespace(pk="123", user=SimpleNamespace(pk="8", username="reader"),
            text="What happens next?", created_at_utc=datetime.now(timezone.utc))]

    def post(self, *, caption="Caption", owner="7", pk="42"):
        return SimpleNamespace(id=f"{pk}_{owner}", pk=pk, code="Example", user=SimpleNamespace(pk=owner, username="mine"),
            caption_text=caption, taken_at=datetime.now(timezone.utc), media_type=8, resources=[
                SimpleNamespace(pk="1", media_type=1, thumbnail_url="https://example.test/1.jpg"),
                SimpleNamespace(pk="2", media_type=1, thumbnail_url="https://example.test/2.jpg")])

    def user_medias_paginated_v1(self, user_id, amount, end_cursor):
        assert user_id == self.user_id and amount == 33
        self.last_json = {"status": "ok", "items": [{} for _ in self.page]}
        return self.page, "next-page"

    def media_info_v1(self, media_pk):
        return self.post(pk=media_pk)

    def media_pk_from_url(self, url):
        self.last_post_url = url
        return "42"

    def hashtag_medias_v1_chunk(self, name, max_amount, tab_key, max_id):
        self.hashtag_calls.append((name, max_amount, tab_key, max_id))
        self.last_json = {"status": "ok", "sections": []}
        return self.page[:max_amount], "provider-cursor-that-is-not-followed"

    def media_comments_v1_chunk(self, media_id, max_id):
        self.last_json = {"status": "ok", "comments": [{} for _ in self.comment_page]}
        return self.comment_page, "", "next-comments"

    def media_comment_replies_chunk(self, media_id, comment_id, max_amount, min_id):
        self.last_json = {"status": "ok", "child_comments": [{} for _ in self.comment_page]}
        return self.comment_page, "next-replies"


class InstagramMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "outbox.sqlite3"
        self.inbox_db = self.root / "inbox.sqlite3"
        self.registry = AccountRegistry(self.root / "accounts.json")
        session = self.root / "session.json"
        session.write_text('{"private_sentinel":"not a real session"}')
        session.chmod(0o600)
        self.registry.register(platform="instagram", account="mine", kind="instagrapi_settings",
                               session_file=session, account_key="meta:mine")
        self.environment = patch.dict(os.environ, {"SOCIAL_FARM_ACCOUNTS_FILE": str(self.registry.path)})
        self.environment.start()
        self.adapter = FakeAdapter()
        self.client = FakeClient()
        self.fetches = []
        self.service = InstagramTools(account="mine", outbox_path=self.db, inbox_path=self.inbox_db,
            carousel_root=self.root / "carousels", registry=self.registry,
            reader=lambda account, operation, **_kwargs: operation(self.client),
            adapter_factory=lambda: self.adapter, reference_root=self.root / "references",
            image_fetcher=self.fetch_image)
        self.images = [self.root / "first.jpg", self.root / "second.png"]
        for index, path in enumerate(self.images):
            path.write_bytes(f"image {index}".encode())

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def call(self, name, **arguments):
        return self.service.call("instagram_" + name, arguments)

    def fetch_image(self, url, destination, byte_limit, **_kwargs):
        self.fetches.append(url)
        data = b"\x89PNG\r\n\x1a\n" + b"fixture image"
        destination.write_bytes(data)
        return {"size_bytes": len(data), "sha256": "unused-fixture", "suffix": ".png"}

    def draft(self, key="once"):
        return self.call("draft_carousel", caption="Caption", images=[str(path) for path in self.images],
                         idempotency_key=key)["job"]

    def rpc(self, name, arguments=None):
        response = dispatch(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "instagram_" + name, "arguments": arguments or {}}})
        result = response["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    def test_status_has_no_session_material_and_list_is_account_scoped(self):
        result = self.call("status", live_check=True)
        self.assertTrue(result["session_ready"])
        self.assertTrue(result["live_checked"])
        self.assertEqual(result["user_id"], "7")
        self.assertNotIn("session.json", json.dumps(result))
        self.assertNotIn("private_sentinel", json.dumps(result))
        own = self.draft()
        with Outbox(self.db) as box:
            other = box.create_draft(platform="threads", account="mine", body="Other platform")
            alien = box.create_draft(platform="instagram", account="someone_else", body="Other account")
        self.assertEqual([job["id"] for job in self.call("jobs")["jobs"]], [own["id"]])
        for job in (other, alien):
            for method in ("job", "queue_job", "publish_job"):
                with self.assertRaises(ToolInputError):
                    self.call(method, job_id=job["id"])
        self.assertEqual(self.adapter.sent, [])

    def test_carousel_retries_and_concurrent_requests_reuse_frozen_job(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(lambda _: self.draft(), range(2)))
        self.assertEqual(jobs[0]["id"], jobs[1]["id"])
        self.assertEqual(len(self.call("jobs")["jobs"]), 1)
        manifest = Path(jobs[0]["media_path"])
        slides = json.loads(manifest.read_text())["items"]
        self.assertEqual([(manifest.parent / item["filename"]).read_bytes() for item in slides],
                         [b"image 0", b"image 1"])
        self.assertEqual(manifest.stat().st_mode & 0o077, 0)
        self.images[0].write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "different slides"):
            self.draft()
        self.assertEqual(len(self.call("jobs")["jobs"]), 1)

    def test_publish_sends_only_named_queued_job_once(self):
        first, second = self.draft("first"), self.draft("second")
        for job in (first, second):
            self.call("queue_job", job_id=job["id"])
        result = self.call("publish_job", job_id=second["id"])
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.adapter.sent, [second["id"]])
        self.assertEqual(self.call("job", job_id=first["id"])["job"]["status"], "queued")
        repeated = self.call("publish_job", job_id=second["id"])
        self.assertTrue(repeated["already_published"])
        self.assertEqual(self.adapter.sent, [second["id"]])
        self.assertNotIn("lease_token", self.call("job", job_id=second["id"])["job"])

    def test_auth_failure_keeps_queue_and_uncertainty_blocks_retries(self):
        job = self.draft()
        self.call("queue_job", job_id=job["id"])
        self.adapter = FakeAdapter(AuthUnavailable("private authentication context"))
        self.assertEqual(self.call("publish_job", job_id=job["id"])["status"], "auth_unavailable")
        self.assertEqual(self.call("job", job_id=job["id"])["job"]["status"], "queued")
        self.adapter = FakeAdapter(PublishUncertain("private provider response"))
        self.assertEqual(self.call("publish_job", job_id=job["id"])["status"], "uncertain")
        self.assertEqual(self.call("publish_job", job_id=job["id"])["status"], "not_queued")
        self.assertEqual(self.adapter.sent, [job["id"]])
        blocked = self.draft("blocked")
        self.call("queue_job", job_id=blocked["id"])
        self.assertEqual(self.call("publish_job", job_id=blocked["id"])["status"], "not_claimable")
        with self.assertRaises(ValueError):
            self.call("retry_job", job_id=job["id"], reason="guess")

    def test_reconciliation_verifies_provider_text_and_expired_lease(self):
        job = self.draft()
        self.call("queue_job", job_id=job["id"])
        with Outbox(self.db) as box:
            box.claim_job(job["id"], platform="instagram")
        with self.assertRaisesRegex(ToolInputError, "uncertain"):
            self.call("reconcile_job", job_id=job["id"], outcome="published", provider_id="9000", reason="observed")
        with Outbox(self.db) as box:
            box.db.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id=?", (job["id"],))
        with patch.object(self.client, "media_info_v1", return_value=self.client.post(caption="Wrong", pk="9000")):
            with self.assertRaisesRegex(ToolInputError, "does not match"):
                self.call("reconcile_job", job_id=job["id"], outcome="published", provider_id="9000", reason="observed")
        self.assertEqual(self.call("job", job_id=job["id"])["job"]["status"], "uncertain")
        result = self.call("reconcile_job", job_id=job["id"], outcome="published", provider_id="9000", reason="Observed own post and exact caption")
        self.assertEqual(result["job"]["status"], "published")
        self.assertEqual(result["job"]["provider_id"], "9000")
        self.assertEqual(self.adapter.sent, [])

    def test_reconcile_keeps_the_expired_token_so_a_late_receipt_still_records(self):
        job = self.draft()
        self.call("queue_job", job_id=job["id"])
        with Outbox(self.db) as box:
            claimed = box.claim_job(job["id"], platform="instagram")
            box.db.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id=?", (job["id"],))
        with patch.object(self.client, "media_info_v1", return_value=self.client.post(caption="Wrong", pk="9000")):
            with self.assertRaisesRegex(ToolInputError, "does not match"):
                self.call("reconcile_job", job_id=job["id"], outcome="published", provider_id="9000", reason="seen")
        with Outbox(self.db) as box:
            self.assertEqual(box.get(job["id"])["status"], "uncertain")
            late = box.finish(job["id"], claimed["lease_token"], outcome="published", provider_id="9000")
        self.assertEqual((late["status"], late["provider_id"]), ("published", "9000"))

    def test_cancelled_jobs_are_a_listable_state_with_a_next_step(self):
        self.assertIn("cancelled", instagram_mcp.JOB_STATES)
        job = self.draft()
        with Outbox(self.db) as box:
            box.cancel(job["id"])
        listed = self.call("jobs", status="cancelled")["jobs"]
        self.assertEqual([item["id"] for item in listed], [job["id"]])
        error, result = self.rpc("publish_job", {"job_id": job["id"]})
        self.assertTrue(error)
        self.assertIn("instagram_draft_carousel", result["error"]["hint"])

    def test_outbox_refusals_get_tool_hints_instead_of_cli_commands(self):
        job = self.draft()
        self.call("queue_job", job_id=job["id"])
        error, result = self.rpc("retry_job", {"job_id": job["id"], "reason": "fixed"})
        self.assertTrue(error)
        self.assertEqual(result["error"]["code"], "not_failed")
        self.assertIn("instagram_job", result["error"]["hint"])
        self.assertNotIn("ghost-in-the-box outbox", result["error"]["hint"])

    def test_only_publishing_is_marked_destructive(self):
        destructive = {item["name"] for item in TOOLS if item["annotations"]["destructiveHint"]}
        self.assertEqual(destructive, {"instagram_publish_job"})

    def test_publisher_statuses_all_have_messages_and_hints(self):
        for status in ("rejected", "receipt_not_recorded", "lease_lost", "outbox_refused", "outbox_unavailable"):
            with self.subTest(status=status):
                message, hint = instagram_mcp.PUBLISH_ERRORS[status]
                self.assertTrue(message and hint)
                failure = instagram_mcp._structured_failure({"ok": False, "status": status}, "mine")
                self.assertEqual(failure["error"]["code"], status)

    def test_comments_persist_then_claim_and_reply_are_idempotent(self):
        result = self.call("comments", media_id="42_7")
        self.assertEqual(result["next_cursor"], "next-comments")
        unread = self.call("inbox")["items"]
        self.assertEqual([item["comment_id"] for item in unread], ["123"])
        self.assertEqual(self.call("inbox")["items"], unread)
        self.call("claim_comments", worker="operator")
        with self.assertRaises(ValueError):
            self.call("draft_reply", comment_id="123", text="Exact reply", worker="other")
        draft = self.call("draft_reply", comment_id="123", text="Exact reply", worker="operator")["job"]
        repeated = self.call("draft_reply", comment_id="123", text="Exact reply", worker="operator")["job"]
        self.assertEqual(draft["id"], repeated["id"])
        self.assertEqual((draft["media_id"], draft["target_id"], draft["body"]), ("42_7", "123", "Exact reply"))
        with self.assertRaises(ValueError):
            self.call("draft_reply", comment_id="123", text="Different reply", worker="operator")

    def test_reads_reject_truncated_pages_and_other_accounts_comments(self):
        self.client.page.append(self.client.post(owner="8"))
        result = self.call("own_posts")
        self.assertEqual((len(result["posts"]), result["other_authors_skipped"]), (1, 1))
        self.assertEqual(result["next_cursor"], "next-page")
        with patch.object(self.client, "user_medias_paginated_v1", return_value=([], "bad")):
            with self.assertRaisesRegex(ToolInputError, "complete"):
                self.call("own_posts")
        with patch.object(self.client, "media_info_v1", return_value=self.client.post(owner="8")):
            with self.assertRaisesRegex(ToolInputError, "own media"):
                self.call("comments", media_id="42")
        with closing(Inbox(self.inbox_db)) as inbox:
            self.assertEqual(inbox.peek("mine"), [])

    def test_schema_validation_precedes_side_effects_and_errors_do_not_leak(self):
        for arguments in ({"posts": True}, {"posts": 999}, {"extra": "secret"}, []):
            response = dispatch(self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "instagram_sync_inbox", "arguments": arguments}})
            self.assertTrue(response["result"]["isError"])
        with patch.object(self.service, "status", side_effect=RuntimeError("private_sentinel")):
            error, result = self.rpc("status")
        self.assertTrue(error)
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertEqual(result["error"]["code"], "internal_error")
        self.assertTrue(result["error"]["hint"])
        self.assertNotIn("private_sentinel", json.dumps(result))
        self.assertIsNone(dispatch(self.service, {"jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "instagram_publish_job", "arguments": {"job_id": "ignored"}}}))
        self.assertEqual(self.adapter.sent, [])

    def test_framed_subprocess_handshake_discovery_and_local_status(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "instagram_status", "arguments": {}}},
        ]
        request = io.BytesIO()
        for message in messages:
            write_message(request, message)
        process = subprocess.run([sys.executable, "scripts/instagram_mcp.py", "--account", "mine",
            "--outbox-db", str(self.db), "--inbox-db", str(self.inbox_db)],
            input=request.getvalue(), capture_output=True, timeout=15, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(process.returncode, 0, process.stderr)
        stream = io.BytesIO(process.stdout)
        replies = [read_message(stream)[0] for _ in range(3)]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "ghost-in-the-box-instagram")
        self.assertEqual(len(replies[1]["result"]["tools"]), len(TOOLS))
        self.assertTrue(json.loads(replies[2]["result"]["content"][0]["text"])["session_ready"])
        self.assertEqual(read_message(stream)[0], None)
        self.assertEqual(process.stderr, b"")

    def test_unicode_framing_ndjson_and_malformed_messages(self):
        message = {"jsonrpc": "2.0", "id": "café", "method": "ping"}
        for framing in ("framed", "lines"):
            source, output = io.BytesIO(), io.BytesIO()
            write_message(source, message, framing)
            source.seek(0)
            self.assertEqual(serve(self.service, source, output), 0)
            output.seek(0)
            response, format_returned = read_message(output)
            self.assertEqual(response["id"], "café")
            self.assertEqual(format_returned, framing)
        for raw in (b"Content-Length: 1048577\r\n\r\n", b"Content-Length: 4\r\n\r\n{}"):
            output = io.BytesIO()
            self.assertEqual(serve(self.service, io.BytesIO(raw), output), 2)
            output.seek(0)
            self.assertEqual(read_message(output)[0]["error"]["code"], -32700)

    def test_url_media_reads_and_private_ordered_carousel_downloads(self):
        url = "https://www.instagram.com/p/Example/?igsh=tracking"
        self.assertEqual(self.call("media", url=url)["media"]["provider_id"], "42")
        result = self.call("download_media", url=url, max_images=1)
        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["next_start_index"], 2)
        self.assertEqual(self.client.last_post_url, "https://www.instagram.com/p/Example/")
        self.assertEqual((result["source_url"], result["author"], result["caption"]),
                         ("https://www.instagram.com/p/Example/", "mine", "Caption"))
        path = Path(result["image_paths"][0])
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(path.parent.stat().st_mode & 0o077, 0)
        rest = self.call("download_media", media_id="42_7", start_index=2)
        self.assertTrue(rest["complete"])
        self.assertEqual(rest["downloads"][0]["slide_index"], 2)
        self.assertEqual(len(json.loads(Path(rest["metadata_path"]).read_text())["downloads"]), 2)
        cached = self.call("download_media", media_id="42")
        self.assertEqual(len(self.fetches), 2)
        self.assertTrue(all(item["cached"] for item in cached["downloads"]))
        self.assertEqual([item["slide_id"] for item in cached["downloads"]], ["1", "2"])
        path.write_bytes(b"changed")
        self.call("download_media", media_id="42")
        self.assertEqual(len(self.fetches), 3)
        self.assertEqual(self.adapter.sent, [])

    def test_download_failures_return_partial_progress_and_can_resume(self):
        original = self.service.image_fetcher
        def failing(url, destination, byte_limit, **kwargs):
            if url.endswith("2.jpg"):
                destination.write_bytes(b"partial")
                raise RuntimeError("private provider body")
            return original(url, destination, byte_limit, **kwargs)
        self.service.image_fetcher = failing
        error, result = self.rpc("download_media", {"media_id": "42"})
        self.assertTrue(error)
        self.assertEqual(len(result["image_paths"]), 1)
        self.assertEqual(result["next_start_index"], 2)
        self.assertEqual(result["error"]["code"], "download_incomplete")
        self.assertIn("start_index=2", result["error"]["hint"])
        self.assertNotIn("private provider body", json.dumps(result))
        self.assertEqual(list(Path(result["directory"]).glob(".image-*")), [])
        self.assertEqual(len(self.call("references")["posts"][0]["image_paths"]), 1)
        self.service.image_fetcher = original
        self.assertTrue(self.call("download_media", media_id="42", start_index=2)["complete"])

    def test_discovery_is_one_bounded_page_and_curation_persists(self):
        self.client.page = [self.client.post(owner="80", pk=str(i)) for i in range(3)]
        found = self.call("discover", hashtag="#SoftwareEngineering", sort="recent", limit=2)
        self.assertEqual(found["saved_count"], 2)
        self.assertEqual(self.client.hashtag_calls, [("softwareengineering", 2, "recent", None)])
        self.assertEqual(self.fetches, [])
        self.call("curate", media_id="1", state="selected", note="Strong visual example")
        self.call("discover", hashtag="softwareengineering", limit=2)
        reopened = InstagramTools(account="mine", reference_root=self.root / "references")
        selected = reopened.call("instagram_references", {"state": "selected", "hashtag": "softwareengineering"})
        self.assertEqual(selected["posts"][0]["note"], "Strong visual example")
        self.assertEqual({item["mode"] for item in selected["posts"][0]["discovered_via"]}, {"top", "recent"})
        page = self.call("references", state="all", limit=1)
        self.assertEqual((len(page["posts"]), page["total"], page["next_offset"]), (1, 2, 1))
        self.assertEqual(len(self.call("references", state="all", offset=1)["posts"]), 1)
        self.assertEqual(self.call("references", state="all", query="no match")["total"], 0)
        self.assertEqual(self.adapter.sent, [])

    def test_download_validation_precedes_reads_and_vision_only_sees_registered_images(self):
        with patch.object(self.service, "read") as read:
            for arguments in ({}, {"media_id": "42", "url": "https://www.instagram.com/p/Example/"},
                              {"url": "https://other.test/p/Example/"},
                              {"url": "https://www.instagram.com/share/p/Example/"}):
                with self.assertRaises((ToolInputError, ReferenceError)):
                    self.call("download_media", **arguments)
            read.assert_not_called()
        result = self.call("download_media", media_id="42", max_images=1)
        calls = []
        def inspect_image(path, question, **kwargs):
            calls.append((path, question, kwargs))
            return {"ok": True, "image": str(path), "answer": "A visual argument about programming."}
        self.service.vision_inspector = inspect_image
        inspected = self.call("inspect_slide", image_path=result["image_paths"][0],
                              question="Read the words", max_tokens=300)
        self.assertEqual(inspected["slide_id"], "1")
        self.assertEqual(calls[0][1:], ("Read the words", {"max_tokens": 300}))
        with self.assertRaises(ReferenceError):
            self.call("inspect_slide", image_path=str(self.images[0]))
        Path(result["image_paths"][0]).write_bytes(b"tampered")
        with self.assertRaises(ReferenceError):
            self.call("inspect_slide", image_path=result["image_paths"][0])
        self.assertEqual(len(calls), 1)

    def test_failed_calls_carry_code_message_and_hint(self):
        error, result = self.rpc("job", {"job_id": "missing"})
        self.assertTrue(error)
        self.assertEqual(result["error"]["code"], "job_not_found")
        self.assertIn("instagram_jobs", result["error"]["hint"])
        error, result = self.rpc("sync_inbox", {"posts": 999})
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertIn("inputSchema", result["error"]["hint"])
        draft = self.draft()
        error, result = self.rpc("publish_job", {"job_id": draft["id"]})
        self.assertTrue(error)
        self.assertEqual((result["status"], result["error"]["code"]), ("not_queued", "not_queued"))
        self.assertIn("instagram_queue_job", result["error"]["hint"])
        error, result = self.rpc("queue_job", {"job_id": draft["id"], "at": "tomorrow"})
        self.assertTrue(error)
        self.assertNotEqual(result["error"]["code"], "internal_error")
        self.assertTrue(result["error"]["message"])
        self.assertEqual(self.call("job", job_id=draft["id"])["job"]["status"], "draft")
        for response in (self.rpc("job", {"job_id": "missing"})[1], result):
            self.assertEqual(set(response["error"]), {"code", "message", "hint"})
        self.assertEqual(self.adapter.sent, [])

    def test_publisher_results_get_tool_hints_with_or_without_an_envelope(self):
        legacy = instagram_mcp._structured_failure({"ok": False, "status": "uncertain", "job_id": "j"}, "mine")
        self.assertEqual((legacy["status"], legacy["error"]["code"]), ("uncertain", "uncertain"))
        self.assertIn("instagram_reconcile_job", legacy["error"]["hint"])
        auth = instagram_mcp._structured_failure({"ok": False, "status": "auth_unavailable"}, "mine")
        self.assertIn("--account mine", auth["error"]["hint"])
        coded = instagram_mcp._structured_failure({"ok": False, "status": "not_queued", "job_status": "failed",
            "error": {"code": "not_queued", "message": "Job j is failed, not queued.", "hint": "cli"}}, "mine")
        self.assertEqual(coded["error"]["message"], "Job j is failed, not queued.")
        self.assertIn("instagram_retry_job", coded["error"]["hint"])
        unknown = instagram_mcp._structured_failure({"ok": False, "status": "brand_new",
            "error": {"code": "brand_new_code", "message": "m", "hint": "publisher hint"}}, "mine")
        self.assertEqual((unknown["error"]["code"], unknown["error"]["hint"]), ("brand_new_code", "publisher hint"))
        self.assertEqual(instagram_mcp._structured_failure({"ok": True}, "mine"), {"ok": True})

    def test_session_failures_name_the_login_command_for_this_account(self):
        def unavailable(*_args, **_kwargs):
            raise InstagramSessionError("session_unavailable")
        self.service.reader = unavailable
        error, result = self.rpc("own_posts")
        self.assertTrue(error)
        self.assertEqual(result["error"]["code"], "session_unavailable")
        self.assertIn("login start --platform instagram --account mine", result["error"]["hint"])
        self.service.reader = lambda *_args, **_kwargs: (_ for _ in ()).throw(InstagramSessionError("<odd text>"))
        self.assertEqual(self.rpc("own_posts")[1]["error"]["code"], "session_unavailable")

    def test_missing_instagram_library_is_a_dependency_error_not_a_crash(self):
        with patch.dict(sys.modules, {"instagrapi": None}):
            with self.assertRaises(InstagramSessionError) as raised:
                instagram_mcp._new_client()
        self.assertEqual(str(raised.exception), "dependency_unavailable")
        self.service.reader = lambda *_args, **_kwargs: instagram_mcp._new_client()
        with patch.dict(sys.modules, {"instagrapi": None}):
            error, result = self.rpc("own_posts")
        self.assertTrue(error)
        self.assertEqual(result["error"]["code"], "dependency_unavailable")
        self.assertIn(config.install_command("instagram"), result["error"]["hint"])

    def test_status_without_a_session_explains_how_to_sign_in(self):
        offline = InstagramTools(account="nobody_yet", outbox_path=self.db, registry=self.registry)
        result = offline.call("instagram_status", {})
        self.assertFalse(result["session_ready"])
        self.assertIn("--account nobody_yet", result["hint"])
        self.assertNotIn("hint", self.call("status"))

    def test_placeholder_account_fails_every_tool_loudly(self):
        placeholder = InstagramTools(account="example_creator", outbox_path=self.db, inbox_path=self.inbox_db,
                                     registry=self.registry, reference_root=self.root / "references")
        for name, arguments in (("instagram_status", {}), ("instagram_inbox", {}), ("instagram_jobs", {})):
            response = dispatch(placeholder, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": name, "arguments": arguments}})
            result = json.loads(response["result"]["content"][0]["text"])
            self.assertTrue(response["result"]["isError"])
            self.assertEqual(result["error"]["code"], "placeholder_account")
        self.assertFalse(self.inbox_db.exists())
        session = self.root / "real-example-session.json"
        session.write_text("{}")
        session.chmod(0o600)
        self.registry.register(platform="instagram", account="example_creator", kind="instagrapi_settings",
                               session_file=session)
        self.assertTrue(placeholder.call("instagram_status", {})["ok"])

    def test_mcp_publish_still_sends_a_future_scheduled_job_on_request(self):
        job = self.draft()
        self.call("queue_job", job_id=job["id"], at="2999-01-01T09:00:00+00:00")
        result = self.call("publish_job", job_id=job["id"])
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.adapter.sent, [job["id"]])

    def test_tools_have_titles_for_old_and_new_clients(self):
        self.assertEqual(len({item["title"] for item in TOOLS}), len(TOOLS))
        for item in TOOLS:
            self.assertTrue(item["title"].strip())
            self.assertEqual(item["annotations"]["title"], item["title"])


class ProtocolNegotiationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.service = InstagramTools(account="mine", registry=AccountRegistry(Path(self.temp.name) / "a.json"))

    def tearDown(self):
        self.temp.cleanup()

    def initialize(self, params, session=None):
        return dispatch(self.service, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params},
                        session)["result"]

    def test_supported_versions_are_echoed_and_others_get_the_latest(self):
        for version in SUPPORTED_PROTOCOL_VERSIONS:
            self.assertEqual(self.initialize({"protocolVersion": version})["protocolVersion"], version)
        for params in ({"protocolVersion": "1999-01-01"}, {"protocolVersion": ["2025-06-18"]}, {}):
            self.assertEqual(self.initialize(params)["protocolVersion"], LATEST_PROTOCOL_VERSION)
        self.assertEqual(LATEST_PROTOCOL_VERSION, "2025-06-18")

    def test_server_version_comes_from_package_metadata(self):
        info = self.initialize({"protocolVersion": "2025-06-18"})["serverInfo"]
        self.assertEqual(info["name"], "ghost-in-the-box-instagram")
        self.assertTrue(info["title"])
        self.assertEqual(info["version"], config.package_version())
        self.assertEqual(server_version(), config.package_version())
        self.assertNotEqual(info["version"], "1.0.0")

    def test_server_version_falls_back_to_the_project_file_then_unknown(self):
        missing = config.importlib.metadata.PackageNotFoundError("ghost-in-the-box")
        with patch.object(config.importlib.metadata, "version", side_effect=missing):
            self.assertEqual(server_version(), project_version())
            with patch.object(Path, "open", side_effect=OSError("unreadable")):
                self.assertEqual(server_version(), "0+unknown")

    def test_batches_are_accepted_only_after_negotiating_2025_03_26(self):
        batch = [{"jsonrpc": "2.0", "id": 2, "method": "ping"},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": {}}]
        for version, accepted in (("2025-03-26", True), ("2025-06-18", False), ("2024-11-05", False)):
            with self.subTest(version=version):
                source, output = io.BytesIO(), io.BytesIO()
                write_message(source, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": version}}, "lines")
                write_message(source, batch, "lines")
                write_message(source, [{"jsonrpc": "2.0", "method": "notifications/initialized"}], "lines")
                source.seek(0)
                self.assertEqual(serve(self.service, source, output), 0)
                replies = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(replies[0]["result"]["protocolVersion"], version)
                if accepted:
                    self.assertEqual(len(replies), 2)
                    self.assertEqual([item["id"] for item in replies[1]], [2, 3, 4])
                    self.assertEqual(len(replies[1][1]["result"]["tools"]), len(TOOLS))
                    self.assertEqual(replies[1][2]["error"]["code"], -32600)
                else:
                    self.assertEqual(len(replies), 3)
                    self.assertTrue(all(reply["error"]["code"] == -32600 for reply in replies[1:]))

    def test_blank_lines_between_newline_messages_are_ignored(self):
        source, output = io.BytesIO(b"\n\r\n" + b'{"jsonrpc":"2.0","id":5,"method":"ping"}\n\n'), io.BytesIO()
        self.assertEqual(serve(self.service, source, output), 0)
        self.assertEqual(json.loads(output.getvalue())["id"], 5)

    def test_malformed_newline_json_is_answered_in_the_callers_framing(self):
        source = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n{"broken"\n')
        output = io.BytesIO()
        self.assertEqual(serve(self.service, source, output), 2)
        lines = output.getvalue().splitlines()
        self.assertEqual(json.loads(lines[1])["error"]["code"], -32700)


def project_version():
    import tomllib
    with open(Path(__file__).resolve().parents[1] / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["version"]


class AccountSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = AccountRegistry(self.root / "accounts.json")

    def tearDown(self):
        self.temp.cleanup()

    def register(self, platform, account):
        session = self.root / f"{platform}-{account}.json"
        session.write_text("{}")
        session.chmod(0o600)
        self.registry.register(platform=platform, account=account, kind="instagrapi_settings", session_file=session)

    def resolve(self, explicit=None, environ=None):
        return resolve_account(explicit, environ=environ or {}, registry_factory=lambda: self.registry)

    def test_explicit_and_environment_accounts_win(self):
        self.assertEqual(self.resolve("not_registered_yet"), "not_registered_yet")
        self.assertEqual(self.resolve(environ={"SOCIAL_FARM_IG_ACCOUNT": "from_env"}), "from_env")
        self.assertEqual(self.resolve("flag", {"SOCIAL_FARM_IG_ACCOUNT": "from_env"}), "flag")
        with self.assertRaisesRegex(AccountSelectionError, "Instagram handle"):
            self.resolve("@not a handle")

    def test_the_only_registered_instagram_account_is_used(self):
        with self.assertRaises(AccountSelectionError) as raised:
            self.resolve()
        self.assertIn("login start --platform instagram", raised.exception.hint)
        self.register("threads", "threads_only")
        with self.assertRaisesRegex(AccountSelectionError, "no Instagram account"):
            self.resolve()
        self.register("instagram", "only_one")
        self.assertEqual(self.resolve(), "only_one")
        self.register("instagram", "second")
        with self.assertRaisesRegex(AccountSelectionError, "only_one, second") as raised:
            self.resolve()
        self.assertIn("--account", raised.exception.hint)

    def test_placeholders_are_refused_unless_really_registered(self):
        for handle in ("example_creator", "YOUR_HANDLE"):
            with self.assertRaisesRegex(AccountSelectionError, "placeholder"):
                self.resolve(handle)
            with self.assertRaisesRegex(AccountSelectionError, "placeholder"):
                self.resolve(environ={"SOCIAL_FARM_IG_ACCOUNT": handle})
        self.register("instagram", "example_creator")
        self.assertEqual(self.resolve("example_creator"), "example_creator")

    def test_unreadable_registry_is_reported_without_contents(self):
        self.registry.path.write_text('{"schema": 99, "secret": "private_sentinel"}')
        with self.assertRaises(AccountSelectionError) as raised:
            self.resolve()
        self.assertNotIn("private_sentinel", str(raised.exception) + raised.exception.hint)
        self.assertIn("doctor", raised.exception.hint)

    def test_server_refuses_to_start_without_a_resolvable_account(self):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("SOCIAL_FARM_IG_ACCOUNT", "SOCIAL_FARM_IG_PYTHON")}
        environment.update(SOCIAL_FARM_ACCOUNTS_FILE=str(self.registry.path), HOME=str(self.root))
        for arguments, expected in (([], "no Instagram account"), (["--account", "example_creator"], "placeholder")):
            with self.subTest(arguments=arguments):
                process = subprocess.run([sys.executable, "scripts/instagram_mcp.py", *arguments],
                    input=b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n', capture_output=True, timeout=15,
                    env=environment, cwd=Path(__file__).resolve().parents[1])
                self.assertEqual(process.returncode, 2)
                self.assertEqual(process.stdout, b"")
                self.assertIn(expected, process.stderr.decode())
                self.assertIn("hint:", process.stderr.decode())


class ToolCatalogTests(unittest.TestCase):
    def test_catalog_summarizes_every_tool_with_required_parameters(self):
        entries = tool_catalog()
        self.assertEqual([entry["name"] for entry in entries], [item["name"] for item in TOOLS])
        by_name = {entry["name"]: entry for entry in entries}
        self.assertEqual(by_name["instagram_draft_reply"]["required"], ["comment_id", "text", "worker"])
        self.assertEqual(by_name["instagram_status"]["optional"], ["live_check"])
        self.assertEqual(by_name["instagram_status"]["description"],
                         "Check configured Instagram account, local session readiness and job counts.")
        for entry in entries:
            self.assertNotIn("\n", entry["description"])
            self.assertTrue(entry["description"].endswith("."))

    def test_substack_tools_are_read_from_source_without_importing_the_extra(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "server.py"
            source.write_text('import substack_mcp\n\n@mcp.tool()\ndef connection_status() -> dict:\n'
                              '    """Read setup. More detail."""\n\n@mcp.tool()\n'
                              'def lookup(slug, *, limit=5, mode):\n    """Find a post."""\n\n'
                              'def helper():\n    pass\n')
            tools = substack_catalog(source)
            self.assertEqual([tool["name"] for tool in tools], ["connection_status", "lookup"])
            self.assertEqual(tools[0]["description"], "Read setup.")
            self.assertEqual((tools[1]["required"], tools[1]["optional"]), (["slug", "mode"], ["limit"]))
            self.assertEqual(substack_catalog(Path(directory) / "missing.py"), [])
            self.assertEqual([server["server"] for server in catalog(Path(directory) / "missing.py")["servers"]],
                             ["ghost-in-the-box-instagram"])
        real = {tool["name"] for tool in substack_catalog()}
        self.assertIn("connection_status", real)

    def test_markdown_has_one_escaped_row_per_tool(self):
        data = catalog()
        markdown = catalog_markdown(data)
        rows = [line for line in markdown.splitlines() if line.startswith("| `")]
        self.assertEqual(len(rows), sum(len(server["tools"]) for server in data["servers"]))
        self.assertIn("| `instagram_publish_job` | Publish a queued job |", markdown)
        self.assertIn(data["version"], markdown)
        piped = catalog_markdown({"version": "1", "protocol_versions": ["x"], "servers": [{
            "server": "s", "command": "c", "tools": [{"name": "t", "title": "a|b", "description": "d",
                "required": [], "optional": [], "read_only": True, "contacts_provider": False}]}]})
        self.assertIn("| a\\|b |", piped)

    def test_list_tools_command_formats(self):
        root = Path(__file__).resolve().parents[1]
        def run(*arguments):
            process = subprocess.run([sys.executable, "scripts/instagram_mcp.py", "--list-tools", *arguments],
                                     capture_output=True, timeout=15, cwd=root)
            self.assertEqual(process.returncode, 0, process.stderr)
            return process.stdout.decode()
        self.assertEqual(json.loads(run())["servers"][0]["tools"], tool_catalog())
        self.assertEqual(json.loads(run("--format", "mcp")), {"tools": TOOLS})
        self.assertTrue(run("--format", "md").startswith("# MCP tool catalog"))

if __name__ == "__main__":
    unittest.main()
