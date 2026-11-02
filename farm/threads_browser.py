"""Persistent browser fallback for a single exact Threads outbox job.

The bridge uses the locally installed, MIT licensed threads-mcp browser code.
Secrets cross only an inherited stdin pipe and are never logged or put in argv.
"""

from __future__ import annotations

from farm.accounts import account_key_matches
from farm.config import threads_browser_dir



from contextlib import contextmanager
from farm import locking as fcntl
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from farm.accounts import AccountRegistry
from farm.outbox import frozen_payload_matches
from farm.adapters.threads import (AuthUnavailable, PreSendFailure,
                                   PublishUncertain, ThreadsReadFailure)
from farm.threads_transport import (NODE_INSTALL, SETUP_COMMAND, XVFB_INSTALL,
                                    ThreadsDependencyUnavailable,
                                    node_paths, _linux_x64)


BASE = threads_browser_dir()
BRIDGE = Path(__file__).resolve().parents[1] / "scripts/threads_browser_bridge.mjs"
READ_BRIDGE = Path(__file__).resolve().parents[1] / "scripts/threads_browser_read_bridge.mjs"


def browser_command(node: Path, bridge: Path) -> list[str]:
    xvfb = shutil.which("xvfb-run")
    if xvfb:
        return [xvfb, "-a", str(node), str(bridge)]
    if sys.platform == "darwin" or os.name == "nt" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return [str(node), str(bridge)]
    raise ThreadsDependencyUnavailable("xvfb-run", XVFB_INSTALL,
                                       "browser fallback needs a display or xvfb-run")


def _setup_missing(account: str, node: Path, message: str) -> ThreadsDependencyUnavailable:
    """Name the missing piece of the Node browser and the command that installs it."""
    if not node.is_file():
        install = (SETUP_COMMAND if _linux_x64() else NODE_INSTALL).format(account=account)
        return ThreadsDependencyUnavailable("node", install, message)
    return ThreadsDependencyUnavailable("threads-mcp", SETUP_COMMAND.format(account=account), message)


@contextmanager
def _profile_lock(state_dir: Path, account: str):
    parent = state_dir / "profile"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    fd = os.open(parent / f".{account}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield parent / account
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class BrowserThreadsAdapter:
    def __init__(self, *, registry: AccountRegistry | None = None,
                 runner=subprocess.run, state_dir: Path = BASE,
                 scrapling_state_dir: Path | None = None):
        self.registry = registry or AccountRegistry()
        self.runner = runner
        self.state_dir = Path(state_dir)
        self._post_urls: dict[str, str] = {}
        # Keep explicit test/custom browser roots isolated from the user's state.
        self.scrapling_state_dir = (Path(scrapling_state_dir) if scrapling_state_dir is not None else
                                   self.state_dir.parent / "threads-scrapling")

    def _scrapling(self, account: str):
        registration = self.registry.status("threads", account)
        if registration and registration["kind"] == "cookie_jar":
            from farm.threads_portable import PortableThreadsAdapter
            return PortableThreadsAdapter(registry=self.registry,
                                          state_dir=self.state_dir.parent / "threads-portable")
        from farm.threads_scrapling import ScraplingThreadsAdapter, verified_profile
        if verified_profile(account, self.scrapling_state_dir):
            return ScraplingThreadsAdapter(registry=self.registry, runner=self.runner,
                                           state_dir=self.scrapling_state_dir)
        return None

    def _settings(self, account: str) -> tuple[str, str]:
        if not re.fullmatch(r"[A-Za-z0-9._]+", account):
            raise AuthUnavailable("account unavailable")
        threads = self.registry.status("threads", account)
        instagram = self.registry.status("instagram", account)
        if (not threads or not instagram or
                threads["kind"] != "instagrapi_settings" or
                instagram["kind"] != "instagrapi_settings" or
                threads["account_key"] != instagram["account_key"] or
                threads["session_file"] != instagram["session_file"]):
            raise AuthUnavailable("linked Instagram session unavailable")
        # A browser profile can outlive the mobile session. The saved Instagram
        # cookies are only a seed for an empty profile; identity is verified in
        # the live browser before a submit.
        if not threads["session_exists"] or not threads["session_private"]:
            return "", ""
        try:
            data = json.loads(Path(threads["session_file"]).read_text(encoding="utf-8"))
            auth = data.get("authorization_data") or {}
            cookies = data.get("cookies") or {}
            if (cookies.get("ds_user_id") and auth.get("ds_user_id") and
                    str(cookies["ds_user_id"]) != str(auth["ds_user_id"])):
                raise AuthUnavailable("linked Instagram user ID mismatch")
            sessionid = str(auth.get("sessionid") or cookies.get("sessionid") or "")
            user_id = str(auth.get("ds_user_id") or cookies.get("ds_user_id") or "")
            if len(sessionid) <= 8 or not user_id.isdigit():
                raise ValueError("session incomplete")
            return sessionid, user_id
        except (OSError, ValueError, TypeError, KeyError):
            return "", ""

    def verify(self, account: str) -> dict:
        """Seed or reuse a durable profile and confirm its live account identity."""
        registration = self.registry.status("threads", account)
        if not registration:
            raise AuthUnavailable("Threads account registration unavailable")
        body = "identity check"
        job = {"platform": "threads", "account": account,
               "account_key": registration["account_key"], "action": "post",
               "body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
               "media_path": None, "media_sha256": None}
        return self.publish(job, verify_only=True)

    def publish(self, job: dict, *, verify_only: bool = False) -> dict:
        if job.get("platform") != "threads" or not frozen_payload_matches(job):
            raise PreSendFailure("Threads job changed before send")
        if job.get("media_path") or job.get("action") not in ("post", "reply"):
            raise PreSendFailure("browser publisher supports text posts and replies")
        if not str(job.get("body") or "").strip() or len(job["body"]) > 500:
            raise PreSendFailure("Threads text must contain 1..500 characters")
        registration = self.registry.status("threads", job["account"])
        if not registration or not account_key_matches(registration, job.get("account_key")):
            raise PreSendFailure("Threads job account key mismatch")
        if job["action"] == "reply" and (
                not str(job.get("target_id") or "").isdigit() or
                str(job.get("media_id") or "") != str(job["target_id"])):
            raise PreSendFailure("Threads reply target mismatch")
        scrapling = self._scrapling(job["account"])
        if scrapling is not None:
            return scrapling.publish(job, verify_only=verify_only)
        sessionid, user_id = self._settings(job["account"])
        node, source = node_paths(self.state_dir)
        if not (source / "build/browser/session.js").is_file() or not node.is_file():
            # The router maps this exact message to browser_setup_required.
            raise _setup_missing(job["account"], node, "browser fallback needs one-time setup")
        command = browser_command(node, BRIDGE)
        payload = {
            "account": job["account"], "action": job["action"],
            "body": job["body"], "target_url": job.get("target_url"),
            "sessionid": sessionid, "user_id": user_id,
            "mode": "verify" if verify_only else "publish",
        }
        try:
            with _profile_lock(self.state_dir, job["account"]) as profile:
                profile.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(profile, 0o700)
                environment = os.environ.copy()
                environment.update({
                    "THREADS_PROFILE_DIR": str(profile),
                    "SOCIAL_FARM_THREADS_MCP_SOURCE": str(source),
                    "CLOAKBROWSER_CACHE_DIR": str(self.state_dir / "binary"),
                    "DEBUG": "false",
                })
                result = self.runner(
                    command, input=json.dumps(payload),
                    text=True, capture_output=True, timeout=180, check=False,
                    env=environment,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # The subprocess may have submitted before failing or timing out.
            raise PublishUncertain("browser outcome unknown") from exc
        try:
            lines = result.stdout.strip().splitlines()
            data = json.loads(lines[-1]) if lines else {}
            status = data.get("status")
        except (ValueError, TypeError, AttributeError):
            status = None
            data = {}
        if verify_only and status == "verified" and data.get("account") == job["account"]:
            return {"status": "verified", "account": job["account"]}
        if status == "published":
            provider_id = str(data.get("provider_id") or "")
            permalink = str(data.get("permalink") or "")
            if not provider_id.isdigit() or not permalink.startswith(
                    f"https://www.threads.com/@{job['account']}/post/"):
                raise PublishUncertain("browser receipt incomplete")
            return {"provider_id": provider_id, "permalink": permalink}
        if status == "pre_send_failure":
            raise PreSendFailure("browser job unsupported before send")
        if status == "auth_unavailable":
            raise AuthUnavailable("browser_login_required" if
                                  data.get("account_action") == "browser_login_required" else
                                  "retry_after_cooldown")
        if status == "provider_unavailable":
            raise AuthUnavailable("retry_after_cooldown")
        if status == "bridge_unavailable":
            raise AuthUnavailable("browser_setup_required")
        if status == "possible_duplicate":
            raise PublishUncertain("matching Threads post already exists")
        if status == "uncertain":
            raise PublishUncertain("browser submit outcome unknown")
        raise PublishUncertain("browser bridge outcome unknown")

    def _read(self, account: str, operation: str, *, limit: int = 10,
              post_id: str = "", target_url: str = "") -> dict:
        if not 1 <= limit <= 25:
            raise ValueError("limit must be 1..25")
        scrapling = self._scrapling(account)
        if scrapling is not None:
            return scrapling.read(account, operation, limit=limit, post_id=post_id,
                                  target_url=target_url)
        try:
            sessionid, user_id = self._settings(account)
        except AuthUnavailable as exc:
            raise ThreadsReadFailure("browser read account unavailable") from exc
        node, source = node_paths(self.state_dir)
        if not (source / "build/browser/session.js").is_file() or not node.is_file():
            raise _setup_missing(account, node, "browser read setup unavailable")
        command = browser_command(node, READ_BRIDGE)
        payload = {"account": account, "operation": operation, "limit": limit,
                   "post_id": post_id, "target_url": target_url,
                   "sessionid": sessionid, "user_id": user_id}
        try:
            with _profile_lock(self.state_dir, account) as profile:
                profile.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(profile, 0o700)
                environment = os.environ.copy()
                environment.update({"THREADS_PROFILE_DIR": str(profile),
                                    "SOCIAL_FARM_THREADS_MCP_SOURCE": str(source),
                                    "CLOAKBROWSER_CACHE_DIR": str(self.state_dir / "binary"),
                                    "DEBUG": "false"})
                response = self.runner(command,
                                       input=json.dumps(payload), text=True,
                                       capture_output=True, timeout=150, check=False,
                                       env=environment)
            lines = response.stdout.strip().splitlines()
            data = json.loads(lines[-1]) if lines else {}
            if data.get("status") != "ok" or not isinstance(data.get("result"), dict):
                raise ValueError("browser read unavailable")
            return data["result"]
        except (OSError, ValueError, TypeError, AttributeError,
                subprocess.TimeoutExpired) as exc:
            raise ThreadsReadFailure("browser read unavailable") from exc

    def me(self, account: str) -> dict:
        return self._read(account, "me", limit=1)

    def own_posts(self, account: str, *, limit: int = 10, cursor: str = "") -> dict:
        if not 1 <= limit <= 25:
            raise ValueError("limit must be 1..25")
        if cursor:
            return {"posts": [], "next_cursor": ""}
        result = self._read(account, "own", limit=limit)
        for post in result.get("posts") or []:
            if post.get("id") and post.get("permalink"):
                self._post_urls[str(post["id"])] = str(post["permalink"])
        return result

    def replies(self, account: str, post_id: str, *, limit: int = 10,
                cursor: str = "") -> dict:
        if not str(post_id).isdigit() or not 1 <= limit <= 25:
            raise ValueError("numeric post ID and limit 1..25 are required")
        if cursor:
            return {"post_id": str(post_id), "posts": [], "next_cursor": ""}
        target = self._post_urls.get(str(post_id))
        if not target:
            self.own_posts(account, limit=25)
            target = self._post_urls.get(str(post_id))
        if not target:
            raise ThreadsReadFailure("post is outside recent own browser page")
        return self._read(account, "replies", limit=limit, post_id=str(post_id),
                          target_url=target)
