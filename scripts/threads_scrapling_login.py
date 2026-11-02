#!/usr/bin/env python3
"""Private, bounded Threads web login using Scrapling's persistent StealthySession.

No composer or publishing code is imported. Diagnostic mode only navigates an
isolated copy of the profile. One credential submission per provider site and (if necessary) one OTP submission
are allowed per invocation; raw provider data never reaches the terminal.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm import locking as fcntl
from farm.config import STATE_ROOT
from farm.config import credentials_path
from farm.threads_auth import ThreadsAuthError, read_verification_code
from farm.cli_output import EXIT_AUTH, EXIT_RUNTIME, EXIT_USAGE, attach_error, guarded
from farm.threads_transport import scrapling_install

BASE = STATE_ROOT / 'threads-scrapling'
BROWSER_BASE = STATE_ROOT / 'threads-browser-fallback'
CREDS = credentials_path()
IG_SESSIONS = STATE_ROOT / 'ig'
URL = "https://www.threads.com"
BRIDGE = Path(__file__).with_name("threads_scrapling_cookie_bridge.mjs")
PASSWORD = ['input[type="password"]']
USERNAME = ['input[name="username"]', 'input[autocomplete="username"]',
            'input[type="email"]', 'input[type="text"]']
OTP = ['input[autocomplete="one-time-code"]', 'input[name="verificationCode"]',
       'input[name="verification_code"]', 'input[name="security_code"]',
       'input[name="code"]']
CHOICE = ['button:has-text("Log in with Instagram")',
          'a:has-text("Log in with Instagram")',
          'button:has-text("Continue with Instagram")',
          'a:has-text("Continue with Instagram")',
          '[role="button"]:has-text("Log in with Instagram")',
          '[role="button"]:has-text("Continue with Instagram")']


class LocalInputsInvalid(Exception):
    pass


_RELOGIN = ("ghost-in-the-box login start --platform threads --account {account}, "
            "or ghost-in-the-box threads --account {account} connect --refresh")
ERRORS = {
    "local_inputs_invalid": (EXIT_USAGE, "The account, credentials file or saved Instagram "
                             "session is missing, not private or does not match.",
                             "Check --account and that the session files are chmod 600."),
    "dependency_unavailable": (EXIT_AUTH, "{dependency} is not installed.", "{install}"),
    "browser_setup_required": (EXIT_AUTH, "The Node browser transport is not set up.",
                               "ghost-in-the-box threads-setup --account {account}"),
    "verification_expired": (EXIT_AUTH, "The verified browser login is older than 15 minutes.",
                             "Run the login again, then transfer."),
    "linked_account_unavailable": (EXIT_AUTH, "Threads did not offer the linked Instagram "
                                   "account.", _RELOGIN),
    "identity_unverified": (EXIT_AUTH, "The browser could not confirm it is signed in as "
                            "@{account}.", _RELOGIN),
    "two_factor_required": (EXIT_AUTH, "Threads asked for an authenticator code.",
                            "Re-run with --mode login --verification-code-stdin."),
    "code_rejected": (EXIT_AUTH, "The authenticator code was rejected.",
                      "Wait for a new code and re-run once."),
    "checkpoint_required": (EXIT_AUTH, "Threads requires a security checkpoint.",
                            "Finish the checkpoint in the Threads or Instagram app, then retry."),
    "login_rejected": (EXIT_AUTH, "Threads rejected the login.", _RELOGIN),
    "profile_busy": (EXIT_RUNTIME, "Another command is using this browser profile.",
                     "Wait for it to finish, then retry."),
}


def private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise LocalInputsInvalid()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def private_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise LocalInputsInvalid()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LocalInputsInvalid()
    return value


def save_json(path: Path, data: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".save-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        # Fail promptly instead of waiting behind a forgotten browser process.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def read_code() -> str:
    # Called only after a visible OTP challenge; a bad code is a local input error.
    try:
        return read_verification_code()
    except ThreadsAuthError:
        raise ValueError("invalid_code_format") from None


def trusted(page) -> bool:
    parsed = urlparse(page.url)
    return parsed.scheme == "https" and parsed.hostname in {
        "threads.com", "www.threads.com", "instagram.com", "www.instagram.com"}


def visible(page, selectors):
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count() and locator.is_visible():
            return locator
    return None


def inspect_page(page) -> dict:
    """Only fixed enums and field-presence booleans, never URL/text/values."""
    host = urlparse(page.url).hostname or ""
    site = ("threads" if host in {"threads.com", "www.threads.com"} else
            "instagram" if host in {"instagram.com", "www.instagram.com"} else "other")
    path = urlparse(page.url).path.lower()
    fields = {"password": bool(visible(page, PASSWORD)),
              "username": bool(visible(page, USERNAME)),
              "code": bool(visible(page, OTP)),
              "instagram_choice": bool(visible(page, CHOICE))}
    # Detect common hard stops without exposing provider response bodies.
    markers = page.evaluate("""() => {
      const text = (document.body?.innerText || '').toLowerCase();
      return {throttled: /try again later|too many requests|please wait a few minutes/.test(text),
        rejected: /incorrect password|password (?:was|is) incorrect|invalid password/.test(text),
        checkpoint: /confirm (?:it.s you|your identity)|suspicious login|check your notifications/.test(text)};
    }""")
    stage = ("unexpected_site" if site == "other" else
             "provider_throttled" if markers.get("throttled") else
             "login_rejected" if markers.get("rejected") else
             "two_factor" if fields["code"] else
             "checkpoint" if markers.get("checkpoint") or
                 re.search(r"checkpoint|challenge|two_factor", path) else
             "credentials" if fields["password"] else
             "instagram_choice" if fields["instagram_choice"] else
             "login" if re.search(r"(?:^|/)login(?:/|$)", path) else "page")
    controls = page.evaluate("""() => Array.from(document.querySelectorAll('body *'))
      .filter(n => /^(log in|continue with instagram)$/i.test((n.textContent || '').trim()) &&
        n.getClientRects().length && !Array.from(n.children).some(c =>
          (c.textContent || '').trim() === (n.textContent || '').trim()))
      .slice(0, 6).map(n => ({tag: n.tagName.toLowerCase(),
        role: ['button', 'link'].includes(n.getAttribute('role')) ? n.getAttribute('role') : 'none',
        label: /^log in$/i.test((n.textContent || '').trim()) ? 'log_in' : 'instagram_choice',
        disabled: n.hasAttribute('disabled') || n.getAttribute('aria-disabled') === 'true',
        ancestors: Array.from((function*(){let p=n.parentElement; for(let i=0;p&&i<3;i++,p=p.parentElement) yield p;})())
          .map(p => ({tag: p.tagName.toLowerCase(), role: ['button', 'link'].includes(p.getAttribute('role')) ?
            p.getAttribute('role') : 'none'}))}))""")
    return {"site": site, "form_stage": stage, "fields": fields, "controls": controls}


def settled(page, timeout_ms=15000, submitted_site=None):
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    while True:
        pages = [p for p in page.context.pages if not p.is_closed()]
        current = pages[-1] if pages else page
        state = inspect_page(current)
        stage = state["form_stage"]
        # Existing fields can remain visible while a credential request is in
        # flight. Do not mistake that transient DOM for a rejected password.
        if submitted_site is not None:
            pending = stage in {"page", "login"} or (
                stage == "credentials" and state["site"] == submitted_site)
            ready = (not pending or (stage == "page" and time.monotonic() - started >= 2))
        else:
            ready = stage not in {"page", "login"}
        if ready or time.monotonic() >= deadline:
            return current, state
        current.wait_for_timeout(350)


def submit(page, *, code=False) -> bool:
    # Do not use a generic Continue/Enter shortcut that could submit a composer.
    selectors = (['button:has-text("Confirm")', 'button:has-text("Verify")',
                  'button:has-text("Continue")', '[role="button"]:text-is("Confirm")',
                  '[role="button"]:text-is("Verify")', '[role="button"]:text-is("Continue")',
                  'button[type="submit"]'] if code else
                 ['button[type="submit"]', 'input[type="submit"]',
                  'button:has-text("Log in")', '[role="button"]:has-text("Log in")',
                  ':text-is("Log in")'])
    button = visible(page, selectors)
    if not trusted(page) or not button:
        return False
    button.click(timeout=10000)
    return True


# Handles in the signed-in Profile link; the login wizard reads the same link.
PROFILE_HANDLES = """() => Array.from(document.querySelectorAll('a[href^="/@"]'))
      .filter(a => /^(?:profile|your profile)$/i.test(a.getAttribute('aria-label') || '') ||
        Array.from(a.querySelectorAll('[aria-label]')).some(n =>
          /^(?:profile|your profile)$/i.test(n.getAttribute('aria-label') || '')))
      .map(a => a.getAttribute('href').match(/^\\/@([A-Za-z0-9._]+)\\/?$/)?.[1])
      .filter(Boolean)"""


def verify_identity(page, account: str) -> tuple[bool, list]:
    """Require the signed-in Profile link and matching embedded account ID."""
    page.goto(URL + "/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2200)
    if not trusted(page) or inspect_page(page)["form_stage"] != "page":
        return False, []
    handles = page.evaluate(PROFILE_HANDLES)
    if not handles or set(h.casefold() for h in handles) != {account.casefold()}:
        return False, []
    cookies = page.context.cookies([URL, "https://www.instagram.com"])
    own = [c for c in cookies if c["domain"].lstrip(".") in {"threads.com", "www.threads.com"}]
    sessions = {c["value"] for c in own if c["name"] == "sessionid" and len(c["value"]) > 8}
    ids = {c["value"] for c in own if c["name"] == "ds_user_id" and c["value"].isdigit()}
    if len(sessions) != 1 or len(ids) != 1:
        return False, []
    user_id = next(iter(ids))
    page.goto(f"{URL}/@{account}", wait_until="domcontentloaded", timeout=45000)
    # Read server-rendered identity before React hydration removes JSON blocks.
    matched = page.evaluate("""({account, userId}) => {
      const walk = value => {
        if (!value || typeof value !== 'object') return false;
        if (String(value.username || '').toLowerCase() === account.toLowerCase() &&
            [value.pk, value.id].some(id => String(id || '') === userId)) return true;
        return Object.values(value).some(walk);
      };
      return Array.from(document.querySelectorAll('script[type="application/json"]')).some(n => {
        try { return walk(JSON.parse(n.textContent || '')); } catch { return false; }
      });
    }""", {"account": account, "userId": user_id})
    # Snapshot the rotated cookies only after both identity checks succeed.
    final = page.context.cookies([URL, "https://www.instagram.com"])
    final_ids = {c["value"] for c in final if c["name"] == "ds_user_id" and
                 c["domain"].lstrip(".") in {"threads.com", "www.threads.com"}}
    return bool(matched and final_ids == {user_id}), final


def screenshot_private(page, path: Path) -> bool:
    try:
        page.screenshot(path=str(path), full_page=False,
                        mask=[page.locator("input, textarea, [contenteditable='true']")])
        os.chmod(path, 0o600)
        return True
    except Exception:
        return False


def linked_instagram_cookies(account: str) -> list[dict]:
    """Import the already working Instagram app session into an isolated web profile."""
    settings = private_json(IG_SESSIONS / f"{account}.json")
    authorization = settings.get("authorization_data") or {}
    sessionid = authorization.get("sessionid")
    user_id = str(authorization.get("ds_user_id") or "")
    if (not isinstance(sessionid, str) or len(sessionid) < 8 or
            not user_id.isdecimal()):
        raise LocalInputsInvalid()
    values = {
        "sessionid": sessionid,
        "ds_user_id": user_id,
        "mid": settings.get("mid"),
        "ig_did": (settings.get("uuids") or {}).get("uuid"),
        "rur": (settings.get("cookies") or {}).get("rur"),
    }
    return [{"name": name, "value": value, "domain": ".instagram.com",
             "path": "/", "secure": True}
            for name, value in values.items() if isinstance(value, str) and value]


def browser_run(args, profile: Path, account_dir: Path, *, factory=None) -> dict:
    result = {"ok": False, "status": "browser_unavailable", "needs_code": False}
    credentials = private_json(CREDS) if args.mode == "login" else None
    if credentials and (str(credentials.get("username", "")).casefold() != args.account.casefold()
                        or not isinstance(credentials.get("password"), str)
                        or not credentials["password"]):
        raise LocalInputsInvalid()
    if args.mode in {"login", "linked"}:
        # Old exports must never remain usable following a new failed login.
        (account_dir / "verified-cookies.json").unlink(missing_ok=True)

    def action(page):
        try:
            if args.mode == "linked":
                page.context.add_cookies(linked_instagram_cookies(args.account))
                page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
                page.goto(URL + "/login", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1800)
                result.update(inspect_page(page))
                choice = page.get_by_text(args.account, exact=True)
                if not trusted(page) or choice.count() != 1 or not choice.is_visible():
                    result["status"] = "linked_account_unavailable"
                    return
                choice.click(timeout=10000)
                page, state = settled(page, timeout_ms=20000, submitted_site="threads")
                result.update(state)
                verified, cookies = verify_identity(page, args.account)
                if not verified:
                    result["status"] = "identity_unverified"
                    return
                save_json(account_dir / "verified-cookies.json", {
                    "version": 1, "account": args.account,
                    "verified_at": datetime.now(timezone.utc).isoformat(), "cookies": cookies})
                result.update(ok=True, status="saved", form_stage="verified", needs_code=False)
                return
            page, state = settled(page)
            result.update(state)
            if args.mode == "diagnose":
                result.update(ok=True, status="diagnostic")
                return
            stage = state["form_stage"]
            if stage == "instagram_choice":
                choice = visible(page, CHOICE)
                if not trusted(page) or choice is None:
                    return
                choice.click(timeout=10000)
                page, state = settled(page)
                result.update(state)
                stage = state["form_stage"]
            submitted_sites = set()
            while stage == "credentials" and state["site"] not in submitted_sites:
                submitted_sites.add(state["site"])
                username, password = visible(page, USERNAME), visible(page, PASSWORD)
                if (len(submitted_sites) > 2 or not trusted(page) or
                        state["site"] not in {"threads", "instagram"} or
                        username is None or password is None):
                    result["status"] = "login_form_unavailable"
                    return
                username.fill(args.account)
                password.fill(credentials["password"])
                if not submit(page):
                    result["status"] = "login_form_unavailable"
                    return
                page, state = settled(page, timeout_ms=20000, submitted_site=state["site"])
                result.update(state)
                stage = state["form_stage"]
            if stage == "two_factor":
                result.update(status="two_factor_required", needs_code=True)
                if not args.verification_code_stdin:
                    return
                code = read_code()
                field = visible(page, OTP)
                if not trusted(page) or field is None:
                    return
                field.fill(code)
                del code
                if not submit(page, code=True):
                    return
                page, state = settled(page, timeout_ms=20000, submitted_site=state["site"])
                result.update(state)
                stage = state["form_stage"]
                if stage == "two_factor":
                    result["status"] = "code_rejected"
                    return
            if stage in {"checkpoint", "provider_throttled", "login_rejected", "unexpected_site"}:
                result.update(status={"checkpoint": "checkpoint_required"}.get(stage, stage),
                              needs_code=False)
                return
            if stage in {"credentials", "login"}:
                result.update(status="login_rejected", needs_code=False)
                return
            result.update(status="identity_unverified", needs_code=False)
            verified, cookies = verify_identity(page, args.account)
            if not verified:
                result.update(inspect_page(page))
                return
            save_json(account_dir / "verified-cookies.json", {
                "version": 1, "account": args.account,
                "verified_at": datetime.now(timezone.utc).isoformat(), "cookies": cookies})
            result.update(ok=True, status="saved", form_stage="verified", needs_code=False)
        except ValueError:
            result.update(ok=False, status="local_inputs_invalid", needs_code=False)
        except Exception:
            result.update(ok=False, status="browser_action_unavailable", needs_code=False)
        finally:
            if args.screenshot:
                result["screenshot_saved"] = screenshot_private(page, account_dir / "diagnostic.png")

    if factory is None:
        try:
            from scrapling.fetchers import StealthySession
        except ImportError:
            return {"ok": False, "status": "dependency_unavailable", "needs_code": False,
                    "dependency": "scrapling",
                    "install": scrapling_install(sys.executable)}
        factory = StealthySession
    # No automatic retries: one invocation can submit credentials once only.
    with factory(user_data_dir=str(profile), headless=not args.headed,
                 locale="en-US", timezone_id="America/New_York", retries=1,
                 timeout=45000, google_search=False, solve_cloudflare=False,
                 additional_args={"viewport": {"width": 1366, "height": 900}}) as session:
        # Starting at /login may redirect an already authenticated profile home.
        session.fetch(URL + "/login", page_action=action, network_idle=False)
    return result


def transfer(account: str, account_dir: Path) -> dict:
    verified = private_json(account_dir / "verified-cookies.json")
    if verified.get("account", "").casefold() != account.casefold():
        raise LocalInputsInvalid()
    stamp = datetime.fromisoformat(verified["verified_at"])
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if not 0 <= age <= 900:
        return {"ok": False, "status": "verification_expired", "needs_code": False}
    parent = private_dir(BROWSER_BASE / "profile")
    active = parent / account
    if active.is_symlink():
        raise LocalInputsInvalid()
    node = BROWSER_BASE / "node-v24.22.1-linux-x64/bin/node"
    xvfb = shutil.which("xvfb-run")
    if not node.is_file() or not xvfb:
        return {"ok": False, "status": "browser_setup_required", "needs_code": False}
    with lock(parent / f".{account}.lock"):
        temporary = Path(tempfile.mkdtemp(prefix=".scrapling-transfer-", dir=parent))
        try:
            if active.is_dir():
                copy_profile(active, temporary)
            environment = os.environ.copy()
            environment.update(THREADS_PROFILE_DIR=str(temporary),
                               SOCIAL_FARM_THREADS_MCP_SOURCE=str(BROWSER_BASE / "source"),
                               CLOAKBROWSER_CACHE_DIR=str(BROWSER_BASE / "binary"), DEBUG="false")
            response = subprocess.run([xvfb, "-a", str(node), str(BRIDGE)],
                                      input=json.dumps(verified), text=True, capture_output=True,
                                      timeout=90, check=False, env=environment)
            try:
                imported = json.loads(response.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                imported = {}
            if response.returncode or imported.get("status") != "imported":
                return {"ok": False, "status": "transfer_unavailable", "needs_code": False}
            backup = parent / f".{account}.before-scrapling"
            if backup.exists():
                shutil.rmtree(backup)
            if active.exists():
                os.replace(active, backup)
            try:
                os.replace(temporary, active)
            except OSError:
                if backup.exists():
                    os.replace(backup, active)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            return {"ok": True, "status": "transferred", "needs_code": False}
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def copy_profile(source: Path, target: Path) -> None:
    # Chromium lock symlinks point outside the profile and must never be copied.
    shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True,
                    ignore=shutil.ignore_patterns("Singleton*", "DevToolsActivePort"))


@guarded("threads")
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="your Threads handle")
    parser.add_argument("--mode", choices=("diagnose", "login", "linked", "transfer"), default="diagnose")
    parser.add_argument("--verification-code-stdin", action="store_true")
    parser.add_argument("--headed", action="store_true", help="Requires DISPLAY (or xvfb-run).")
    parser.add_argument("--screenshot", action="store_true", help="Save a private masked screenshot.")
    args = parser.parse_args(argv)
    os.umask(0o077)
    result = {"ok": False, "status": "local_inputs_invalid", "needs_code": False}
    try:
        if (args.account in {".", ".."} or len(args.account) > 30 or
                not re.fullmatch(r"[A-Za-z0-9._]+", args.account) or
                (args.verification_code_stdin and args.mode != "login")):
            raise LocalInputsInvalid()
        account_dir = private_dir(private_dir(BASE) / args.account)
        with lock(account_dir / ".lock"):
            if args.mode == "transfer":
                result = transfer(args.account, account_dir)
            else:
                profile = private_dir(account_dir / "profile")
                # Linked login starts clean so stale cookies from failed web
                # attempts cannot defeat a working Instagram account choice.
                with tempfile.TemporaryDirectory(prefix=".diagnostic-", dir=account_dir) as temporary:
                    selected = Path(temporary) if args.mode in {"diagnose", "linked"} else profile
                    if args.mode == "diagnose":
                        copy_profile(profile, selected)
                    with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
                        previous_logging = logging.root.manager.disable
                        logging.disable(logging.CRITICAL)
                        try:
                            result = browser_run(args, selected, account_dir)
                        finally:
                            logging.disable(previous_logging)
                    if args.mode == "linked" and result.get("ok"):
                        backup = account_dir / ".profile-before-linked"
                        if backup.exists():
                            shutil.rmtree(backup)
                        os.replace(profile, backup)
                        try:
                            os.replace(selected, profile)
                        except OSError:
                            os.replace(backup, profile)
                            (account_dir / "verified-cookies.json").unlink(missing_ok=True)
                            raise
                        shutil.rmtree(backup)
    except (LocalInputsInvalid, ValueError, KeyError, TypeError):
        result = {"ok": False, "status": "local_inputs_invalid", "needs_code": False}
    except BlockingIOError:
        result = {"ok": False, "status": "profile_busy", "needs_code": False}
    except Exception:
        result = {"ok": False, "status": "browser_unavailable", "needs_code": False}
    code = attach_error(result, ERRORS, account=args.account)
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
