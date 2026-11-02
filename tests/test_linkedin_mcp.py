from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from _env import IsolatedStateTestCase
from farm import linkedin_mcp, mcp_server
from farm.accounts import AccountRegistry
from farm.adapters.linkedin import (
    SHARE_QUERY_ENV, AuthUnavailable, LinkedInAdapter, LinkedInReadFailure,
)
from farm.linkedin_mcp import (
    TOOLS, AccountSelectionError, LinkedInTools, catalog, dispatch, resolve_account, serve,
)
from farm.login import save_login
from farm.outbox import Outbox, utc_now, utc_text
from scripts import mcp_tools
from test_linkedin_adapter import SHARED, png_bytes, response
from test_login import session

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "jane-doe-123"
ME = {"miniProfile": {"publicIdentifier": ACCOUNT, "entityUrn": "urn:li:fs_miniProfile:ABC123"}}
READ_ONLY = {"linkedin_status", "linkedin_me", "linkedin_profile", "linkedin_posts", "linkedin_conversations",
             "linkedin_messages", "linkedin_list_jobs", "linkedin_show_job", "linkedin_dry_run"}
DESTRUCTIVE = {"linkedin_publish_job", "linkedin_cancel_job"}
CONTACTS_LINKEDIN = {"linkedin_me", "linkedin_profile", "linkedin_posts", "linkedin_conversations",
                     "linkedin_messages", "linkedin_publish_job"}


class FakeReader:
    """The adapter's read surface, recording each call; error makes every read raise it."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []
        self.http = SimpleNamespace(close=Mock())

    def _answer(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.error:
            raise self.error
        return {"route": name, "elements": [{"id": 1}]}

    def me(self, account):
        self.calls.append(("me", (account,), {}))
        if self.error:
            raise self.error
        return {"username": account, "id": "ABC123", "author_urn": "urn:li:person:ABC123"}

    def profile(self, account, handle):
        return self._answer("profile", account, handle)

    def own_posts(self, account, *, limit, start):
        return self._answer("own_posts", account, limit=limit, start=start)

    def conversations(self, account, *, limit, start):
        return self._answer("conversations", account, limit=limit, start=start)

    def messages(self, account, conversation_id, *, limit, start):
        return self._answer("messages", account, conversation_id, limit=limit, start=start)

    def publish(self, job, *, before_send=None):
        raise AssertionError("FakeReader never publishes")


class LinkedInMCPTestCase(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.isolated_state
        self.registry = AccountRegistry()
        self.entry = save_login("linkedin", ACCOUNT, session("linkedin", ACCOUNT),
                                registry=self.registry, state_root=self.root)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(SHARE_QUERY_ENV, None)
        self.db = self.root / "outbox.sqlite3"
        self.reader = FakeReader()
        self.http = Mock(cookies=requests.cookies.RequestsCookieJar())
        self.order = []
        self.http.get.side_effect = self.identity_answer
        self.http.post.side_effect = self.share_answer
        self.share_reply = response(SHARED)
        self.adapter_factory = lambda: self.reader
        self.service = LinkedInTools(account=ACCOUNT, outbox_path=self.db, registry=self.registry,
                                     adapter_factory=lambda: self.adapter_factory())

    def identity_answer(self, *args, **kwargs):
        self.order.append("identity")
        return response(ME)

    def share_answer(self, *args, **kwargs):
        self.order.append("share")
        if isinstance(self.share_reply, Exception):
            raise self.share_reply
        return self.share_reply

    def use_real_adapter(self):
        """Publish through the real LinkedInAdapter, with the HTTP session mocked."""
        adapter = LinkedInAdapter(self.registry, self.http)
        original = adapter.publish

        def publish(job, *, before_send=None):
            def gate():
                self.order.append("before_send")
                before_send()
            return original(job, before_send=gate)

        adapter.publish = publish
        self.adapter_factory = lambda: adapter
        return adapter

    def call(self, name, **arguments):
        return self.service.call("linkedin_" + name, arguments)

    def rpc(self, name, arguments=None, service=None):
        reply = dispatch(service or self.service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                   "params": {"name": "linkedin_" + name,
                                                              "arguments": arguments or {}}})
        result = reply["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    def draft(self, text="Exact LinkedIn text", **extra):
        return self.call("draft_post", text=text, **extra)

    def queued(self, text="Exact LinkedIn text", **extra):
        job_id = self.draft(text, **extra)["job_id"]
        self.call("queue_job", job_id=job_id)
        return job_id

    def stored(self, job_id):
        with Outbox(self.db) as box:
            return box.get(job_id)


class ProtocolTests(LinkedInMCPTestCase):
    def test_initialize_negotiates_and_names_the_linkedin_server(self):
        for version in mcp_server.SUPPORTED_PROTOCOL_VERSIONS:
            reply = dispatch(self.service, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                            "params": {"protocolVersion": version}})["result"]
            self.assertEqual(reply["protocolVersion"], version)
        reply = dispatch(self.service, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                        "params": {"protocolVersion": "1999-01-01"}})["result"]
        self.assertEqual(reply["protocolVersion"], mcp_server.LATEST_PROTOCOL_VERSION)
        self.assertEqual(reply["serverInfo"]["name"], "ghost-in-the-box-linkedin")
        self.assertEqual(reply["serverInfo"]["version"], mcp_server.server_version())
        self.assertIn("linkedin_status", reply["instructions"])
        self.assertIn("tools", reply["capabilities"])

    def test_tools_list_has_titles_schemas_and_annotations(self):
        tools = dispatch(self.service, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], [tool["name"] for tool in TOOLS])
        self.assertEqual(len(tools), 15)
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["name"].startswith("linkedin_"))
                self.assertTrue(tool["title"])
                self.assertEqual(tool["annotations"]["title"], tool["title"])
                self.assertGreater(len(tool["description"]), 60)
                self.assertFalse(tool["inputSchema"]["additionalProperties"])
                for required in tool["inputSchema"]["required"]:
                    self.assertIn(required, tool["inputSchema"]["properties"])

    def test_read_only_and_destructive_annotations(self):
        annotations = {tool["name"]: tool["annotations"] for tool in TOOLS}
        self.assertEqual({name for name, value in annotations.items() if value["readOnlyHint"]}, READ_ONLY)
        self.assertEqual({name for name, value in annotations.items() if value["destructiveHint"]}, DESTRUCTIVE)
        self.assertEqual({name for name, value in annotations.items() if value["openWorldHint"]}, CONTACTS_LINKEDIN)
        self.assertFalse(READ_ONLY & DESTRUCTIVE)

    def test_unknown_tools_and_bad_arguments_are_structured(self):
        reply = dispatch(self.service, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                        "params": {"name": "instagram_status", "arguments": {}}})
        self.assertEqual(reply["error"]["code"], -32602)
        failed, result = self.rpc("posts", {"limit": 26})
        self.assertTrue(failed)
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertIn("linkedin_posts", result["error"]["hint"])
        failed, result = self.rpc("draft_post", {"text": "x" * 3001})
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        failed, result = self.rpc("draft_post", {"text": "hello", "surprise": 1})
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertEqual(self.reader.calls, [])

    def test_serve_answers_newline_json_and_content_length_frames(self):
        for framing in ("lines", "framed"):
            with self.subTest(framing=framing):
                source, output = io.BytesIO(), io.BytesIO()
                mcp_server.write_message(source, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                  "params": {"protocolVersion": "2025-06-18"}}, framing)
                mcp_server.write_message(source, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "linkedin_status", "arguments": {}}}, framing)
                source.seek(0)
                self.assertEqual(serve(self.service, source, output), 0)
                output.seek(0)
                first, mode = mcp_server.read_message(output)
                self.assertEqual(mode, framing)
                self.assertEqual(first["result"]["serverInfo"]["name"], "ghost-in-the-box-linkedin")
                status = json.loads(mcp_server.read_message(output)[0]["result"]["content"][0]["text"])
                self.assertEqual(status["account"], ACCOUNT)


class StatusAndReadTests(LinkedInMCPTestCase):
    def test_status_is_local_and_reveals_no_session_material(self):
        self.adapter_factory = Mock(side_effect=AssertionError("status must not build an adapter"))
        self.queued()
        with patch.object(requests.Session, "request", side_effect=AssertionError("network")):
            failed, result = self.rpc("status")
        self.assertFalse(failed)
        self.assertEqual(result["account"], ACCOUNT)
        self.assertEqual(result["registry_kind"], "cookie_jar")
        self.assertEqual(result["session"], {"present": True, "private": True, "regular_file": True,
                                             "cookies_present": True})
        self.assertEqual(result["transport"], {"ready": True, "share_query_id": "default",
                                               "share_query_id_valid": True, "problems": []})
        self.assertEqual(result["jobs"], {"queued": 1})
        self.assertEqual(result["network_requests"], 0)
        text = json.dumps(result)
        self.assertNotIn("TEST_COOKIE", text)
        self.assertNotIn("ajax:123", text)
        self.assertNotIn(self.entry["session_file"], text)
        self.assertNotIn("hint", result)

    def test_status_names_what_to_fix(self):
        Path(self.entry["session_file"]).chmod(0o644)
        result = self.call("status")
        self.assertEqual(result["transport"]["problems"], ["session_not_private"])
        self.assertIn("doctor", result["hint"])
        Path(self.entry["session_file"]).chmod(0o600)
        os.environ[SHARE_QUERY_ENV] = "not-a-query-id"
        result = self.call("status")
        self.assertEqual(result["transport"]["share_query_id"], "environment")
        self.assertEqual(result["transport"]["problems"], ["share_query_id_invalid"])
        self.assertIn(SHARE_QUERY_ENV, result["hint"])
        del os.environ[SHARE_QUERY_ENV]
        other = LinkedInTools(account="someone-else", outbox_path=self.db, registry=self.registry)
        result = other.call("linkedin_status", {})
        self.assertFalse(result["registered"])
        self.assertEqual(result["transport"]["problems"], ["account_not_registered"])
        self.assertIn("login start --platform linkedin --account someone-else", result["hint"])

    def test_every_read_returns_structured_json_and_closes_the_session(self):
        self.assertEqual(self.call("me")["identity"]["username"], ACCOUNT)
        profile = self.call("profile", handle="other-member")
        self.assertEqual(profile["profile"]["route"], "profile")
        posts = self.call("posts", limit=5, start=10)
        self.assertEqual((posts["posts"]["route"], posts["next_start"]), ("own_posts", 15))
        conversations = self.call("conversations")
        self.assertEqual(conversations["conversations"]["route"], "conversations")
        messages = self.call("messages", conversation_id="2-ABC==", limit=3)
        self.assertEqual(messages["messages"]["route"], "messages")
        self.assertEqual(self.reader.calls, [
            ("me", (ACCOUNT,), {}),
            ("profile", (ACCOUNT, "other-member"), {}),
            ("own_posts", (ACCOUNT,), {"limit": 5, "start": 10}),
            ("conversations", (ACCOUNT,), {"limit": 10, "start": 0}),
            ("messages", (ACCOUNT, "2-ABC=="), {"limit": 3, "start": 0}),
        ])
        self.assertEqual(self.reader.http.close.call_count, 5)

    def test_auth_failure_returns_the_shared_envelope_with_a_login_hint(self):
        self.reader = FakeReader(AuthUnavailable("LinkedIn session expired, challenged or throttled"))
        for name, arguments in (("me", {}), ("posts", {}), ("messages", {"conversation_id": "2-x"})):
            with self.subTest(tool=name):
                failed, result = self.rpc(name, arguments)
                self.assertTrue(failed)
                self.assertIs(result["ok"], False)
                self.assertEqual(set(result["error"]), {"code", "message", "hint"})
                self.assertEqual(result["error"]["code"], "auth_unavailable")
                self.assertIn(f"login start --platform linkedin --account {ACCOUNT}", result["error"]["hint"])
                self.assertNotIn("TEST_COOKIE", json.dumps(result))

    def test_read_failures_keep_provider_text_private(self):
        self.reader = FakeReader(LinkedInReadFailure("LinkedIn read failed"))
        failed, result = self.rpc("posts")
        self.assertEqual(result["error"]["code"], "read_unavailable")
        self.assertIn("linkedin_status", result["error"]["hint"])
        self.reader = FakeReader(LinkedInReadFailure("LinkedIn session belongs to a different account"))
        self.assertEqual(self.rpc("me")[1]["error"]["code"], "account_mismatch")
        self.reader = FakeReader(RuntimeError('{"secret": "PRIVATE_SENTINEL"}'))
        failed, result = self.rpc("profile", {"handle": "someone"})
        self.assertEqual(result["error"]["code"], "internal_error")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))

    def test_real_adapter_reads_through_the_mocked_session(self):
        self.use_real_adapter()
        self.assertEqual(self.call("me")["identity"]["id"], "ABC123")
        self.http.get.side_effect = lambda *args, **kwargs: response(status=401)
        failed, result = self.rpc("me")
        self.assertEqual(result["error"]["code"], "auth_unavailable")
        self.http.post.assert_not_called()


class DraftAndJobTests(LinkedInMCPTestCase):
    def setUp(self):
        super().setUp()
        self.image = self.root / "photo.png"
        self.image.write_bytes(png_bytes())

    def test_draft_post_creates_an_account_job_with_the_media_summary(self):
        result = self.draft("With a picture", media_path=str(self.image), alt_text="A red square",
                            source_url="https://example.com/source", idempotency_key="once")
        job = result["job"]
        self.assertEqual(result["job_id"], job["id"])
        self.assertEqual((job["platform"], job["account"], job["status"]), ("linkedin", ACCOUNT, "draft"))
        self.assertEqual((job["body"], job["media_alt"]), ("With a picture", "A red square"))
        self.assertEqual(job["source_urls"], ["https://example.com/source"])
        self.assertEqual(result["media"], {"path": str(self.image.resolve()), "mime": "image/png",
                                           "size_bytes": self.image.stat().st_size, "alt_text": "A red square",
                                           "valid": True})
        self.assertIn("linkedin_queue_job", result["next_step"])
        self.assertEqual(self.stored(job["id"])["account_key"], f"linkedin:{ACCOUNT}")
        text_only = self.draft("Just words")
        self.assertIsNone(text_only["media"])

    def test_idempotency_key_returns_the_same_draft_and_refuses_new_content(self):
        first = self.draft("Same", idempotency_key="k1")["job_id"]
        self.assertEqual(self.draft("Same", idempotency_key="k1")["job_id"], first)
        failed, result = self.rpc("draft_post", {"text": "Different", "idempotency_key": "k1"})
        self.assertTrue(failed)
        self.assertEqual(result["error"]["code"], "idempotency_conflict")
        self.assertIn("linkedin_list_jobs", result["error"]["hint"])
        self.assertEqual(len(self.call("list_jobs")["jobs"]), 1)

    def test_outbox_media_rules_apply_with_tool_hints(self):
        carousel = self.root / "carousel.json"
        carousel.write_text("{}")
        cases = [
            ({"media_path": str(carousel)}, "linkedin_single_image_only", "media_path"),
            ({"alt_text": "No image"}, "alt_requires_media", "media_path"),
            ({"media_path": "relative/photo.png"}, "invalid_media", "absolute"),
            ({"media_path": str(self.root / "missing.png")}, "invalid_media", "media_path"),
            ({"media_path": str(self.image), "alt_text": "x" * 4087}, "invalid_arguments", "linkedin_draft_post"),
        ]
        for extra, code, hint in cases:
            with self.subTest(code=code, extra=list(extra)):
                failed, result = self.rpc("draft_post", {"text": "Post", **extra})
                self.assertTrue(failed)
                self.assertEqual(result["error"]["code"], code)
                self.assertIn(hint, result["error"]["hint"])
                self.assertNotIn("--media", result["error"]["hint"])
        self.assertEqual(self.call("list_jobs", status="all")["jobs"], [])

    def test_list_show_queue_and_cancel_are_scoped_to_the_account(self):
        draft = self.draft("Queue me")["job_id"]
        later = utc_text(utc_now() + timedelta(days=1))
        queued = self.call("queue_job", job_id=draft, scheduled_at=later)
        self.assertEqual((queued["job"]["status"], queued["job"]["scheduled_at"]), ("queued", later))
        self.assertTrue(queued["job"]["body_sha256"])
        shown = self.call("show_job", job_id=draft)["job"]
        self.assertEqual(shown["body"], "Queue me")
        self.assertNotIn("lease_token", shown)
        self.assertEqual([job["id"] for job in self.call("list_jobs")["jobs"]], [draft])
        self.assertEqual(self.call("list_jobs", status="draft")["jobs"], [])
        cancelled = self.call("cancel_job", job_id=draft, reason="changed my mind")["job"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.call("list_jobs")["jobs"], [])
        self.assertEqual(len(self.call("list_jobs", status="cancelled")["jobs"]), 1)
        with Outbox(self.db) as box:
            alien = box.create_draft(platform="linkedin", account="someone-else", body="Not mine")
            threads = box.create_draft(platform="threads", account=ACCOUNT.replace("-", "_"), body="Threads")
        for job in (alien, threads):
            for name in ("show_job", "queue_job", "cancel_job", "dry_run", "publish_job"):
                with self.subTest(tool=name, platform=job["platform"]):
                    failed, result = self.rpc(name, {"job_id": job["id"]})
                    self.assertEqual(result["error"]["code"], "job_not_found")
                    self.assertIn("linkedin_list_jobs", result["error"]["hint"])

    def test_queue_refuses_a_time_without_timezone_and_state_errors_name_tools(self):
        draft = self.draft()["job_id"]
        failed, result = self.rpc("queue_job", {"job_id": draft, "scheduled_at": "2026-11-02T09:00:00"})
        self.assertEqual(result["error"]["code"], "invalid_time")
        self.call("queue_job", job_id=draft)
        self.call("cancel_job", job_id=draft)
        failed, result = self.rpc("queue_job", {"job_id": draft})
        self.assertEqual(result["error"]["code"], "not_draft")
        self.assertIn("linkedin_show_job", result["error"]["hint"])
        self.assertNotIn("ghost-in-the-box outbox", result["error"]["hint"])

    def test_dry_run_makes_no_network_request(self):
        self.adapter_factory = Mock(side_effect=AssertionError("dry run must not build an adapter"))
        job_id = self.queued("Preview", media_path=str(self.image), alt_text="A red square")
        with patch.object(requests.Session, "request", side_effect=AssertionError("network")):
            result = self.call("dry_run", job_id=job_id)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["network_requests"], 0)
        self.assertTrue(result["would_publish"], result["blockers"])
        self.assertEqual(result["payload"]["body"], "Preview")
        self.assertTrue(result["payload"]["matches_frozen_hash"])
        self.assertEqual(result["media"]["mime"], "image/png")
        self.assertEqual(result["target_account"]["account"], ACCOUNT)
        self.assertEqual(result["blocker_hints"], {})
        self.assertEqual(self.stored(job_id)["status"], "queued")
        self.http.get.assert_not_called()
        self.http.post.assert_not_called()

    def test_dry_run_blockers_come_with_tool_hints(self):
        draft = self.draft()["job_id"]
        result = self.call("dry_run", job_id=draft)
        self.assertEqual(result["blockers"], ["job_not_queued"])
        self.assertIn("linkedin_queue_job", result["blocker_hints"]["job_not_queued"])
        self.call("queue_job", job_id=draft, scheduled_at=utc_text(utc_now() + timedelta(hours=2)))
        self.assertEqual(self.call("dry_run", job_id=draft)["blockers"], ["not_due"])
        self.assertIn("now=true", self.call("dry_run", job_id=draft)["blocker_hints"]["not_due"])
        self.assertTrue(self.call("dry_run", job_id=draft, now=True)["would_publish"])


class PublishTests(LinkedInMCPTestCase):
    def setUp(self):
        super().setUp()
        self.use_real_adapter()

    def test_publish_uses_the_shared_publisher_with_before_send_right_before_the_share(self):
        job_id = self.queued("Hello LinkedIn")
        failed, result = self.rpc("publish_job", {"job_id": job_id})
        self.assertFalse(failed, result)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["provider_id"], "urn:li:share:123")
        self.assertEqual(result["permalink"], "https://www.linkedin.com/feed/update/urn:li:share:123/")
        self.assertEqual(self.order, ["identity", "before_send", "share"])
        payload = self.http.post.call_args.kwargs["json"]
        self.assertEqual(payload["variables"]["post"]["commentary"]["text"], "Hello LinkedIn")
        stored = self.stored(job_id)
        self.assertEqual((stored["status"], stored["provider_id"], stored["attempts"]),
                         ("published", "urn:li:share:123", 1))
        again = self.call("publish_job", job_id=job_id)
        self.assertTrue(again["already_published"])
        self.assertEqual(self.http.post.call_count, 1)

    def test_publish_passes_the_image_through_the_same_path(self):
        image = self.root / "photo.png"
        image.write_bytes(png_bytes())
        job_id = self.queued("With image", media_path=str(image), alt_text="A red square")
        answers = iter([response({"data": {"value": {
            "urn": "urn:li:digitalmediaAsset:ABC", "singleUploadUrl": "https://www.linkedin.com/dms-uploads/x",
            "singleUploadHeaders": {}, "type": "SINGLE"}}}), response(SHARED)])
        self.http.post.side_effect = lambda *args, **kwargs: (self.order.append("post"), next(answers))[1]
        self.http.put.side_effect = lambda *args, **kwargs: (self.order.append("put"), response(status=201))[1]
        result = self.call("publish_job", job_id=job_id)
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.order, ["identity", "post", "put", "before_send", "post"])
        self.assertEqual(self.http.put.call_args.kwargs["data"], image.read_bytes())
        media = self.http.post.call_args.kwargs["json"]["variables"]["post"]["media"]
        self.assertEqual(media["altText"], "A red square")
        self.assertEqual(self.stored(job_id)["media_sha256"], hashlib.sha256(image.read_bytes()).hexdigest())

    def test_not_due_is_respected_unless_now(self):
        draft = self.draft("Later")["job_id"]
        later = utc_text(utc_now() + timedelta(days=2))
        self.call("queue_job", job_id=draft, scheduled_at=later)
        failed, result = self.rpc("publish_job", {"job_id": draft})
        self.assertTrue(failed)
        self.assertEqual((result["status"], result["error"]["code"]), ("not_due", "not_due"))
        self.assertIn("now=true", result["error"]["hint"])
        self.assertIn(later, result["error"]["hint"])
        self.assertEqual(self.order, [])
        self.assertEqual(self.stored(draft)["status"], "queued")
        self.assertEqual(self.call("publish_job", job_id=draft, now=True)["status"], "published")

    def test_auth_failure_keeps_the_job_queued_and_never_posts(self):
        job_id = self.queued()
        self.http.get.side_effect = lambda *args, **kwargs: response(status=401)
        failed, result = self.rpc("publish_job", {"job_id": job_id})
        self.assertTrue(failed)
        self.assertEqual(result["status"], "auth_unavailable")
        self.assertEqual(result["error"]["code"], "auth_unavailable")
        self.assertIn(f"login start --platform linkedin --account {ACCOUNT}", result["error"]["hint"])
        self.assertIn("stays queued", result["error"]["hint"])
        self.assertEqual(self.stored(job_id)["status"], "queued")
        self.http.post.assert_not_called()

    def test_uncertain_send_is_never_resent_and_resolves_with_evidence(self):
        job_id = self.queued()
        self.share_reply = requests.Timeout("PRIVATE_RESPONSE")
        failed, result = self.rpc("publish_job", {"job_id": job_id})
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("linkedin_resolve_uncertain", result["error"]["hint"])
        self.assertNotIn("PRIVATE_RESPONSE", json.dumps(result))
        failed, result = self.rpc("publish_job", {"job_id": job_id})
        self.assertEqual((result["status"], result["job_status"]), ("not_queued", "uncertain"))
        self.assertIn("linkedin_resolve_uncertain", result["error"]["hint"])
        self.assertEqual(self.http.post.call_count, 1)
        blocked = self.queued("Another post")
        failed, result = self.rpc("publish_job", {"job_id": blocked})
        self.assertEqual(result["status"], "not_claimable")
        self.assertIn("linkedin_resolve_uncertain", result["error"]["hint"])
        self.assertIn("account_busy", self.call("dry_run", job_id=blocked)["blockers"])

        failed, result = self.rpc("resolve_uncertain", {"job_id": job_id, "outcome": "published",
                                                        "reason": "saw it on the feed"})
        self.assertEqual(result["error"]["code"], "provider_id_required")
        failed, result = self.rpc("resolve_uncertain", {"job_id": job_id, "outcome": "failed", "reason": "x",
                                                        "provider_id": "urn:li:share:9"})
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        failed, result = self.rpc("resolve_uncertain", {"job_id": job_id, "outcome": "published", "reason": "x",
                                                        "provider_id": "https://linkedin.com/feed/9"})
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        resolved = self.call("resolve_uncertain", job_id=job_id, outcome="published",
                             reason="the post is on the feed", provider_id="urn:li:share:999")["job"]
        self.assertEqual((resolved["status"], resolved["provider_id"], resolved["permalink"]),
                         ("published", "urn:li:share:999", "https://www.linkedin.com/feed/update/urn:li:share:999/"))
        failed, result = self.rpc("resolve_uncertain", {"job_id": job_id, "outcome": "failed", "reason": "x"})
        self.assertEqual(result["error"]["code"], "not_uncertain")
        self.assertIn("linkedin_show_job", result["error"]["hint"])

    def test_expired_lease_can_be_resolved_but_a_live_one_cannot(self):
        job_id = self.queued()
        with Outbox(self.db) as box:
            box.claim_job(job_id, lease_seconds=60)
        failed, result = self.rpc("resolve_uncertain", {"job_id": job_id, "outcome": "queued", "reason": "x"})
        self.assertEqual(result["error"]["code"], "not_uncertain")
        with Outbox(self.db) as box:
            box.db.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id=?", (job_id,))
        resolved = self.call("resolve_uncertain", job_id=job_id, outcome="queued",
                             reason="the publisher crashed before connecting")["job"]
        self.assertEqual(resolved["status"], "queued")

    def test_retry_failed_mirrors_the_cli_guards(self):
        job_id = self.queued()
        self.share_reply = response(status=400, text='{"message":"Invalid share"}')
        failed, result = self.rpc("publish_job", {"job_id": job_id})
        self.assertEqual(result["status"], "rejected")
        self.assertIn("linkedin_retry_failed", result["error"]["hint"])
        self.assertNotIn("ghost-in-the-box outbox", result["error"]["hint"])
        self.assertEqual(self.stored(job_id)["failure_kind"], "rejected")
        retried = self.call("retry_failed", job_id=job_id, reason="LinkedIn had an outage")["job"]
        self.assertEqual(retried["status"], "queued")
        failed, result = self.rpc("retry_failed", {"job_id": job_id, "reason": "again"})
        self.assertEqual(result["error"]["code"], "not_failed")
        self.assertIn("linkedin_show_job", result["error"]["hint"])

        self.share_reply = requests.Timeout()
        self.call("publish_job", job_id=job_id)
        self.call("resolve_uncertain", job_id=job_id, outcome="failed", reason="not on the feed after an hour")
        failed, result = self.rpc("retry_failed", {"job_id": job_id, "reason": "try again"})
        self.assertEqual(result["error"]["code"], "possibly_sent")
        self.assertIn("linkedin_draft_post", result["error"]["hint"])
        self.assertEqual(self.stored(job_id)["status"], "failed")

    def test_cancel_refuses_published_jobs_with_a_tool_hint(self):
        job_id = self.queued()
        self.call("publish_job", job_id=job_id)
        failed, result = self.rpc("cancel_job", {"job_id": job_id})
        self.assertEqual(result["error"]["code"], "already_published")
        self.assertIn("linkedin_show_job", result["error"]["hint"])

    def test_publisher_hints_name_tools_not_cli_commands(self):
        for status in ("not_found", "not_queued", "not_claimable", "rejected", "failed_before_send", "uncertain",
                       "lease_lost", "outbox_refused", "outbox_unavailable", "auth_unavailable", "not_due",
                       "receipt_not_recorded"):
            with self.subTest(status=status):
                failure = linkedin_mcp._structured_failure(
                    {"ok": False, "status": status, "scheduled_at": "T", "provider_id": "urn:li:share:1",
                     "error": {"code": status, "message": "m", "hint": "ghost-in-the-box outbox show X"}},
                    ACCOUNT)
                self.assertEqual(set(failure["error"]), {"code", "message", "hint"})
                self.assertNotIn("ghost-in-the-box outbox", failure["error"]["hint"])
                self.assertNotIn("ghost-in-the-box publish", failure["error"]["hint"])
        for hints in (linkedin_mcp.OUTBOX_HINTS, linkedin_mcp.NOT_QUEUED_HINTS, linkedin_mcp.BLOCKER_HINTS):
            for code, hint in hints.items():
                self.assertNotRegex(hint, r"--[a-z]", code)


class AccountSelectionTests(LinkedInMCPTestCase):
    def resolve(self, explicit=None, environ=None):
        return resolve_account(explicit, environ=environ or {}, registry_factory=lambda: self.registry)

    def test_flag_beats_environment_beats_the_single_registered_account(self):
        self.assertEqual(self.resolve(), ACCOUNT)
        self.assertEqual(self.resolve(environ={"SOCIAL_FARM_LINKEDIN_ACCOUNT": "from-env"}), "from-env")
        self.assertEqual(self.resolve("from-flag", {"SOCIAL_FARM_LINKEDIN_ACCOUNT": "from-env"}), "from-flag")
        self.assertEqual(self.resolve(environ={"SOCIAL_FARM_IG_ACCOUNT": "instagram_only"}), ACCOUNT)
        with self.assertRaisesRegex(AccountSelectionError, "LinkedIn handle"):
            self.resolve("@jane doe")

    def test_only_linkedin_registrations_count(self):
        empty = AccountRegistry(self.root / "empty.json")
        with self.assertRaises(AccountSelectionError) as raised:
            resolve_account(environ={}, registry_factory=lambda: empty)
        self.assertIn("login start --platform linkedin", raised.exception.hint)
        session_file = self.root / "ig.json"
        session_file.write_text("{}")
        session_file.chmod(0o600)
        empty.register(platform="instagram", account="ig_only", kind="instagrapi_settings", session_file=session_file)
        with self.assertRaisesRegex(AccountSelectionError, "no LinkedIn account"):
            resolve_account(environ={}, registry_factory=lambda: empty)
        save_login("linkedin", "second-member", session("linkedin", "second-member"),
                   registry=self.registry, state_root=self.root)
        with self.assertRaisesRegex(AccountSelectionError, f"{ACCOUNT}, second-member") as raised:
            self.resolve()
        self.assertIn("SOCIAL_FARM_LINKEDIN_ACCOUNT", raised.exception.hint)

    def test_placeholders_are_refused_unless_really_registered(self):
        for handle in ("YOUR_HANDLE", "your-handle", "YOUR_PUBLIC_HANDLE", "example_creator"):
            with self.subTest(handle=handle):
                with self.assertRaisesRegex(AccountSelectionError, "placeholder"):
                    self.resolve(handle)
                with self.assertRaisesRegex(AccountSelectionError, "placeholder"):
                    self.resolve(environ={"SOCIAL_FARM_LINKEDIN_ACCOUNT": handle})
        save_login("linkedin", "your-handle", session("linkedin", "your-handle"),
                   registry=self.registry, state_root=self.root)
        self.assertEqual(self.resolve("your-handle"), "your-handle")

    def test_service_refuses_an_unregistered_placeholder(self):
        placeholder = LinkedInTools(account="YOUR_HANDLE", outbox_path=self.db, registry=self.registry,
                                    adapter_factory=lambda: self.reader)
        failed, result = self.rpc("status", service=placeholder)
        self.assertTrue(failed)
        self.assertEqual(result["error"]["code"], "placeholder_account")
        self.assertEqual(self.reader.calls, [])

    def test_server_refuses_to_start_without_a_resolvable_account(self):
        environment = {key: value for key, value in os.environ.items() if key != "SOCIAL_FARM_LINKEDIN_ACCOUNT"}
        environment.update(SOCIAL_FARM_ACCOUNTS_FILE=str(self.root / "empty.json"), HOME=str(self.root))
        for arguments, expected in (([], "no LinkedIn account"), (["--account", "YOUR_HANDLE"], "placeholder"),
                                    (["--account", "bad handle"], "LinkedIn handle")):
            with self.subTest(arguments=arguments):
                process = subprocess.run([sys.executable, "scripts/linkedin_mcp.py", *arguments],
                    input=b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n', capture_output=True, timeout=30,
                    env=environment, cwd=ROOT)
                self.assertEqual(process.returncode, 2)
                self.assertEqual(process.stdout, b"")
                self.assertIn(expected, process.stderr.decode())
                self.assertIn("hint:", process.stderr.decode())


class CatalogTests(unittest.TestCase):
    def run_command(self, *arguments):
        process = subprocess.run([sys.executable, *arguments], capture_output=True, timeout=30, cwd=ROOT)
        self.assertEqual(process.returncode, 0, process.stderr)
        return process.stdout.decode()

    def test_linkedin_list_tools_formats(self):
        data = json.loads(self.run_command("scripts/linkedin_mcp.py", "--list-tools"))
        self.assertEqual(data, catalog())
        self.assertEqual([server["server"] for server in data["servers"]], ["ghost-in-the-box-linkedin"])
        self.assertEqual(json.loads(self.run_command("scripts/linkedin_mcp.py", "--list-tools", "--format", "mcp")),
                         {"tools": TOOLS})
        markdown = self.run_command("scripts/linkedin_mcp.py", "--list-tools", "--format", "md")
        self.assertIn("ghost-in-the-box linkedin-mcp --list-tools --format md", markdown)
        self.assertIn("| `linkedin_publish_job` | Publish a LinkedIn job |", markdown)

    def test_mcp_tools_lists_every_server_in_one_document(self):
        data = mcp_tools.catalog()
        self.assertEqual([server["server"] for server in data["servers"]],
                         ["ghost-in-the-box-instagram", "ghost-in-the-box-linkedin", "ghost-in-the-box-substack"])
        self.assertEqual(data["generated_by"], "ghost-in-the-box mcp-tools")
        markdown = self.run_command("scripts/mcp_tools.py", "--format", "md")
        self.assertIn("Generated by `ghost-in-the-box mcp-tools --format md`", markdown)
        rows = [line for line in markdown.splitlines() if line.startswith("| `")]
        self.assertEqual(len(rows), sum(len(server["tools"]) for server in data["servers"]))
        definitions = json.loads(self.run_command("scripts/mcp_tools.py", "--format", "mcp"))
        self.assertEqual([len(server["tools"]) for server in definitions["servers"]], [24, len(TOOLS)])
        self.assertEqual(json.loads(self.run_command("scripts/mcp_tools.py"))["servers"], data["servers"])

    def test_instagram_list_tools_is_unchanged(self):
        data = json.loads(self.run_command("scripts/instagram_mcp.py", "--list-tools"))
        self.assertEqual(data["generated_by"], "ghost-in-the-box instagram-mcp --list-tools")
        self.assertNotIn("ghost-in-the-box-linkedin", [server["server"] for server in data["servers"]])


if __name__ == "__main__":
    unittest.main()
