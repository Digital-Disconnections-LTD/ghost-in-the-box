from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from farm.accounts import AccountRegistry
from farm.adapters import DependencyUnavailable, PreSendFailure, PublishRejected
from farm.adapters.instagram import AuthUnavailable as InstagramAuthUnavailable
from farm.adapters.threads import AuthUnavailable as ThreadsAuthUnavailable, PublishUncertain
from farm.outbox import Outbox
from scripts.social_publish_job import main, run, run_due
from _env import IsolatedStateTestCase


class FakeAdapter:
    def __init__(self, error=None):
        self.error = error
        self.sent = []
        self.saved = False

    def publish(self, job):
        self.sent.append(job["id"])
        if self.error:
            raise self.error
        return {"provider_id": "provider-123", "permalink": "https://example.test/post"}

    def save_session(self):
        self.saved = True


class BoundaryAdapter(FakeAdapter):
    """Reports its send boundary like the real adapters do."""

    def __init__(self, error=None, *, fail_after_boundary=False, during=None):
        super().__init__(error)
        self.fail_after_boundary = fail_after_boundary
        self.during = during

    def publish(self, job, *, before_send=None):
        self.sent.append(job["id"])
        if self.error and not self.fail_after_boundary:
            raise self.error
        if before_send is not None:
            before_send()
        if self.during:
            self.during(job)
        if self.error:
            raise self.error
        return {"provider_id": "provider-123", "permalink": "https://example.test/post"}


def must_not_build():
    raise AssertionError("adapter must not be constructed")


class DirectPublishTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.temp = TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "outbox.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def draft(self, platform, *, at=None):
        with Outbox(self.db_path) as box:
            row = box.create_draft(platform=platform, account="mine", body="Exact operator text")
            return box.queue(row["id"], at=at)

    def invoke(self, job_id, *, ig=None, threads=None, browser=None, linkedin=None, **options):
        output = io.StringIO()
        with redirect_stdout(output):
            code = run(job_id, box_factory=lambda: Outbox(self.db_path),
                       instagram_factory=lambda: ig or FakeAdapter(),
                       threads_factory=lambda: threads or FakeAdapter(),
                       browser_threads_factory=lambda: browser or FakeAdapter(
                           ThreadsAuthUnavailable("browser login required")),
                       linkedin_factory=lambda: linkedin or FakeAdapter(),
                       envelope=options.pop("envelope", True), **options)
        return code, json.loads(output.getvalue())

    def invoke_due(self, **options):
        output = io.StringIO()
        factories = {"instagram_factory": FakeAdapter, "threads_factory": FakeAdapter,
                     "linkedin_factory": FakeAdapter,
                     "browser_threads_factory": lambda: FakeAdapter(ThreadsAuthUnavailable("browser_login_required"))}
        factories.update(options.pop("factories", {}))
        with redirect_stdout(output):
            code = run_due(box_factory=lambda: Outbox(self.db_path), **factories, **options)
        return code, json.loads(output.getvalue())

    def status(self, job_id):
        with Outbox(self.db_path) as box:
            return box.get(job_id)["status"]

    def assertEnvelope(self, result):
        self.assertIs(result["ok"], False)
        self.assertEqual(set(result["error"]), {"code", "message", "hint"})
        self.assertTrue(result["error"]["code"])
        self.assertIn("status", result)

    def test_claims_only_named_job_even_when_another_is_due(self):
        first = self.draft("threads")
        second = self.draft("threads", at="2030-01-01T00:00:00Z")
        adapter = FakeAdapter()
        code, result = self.invoke(second["id"], threads=adapter)
        self.assertEqual((code, result["status"]), (0, "published"))
        self.assertEqual(adapter.sent, [second["id"]])
        self.assertEqual(result["provider_id"], "provider-123")
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(first["id"])["status"], "queued")
            self.assertEqual(box.get(second["id"])["status"], "published")

    def test_threads_auth_failure_preserves_exact_queued_job(self):
        job = self.draft("threads")
        adapter = FakeAdapter(ThreadsAuthUnavailable("HTTP 403"))
        code, result = self.invoke(job["id"], threads=adapter)
        self.assertEqual((code, result["status"]), (4, "auth_unavailable"))
        self.assertNotIn("HTTP 403", json.dumps(result))
        self.assertEnvelope(result)
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["status"], "queued")

    def test_threads_mobile_auth_falls_back_to_browser_once(self):
        job = self.draft("threads")
        mobile = FakeAdapter(ThreadsAuthUnavailable("HTTP 403"))
        browser = FakeAdapter()
        code, result = self.invoke(job["id"], threads=mobile, browser=browser)
        self.assertEqual((code, result["status"]), (0, "published"))
        self.assertEqual(mobile.sent, [job["id"]])
        self.assertEqual(browser.sent, [job["id"]])
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["status"], "published")

    def test_browser_uncertainty_blocks_second_send(self):
        job = self.draft("threads")
        mobile = FakeAdapter(ThreadsAuthUnavailable("HTTP 403"))
        browser = FakeAdapter(PublishUncertain("unknown"))
        code, result = self.invoke(job["id"], threads=mobile, browser=browser)
        self.assertEqual((code, result["status"]), (3, "uncertain"))
        self.assertEqual(self.invoke(job["id"])[1]["status"], "not_queued")

    def test_instagram_auth_failure_preserves_job_and_success_saves_session(self):
        job = self.draft("instagram")
        code, result = self.invoke(job["id"],
                                   ig=FakeAdapter(InstagramAuthUnavailable("session expired")))
        self.assertEqual((code, result["status"]), (4, "auth_unavailable"))
        self.assertIn("login start --platform instagram --account mine", result["error"]["hint"])
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["status"], "queued")
        adapter = FakeAdapter()
        code, result = self.invoke(job["id"], ig=adapter)
        self.assertEqual((code, result["status"]), (0, "published"))
        self.assertTrue(result["session_saved"])
        self.assertTrue(adapter.saved)

    def test_missing_instagram_dependency_keeps_the_job_queued(self):
        job = self.draft("instagram")
        code, result = self.invoke(job["id"], ig=FakeAdapter(
            DependencyUnavailable("Instagram client dependency unavailable",
                                  install_hint="python -m pip install -e '.[instagram]'")))
        self.assertEqual((code, result["status"]), (4, "dependency_unavailable"))
        self.assertIn(".[instagram]", result["error"]["hint"])
        self.assertEqual(self.status(job["id"]), "queued")
        code, result = self.invoke(job["id"], ig=FakeAdapter(
            InstagramAuthUnavailable("Instagram client dependency unavailable")))
        self.assertEqual((code, result["status"]), (4, "dependency_unavailable"))
        self.assertEqual(self.status(job["id"]), "queued")

    def test_threads_missing_browser_dependency_is_not_a_cooldown(self):
        job = self.draft("threads")
        mobile = FakeAdapter(ThreadsAuthUnavailable("portable browser session selected"))
        for browser in (FakeAdapter(DependencyUnavailable("playwright missing",
                                                          install_hint="pip install playwright")),
                        FakeAdapter(ThreadsAuthUnavailable("install the login extra and Playwright Chromium"))):
            with self.subTest(browser=str(browser.error)):
                code, result = self.invoke(job["id"], threads=mobile, browser=browser)
                self.assertEqual((code, result["status"]), (4, "dependency_unavailable"))
                self.assertEqual(result["error"]["code"], "dependency_unavailable")
                self.assertNotEqual(result.get("account_action"), "retry_after_cooldown")
                self.assertIn("install", result["error"]["hint"])
                self.assertEqual(self.status(job["id"]), "queued")

    def test_threads_auth_actions_name_the_real_next_step(self):
        job = self.draft("threads")
        mobile = FakeAdapter(ThreadsAuthUnavailable("portable browser session selected"))
        cases = {"browser_login_required": "browser_login_required",
                 "portable browser session unavailable": "check_login",
                 "retry_after_cooldown": "retry_after_cooldown",
                 "Threads bearer preflight returned HTTP 429": "retry_after_cooldown",
                 "browser fallback needs one-time setup": "browser_setup_required"}
        for message, action in cases.items():
            with self.subTest(message=message):
                code, result = self.invoke(job["id"], threads=mobile,
                                           browser=FakeAdapter(ThreadsAuthUnavailable(message)))
                self.assertEqual((code, result["account_action"]), (4, action))
                self.assertEqual(result["error"]["code"], action)
                self.assertNotIn(message, result["error"]["message"])

    def test_receipt_survives_lease_expiry_during_a_slow_send(self):
        job = self.draft("instagram")

        def expire_and_let_another_worker_sweep(claimed):
            with Outbox(self.db_path) as box:
                box.db.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id=?",
                               (claimed["id"],))
                self.assertIsNone(box.claim_due())
                self.assertEqual(box.get(claimed["id"])["status"], "uncertain")

        adapter = BoundaryAdapter(during=expire_and_let_another_worker_sweep)
        code, result = self.invoke(job["id"], ig=adapter)
        self.assertEqual((code, result["status"]), (0, "published"))
        with Outbox(self.db_path) as box:
            saved = box.get(job["id"])
        self.assertEqual((saved["status"], saved["provider_id"]), ("published", "provider-123"))

    def test_receipt_is_printed_when_the_outbox_cannot_record_it(self):
        job = self.draft("instagram")

        def operator_resolves_meanwhile(claimed):
            with Outbox(self.db_path) as box:
                box.db.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id=?",
                               (claimed["id"],))
                box.expire_leases()
                box.resolve_uncertain(claimed["id"], outcome="failed", reason="looked too early")

        code, result = self.invoke(job["id"], ig=BoundaryAdapter(during=operator_resolves_meanwhile))
        self.assertEqual((code, result["status"]), (3, "receipt_not_recorded"))
        self.assertEqual(result["provider_id"], "provider-123")
        self.assertIn("--provider-id provider-123", result["error"]["hint"])
        self.assertEnvelope(result)

    def test_unexpected_error_before_the_send_boundary_is_a_plain_failure(self):
        job = self.draft("linkedin")
        code, result = self.invoke(job["id"], linkedin=BoundaryAdapter(RuntimeError("payload bug")))
        self.assertEqual((code, result["status"], result["error"]["code"]),
                         (1, "failed_before_send", "adapter_error"))
        with Outbox(self.db_path) as box:
            saved = box.get(job["id"])
            self.assertEqual((saved["status"], saved["failure_kind"]), ("failed", "pre_send"))
            self.assertEqual(box.retry_failed(job["id"], reason="fixed")["status"], "queued")

    def test_unexpected_error_after_the_send_boundary_is_uncertain(self):
        job = self.draft("linkedin")
        code, result = self.invoke(job["id"], linkedin=BoundaryAdapter(
            RuntimeError("socket closed"), fail_after_boundary=True))
        self.assertEqual((code, result["status"], result["error"]["code"]),
                         (3, "uncertain", "adapter_error_after_send"))
        self.assertEqual(self.status(job["id"]), "uncertain")

    def test_adapter_without_a_boundary_hook_treats_surprises_as_uncertain(self):
        job = self.draft("linkedin")
        code, result = self.invoke(job["id"], linkedin=FakeAdapter(RuntimeError("unknown")))
        self.assertEqual((code, result["status"]), (3, "uncertain"))

    def test_definite_rejection_fails_the_job_and_allows_explicit_retry(self):
        job = self.draft("threads")
        code, result = self.invoke(job["id"], threads=FakeAdapter(
            PublishRejected("Threads configure rejected with HTTP 400", status_code=400)))
        self.assertEqual((code, result["status"], result["error"]["code"]),
                         (1, "rejected", "provider_rejected"))
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["failure_kind"], "rejected")
            self.assertEqual(box.retry_failed(job["id"], reason="edited")["status"], "queued")

    def test_pre_send_failure_keeps_the_adapter_reason(self):
        job = self.draft("threads")
        code, result = self.invoke(job["id"], threads=FakeAdapter(PreSendFailure("text too long")))
        self.assertEqual((code, result["status"]), (1, "failed_before_send"))
        self.assertEqual(result["error"]["message"], "text too long")
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["last_error"], "text too long")

    def test_adapter_text_is_sanitised_before_output_and_storage(self):
        leaks = ['provider said {"error": "bad", "sessionid": "s3cr3t"}',
                 "token IGT:2:QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODk= rejected",
                 "line one\nline two " + "x" * 300]
        for text in leaks:
            with self.subTest(text=text[:20]):
                job = self.draft("threads")
                code, result = self.invoke(job["id"], threads=FakeAdapter(
                    PublishRejected(text, status_code=400)))
                self.assertEqual(result["error"]["message"], "PublishRejected (HTTP 400)")
                with Outbox(self.db_path) as box:
                    stored = box.get(job["id"])["last_error"]
                self.assertEqual(stored, "PublishRejected (HTTP 400)")
                for secret in ("s3cr3t", "QUJDREVG", "line two"):
                    self.assertNotIn(secret, json.dumps(result) + stored)

    def test_fixed_adapter_reasons_are_kept_verbatim(self):
        reason = "Scrapling stopped before submit (identity:account_mismatch)"
        job = self.draft("threads")
        code, result = self.invoke(job["id"], threads=FakeAdapter(PreSendFailure(reason)))
        self.assertEqual(result["error"]["message"], reason)
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["last_error"], reason)

    def test_future_job_needs_now_on_the_command_line(self):
        job = self.draft("threads", at="2099-01-01T00:00:00Z")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([job["id"], "--db", str(self.db_path)])
        result = json.loads(output.getvalue())
        self.assertEqual((code, result["status"], result["error"]["code"]), (2, "not_due", "not_due"))
        self.assertIn("2099-01-01T00:00:00Z", result["error"]["hint"])
        self.assertIn("--now", result["error"]["hint"])
        self.assertEqual(self.status(job["id"]), "queued")
        code, result = self.invoke(job["id"], now=False)
        self.assertEqual(result["status"], "not_due")
        code, result = self.invoke(job["id"], now=True)
        self.assertEqual((code, result["status"]), (0, "published"))

    def test_dry_run_shows_payload_and_account_without_side_effects(self):
        job = self.draft("instagram", at="2099-01-01T00:00:00Z")
        registry = AccountRegistry(Path(self.temp.name) / "accounts.json")
        session = Path(self.temp.name) / "session.json"
        session.write_text("{}")
        session.chmod(0o600)
        registry.register(platform="instagram", account="mine", kind="instagrapi_settings",
                          session_file=session, account_key="meta:mine")
        output = io.StringIO()
        with redirect_stdout(output):
            code = run(job["id"], box_factory=lambda: Outbox(self.db_path), dry=True, now=False,
                       envelope=True,
                       instagram_factory=must_not_build, threads_factory=must_not_build,
                       browser_threads_factory=must_not_build, linkedin_factory=must_not_build,
                       registry_factory=lambda: registry)
        result = json.loads(output.getvalue())
        self.assertEqual((code, result["status"]), (0, "dry_run"))
        self.assertEqual(result["payload"]["body"], "Exact operator text")
        self.assertTrue(result["payload"]["matches_frozen_hash"])
        self.assertEqual(result["target_account"]["account_key"], "meta:mine")
        self.assertTrue(result["target_account"]["account_key_matches"])
        self.assertEqual(result["blockers"], ["not_due"])
        self.assertFalse(result["would_publish"])
        self.assertNotIn("session_file", json.dumps(result))
        with Outbox(self.db_path) as box:
            after = box.get(job["id"])
        self.assertEqual((after["status"], after["attempts"]), ("queued", 0))

    def test_brief_only_legacy_job_is_refused_with_a_hint(self):
        job = self.draft("instagram")
        with Outbox(self.db_path) as box:
            box.db.execute("UPDATE jobs SET platform='tiktok' WHERE id=?", (job["id"],))
        code, result = self.invoke(job["id"])
        self.assertEqual((code, result["status"], result["error"]["code"]),
                         (2, "unsupported_platform", "brief_only_platform"))

    def test_every_refusal_uses_the_error_envelope(self):
        draft_only = None
        with Outbox(self.db_path) as box:
            draft_only = box.create_draft(platform="threads", account="mine", body="not queued")
        for job_id, expected in (("missing", "not_found"), (draft_only["id"], "not_queued")):
            code, result = self.invoke(job_id)
            self.assertEqual((code, result["status"]), (2, expected))
            self.assertEnvelope(result)

    def test_library_callers_get_the_legacy_shape_without_cli_hints(self):
        job = self.draft("threads")
        code, result = self.invoke(job["id"], threads=FakeAdapter(PublishUncertain("x")), envelope=False)
        self.assertEqual((code, result["ok"], result["status"]), (3, False, "uncertain"))
        self.assertNotIn("error", result)

    def test_command_line_rejects_mixed_modes(self):
        for argv in (["JOB", "--due"], [], ["JOB", "--limit", "3"], ["--due", "--dry-run"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(argv + ["--db", str(self.db_path)])
                self.assertEqual(caught.exception.code, 2)

    def test_due_publishes_only_due_jobs_for_platforms_with_adapters(self):
        due_ig = self.draft("instagram")
        due_li = self.draft("linkedin")
        future = self.draft("threads", at="2099-01-01T00:00:00Z")
        code, result = self.invoke_due(limit=10)
        self.assertEqual((code, result["status"], result["processed"]), (0, "processed", 2))
        self.assertEqual({item["job_id"] for item in result["results"]}, {due_ig["id"], due_li["id"]})
        self.assertEqual(self.status(future["id"]), "queued")
        self.assertEqual(self.invoke_due()[1]["status"], "idle")

    def test_due_honours_platform_and_limit(self):
        first = self.draft("threads")
        self.draft("instagram")
        code, result = self.invoke_due(platform="threads")
        self.assertEqual((code, result["status"], result["job_id"]), (0, "published", first["id"]))
        self.assertEqual(result["processed"], 1)

    def test_due_does_not_spin_on_an_account_whose_auth_failed(self):
        jobs = [self.draft("instagram") for _ in range(3)]
        broken = FakeAdapter(InstagramAuthUnavailable("expired"))
        code, result = self.invoke_due(limit=10, factories={"instagram_factory": lambda: broken})
        self.assertEqual((code, result["status"]), (4, "auth_unavailable"))
        self.assertEqual(len(broken.sent), 1)
        self.assertEqual({self.status(job["id"]) for job in jobs}, {"queued"})

    def test_database_errors_are_reported_as_json(self):
        import sqlite3

        def broken_box():
            raise sqlite3.OperationalError("database is locked")

        output = io.StringIO()
        with redirect_stdout(output):
            code = run("any", box_factory=broken_box, envelope=True)
        result = json.loads(output.getvalue())
        self.assertEqual((code, result["error"]["code"]), (1, "outbox_database_error"))
        self.assertEnvelope(result)

    def test_ambiguous_provider_result_is_never_reclaimed(self):
        job = self.draft("threads")
        code, result = self.invoke(job["id"], threads=FakeAdapter(PublishUncertain("unknown")))
        self.assertEqual((code, result["status"]), (3, "uncertain"))
        with Outbox(self.db_path) as box:
            self.assertEqual(box.get(job["id"])["status"], "uncertain")
        self.assertEqual(self.invoke(job["id"])[1]["status"], "not_queued")


if __name__ == "__main__":
    unittest.main()
