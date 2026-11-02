"""Production Scrapling transport selected after a verified browser login."""
from __future__ import annotations

from farm.accounts import account_key_matches
from farm.config import threads_scrapling_dir
from farm.session_files import is_private



import json
import sys
import os
from pathlib import Path
import random
import re
import subprocess
import time

from farm.adapters.threads import AuthUnavailable, PreSendFailure, PublishUncertain, ThreadsReadFailure
from farm.outbox import frozen_payload_matches
from farm.threads_transport import ThreadsDependencyUnavailable, scrapling_install

BASE = threads_scrapling_dir()
PYTHON = Path(os.environ.get("SOCIAL_FARM_SCRAPLING_PYTHON") or sys.executable)
BRIDGE = Path(__file__).resolve().parents[1] / "scripts/threads_scrapling_bridge.py"


class ProfileBusy(ThreadsReadFailure):
    """The local browser profile is in use by another bounded read."""


def verified_profile(account: str, state_dir: Path = BASE) -> bool:
    if account in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", account):
        return False
    path = Path(state_dir) / account / "verified-cookies.json"
    profile = path.parent / "profile"
    try:
        if (path.is_symlink() or not path.is_file() or not is_private(path) or
                profile.is_symlink() or not profile.is_dir()):
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("version") == 1 and data.get("account", "").casefold() == account.casefold()
    except (OSError, ValueError, TypeError, AttributeError):
        return False


class ScraplingThreadsAdapter:
    def __init__(self, *, registry, runner=subprocess.run, state_dir=BASE, python=PYTHON,
                 sleep=time.sleep, jitter=random.uniform, max_busy_retries=16):
        self.registry, self.runner = registry, runner
        self.state_dir, self.python = Path(state_dir), Path(python)
        self.sleep, self.jitter = sleep, jitter
        self.max_busy_retries = max_busy_retries

    def _call(self, account: str, payload: dict) -> dict:
        if not verified_profile(account, self.state_dir):
            raise AuthUnavailable("browser_login_required")
        if not self.python.is_file():
            raise ThreadsDependencyUnavailable(
                "python", "set SOCIAL_FARM_SCRAPLING_PYTHON to a Python with the browser extra")
        env = os.environ.copy()
        env["SOCIAL_FARM_THREADS_SCRAPLING_STATE"] = str(self.state_dir)
        try:
            response = self.runner([str(self.python), str(BRIDGE)], input=json.dumps(payload),
                                   text=True, capture_output=True, timeout=180, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublishUncertain("Scrapling browser outcome unknown") from exc
        try:
            data = json.loads(response.stdout.strip().splitlines()[-1])
            if not isinstance(data, dict):
                raise ValueError("invalid bridge result")
        except (ValueError, IndexError, AttributeError) as exc:
            raise PublishUncertain("Scrapling browser outcome unknown") from exc
        if data.get("status") == "dependency_unavailable":
            # The bridge reports this before opening a browser, so nothing was sent.
            raise ThreadsDependencyUnavailable(
                "scrapling", scrapling_install(self.python))
        return data

    def publish(self, job: dict, *, verify_only=False) -> dict:
        if job.get("platform") != "threads" or not frozen_payload_matches(job):
            raise PreSendFailure("Threads job changed before send")
        if job.get("media_path") or job.get("action") not in {"post", "reply"}:
            raise PreSendFailure("Scrapling publisher supports text posts and replies")
        self._registration(job)
        return self._publish(job, verify_only=verify_only)

    def _registration(self, job):
        account = job["account"]
        registration = self.registry.status("threads", account)
        linked = self.registry.status("instagram", account)
        if (not registration or not linked or
                not account_key_matches(registration, job.get("account_key")) or
                registration["account_key"] != linked["account_key"] or
                registration["kind"] != "instagrapi_settings" or linked["kind"] != "instagrapi_settings" or
                registration["session_file"] != linked["session_file"]):
            raise PreSendFailure("Threads job account registration mismatch")

    def _publish(self, job, *, verify_only=False):
        account = job["account"]
        data = self._call(account, {"mode": "verify" if verify_only else "publish", "job": job})
        status = data.get("status")
        if verify_only and status == "verified" and data.get("account") == account:
            return {"status": "verified", "account": account}
        if status == "published":
            provider_id, permalink = str(data.get("provider_id") or ""), str(data.get("permalink") or "")
            if (not provider_id.isdigit() or not re.fullmatch(
                    rf"https://www\.threads\.com/@{re.escape(account)}/post/[A-Za-z0-9_-]+", permalink)):
                raise PublishUncertain("Scrapling receipt incomplete")
            return {"provider_id": provider_id, "permalink": permalink}
        if status in {"auth_unavailable", "profile_busy", "quiet_hours"}:
            raise AuthUnavailable("browser_login_required" if status == "auth_unavailable" else
                                  "quiet_hours" if status == "quiet_hours" else "retry_after_cooldown")
        if status == "pre_send_failure":
            phase = str(data.get("phase") or "unknown")
            if phase not in {"identity", "journal_check", "read_before",
                             "prepare_composer", "journal_write"}:
                phase = "unknown"
            reason = str(data.get("reason") or "")
            if reason not in {"post_control_missing", "reply_control_missing",
                              "composer_missing", "multiple_composers", "composer_site",
                              "editor_not_empty", "editor_readback",
                              "post_control_ambiguous", "post_control_disabled",
                              "target_unavailable"}:
                reason = "unknown"
            raise PreSendFailure(f"Scrapling stopped before submit ({phase}:{reason})")
        if status == "possible_duplicate":
            raise PublishUncertain("matching Threads post or prior submit exists")
        raise PublishUncertain("Scrapling browser submit outcome unknown")

    def reconcile(self, job: dict) -> dict:
        """Read provider evidence for one existing attempt; never submit again."""
        if job.get("platform") != "threads" or not frozen_payload_matches(job):
            raise PreSendFailure("Threads job changed before reconciliation")
        data = self._call(job["account"], {"mode": "reconcile", "job": job})
        if data.get("status") == "profile_busy":
            raise ProfileBusy("local browser profile is busy")
        if data.get("status") == "published":
            provider_id, permalink = str(data.get("provider_id") or ""), str(data.get("permalink") or "")
            if provider_id.isdigit() and re.fullmatch(
                    rf"https://www\.threads\.com/@{re.escape(job['account'])}/post/[A-Za-z0-9_-]+", permalink):
                return {"status": "published", "provider_id": provider_id, "permalink": permalink}
        return {"status": "unconfirmed", "reason": str(data.get("reason") or "receipt_not_found")}

    def read(self, account: str, operation: str, *, limit=10, post_id="", target_url="", query="") -> dict:
        if not 1 <= limit <= 25:
            raise ValueError("limit must be 1..25")
        payload = {"mode": "read", "account": account, "operation": operation,
                   "limit": limit, "post_id": post_id, "target_url": target_url}
        if query:
            payload["query"] = query
        # Notifications and interactive reads share one persistent browser
        # profile. Stagger the first fetch and retry only the bridge's explicit
        # pre-open profile_busy result; no provider request was made in that case.
        self.sleep(self.jitter(1.0, 4.0))
        for attempt in range(self.max_busy_retries + 1):
            try:
                data = self._call(account, payload)
            except ThreadsDependencyUnavailable:
                raise
            except (AuthUnavailable, PublishUncertain) as exc:
                raise ThreadsReadFailure("Scrapling browser read unavailable") from exc
            if data.get("status") != "profile_busy":
                break
            if attempt == self.max_busy_retries:
                raise ProfileBusy("local browser profile remained busy")
            self.sleep(self.jitter(2.0, 6.0))
        if data.get("status") == "target_unavailable":
            return {"target_unavailable": True}
        if data.get("status") != "ok" or not isinstance(data.get("result"), dict):
            raise ThreadsReadFailure("Scrapling browser read unavailable")
        return data["result"]
