"""Reuse the existing Threads browser executor with portable cookies and Playwright."""
from __future__ import annotations

import os
from pathlib import Path

from farm import locking
from farm.accounts import account_key_matches
from farm.adapters.threads import AuthUnavailable, PreSendFailure
from farm.config import threads_portable_dir
from farm.session_files import read_session, write_session
from farm.threads_scrapling import ScraplingThreadsAdapter
from farm.threads_transport import (CHROMIUM_INSTALL, LOGIN_INSTALL,
                                    ThreadsDependencyUnavailable)


def _browser_missing(exc: Exception) -> bool:
    # Playwright's own local message for a missing browser build; it never
    # contains provider data because no page has been opened yet.
    text = str(exc)
    return "Executable doesn't exist" in text or "playwright install" in text


class PortableThreadsAdapter(ScraplingThreadsAdapter):
    def __init__(self, *, registry, state_dir=None):
        super().__init__(registry=registry, state_dir=state_dir or threads_portable_dir())

    def _registration(self, job):
        entry = self.registry.get("threads", job["account"])
        if (not entry or entry["kind"] != "cookie_jar" or
                not account_key_matches(entry, job.get("account_key"))):
            raise PreSendFailure("Threads job account registration mismatch")

    def _call(self, account, payload):
        from farm.login import validate_session
        from scripts.threads_scrapling_bridge import execute, validate_command
        validate_command(payload)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ThreadsDependencyUnavailable("playwright", LOGIN_INSTALL) from None
        entry = self.registry.get("threads", account)
        if not entry or entry["kind"] != "cookie_jar":
            raise AuthUnavailable("browser_login_required")
        account_dir = self.state_dir / account
        account_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(account_dir / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        result = None
        try:
            locking.flock(fd, locking.LOCK_EX | locking.LOCK_NB)
            path = Path(entry["session_file"])
            saved = read_session(path)
            validate_session("threads", account, saved)
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except Exception as exc:
                    if _browser_missing(exc):
                        raise ThreadsDependencyUnavailable("chromium", CHROMIUM_INSTALL) from None
                    raise
                try:
                    context = browser.new_context(user_agent=saved["user_agent"])
                    context.add_cookies(saved["cookies"])
                    page = context.new_page()
                    result = execute(page, payload, account_dir)
                    try:
                        saved["cookies"] = context.cookies()
                        write_session(path, saved)
                    except OSError:
                        # Keep a confirmed send receipt even if cookie persistence fails.
                        pass
                    return result
                finally:
                    browser.close()
        except BlockingIOError:
            return {"status": "profile_busy"}
        except ThreadsDependencyUnavailable:
            raise
        except Exception:
            # execute owns the submit boundary and reports every attempted send.
            # Startup failures happen before execute, so they can wait for setup.
            if result is not None:
                return result
            raise AuthUnavailable("portable browser session unavailable") from None
        finally:
            os.close(fd)
