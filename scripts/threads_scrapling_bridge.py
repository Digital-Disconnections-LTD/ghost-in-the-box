#!/usr/bin/env python3
"""One private Scrapling browser operation; only stdin commands, sanitized output."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.outbox import frozen_payload_matches
from scripts import threads_scrapling_login as login


class BeforeSubmit(Exception):
    pass


def public_window(now=None) -> bool:
    """Leave a minute for the browser gesture to finish before midnight ET."""
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    return local.hour >= 6 and (local.hour < 23 or local.minute < 59)


def extract_posts(bodies):
    """Match post-shaped objects; embedded quoted/reposted children stay attached."""
    result, seen = [], set()
    stack = list(reversed(bodies))
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            key = str(item.get("pk") or item.get("id") or item.get("code") or "")
            shape = any(k in item for k in ("caption", "text_post_app_info", "like_count", "taken_at"))
            if key and shape and any(k in item for k in ("code", "text_post_app_info", "caption")):
                user = item.get("user") or {}
                caption = item.get("caption") or {}
                if user.get("username") or caption.get("text"):
                    if key not in seen:
                        result.append(item)
                        seen.add(key)
                    continue
            stack.extend(reversed(list(item.values())))
    return result


def capture_posts(page, target):
    bodies = []
    def response_received(response):
        if "graphql" not in response.url:
            return
        try:
            raw = response.text()
            if len(raw) > 20_000_000:
                return
            for line in raw.splitlines():
                try:
                    bodies.append(json.loads(line))
                except ValueError:
                    pass
        except Exception:
            pass
    page.on("response", response_received)
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=45000)
        embedded = page.locator('script[type="application/json"]').all_text_contents()
        for raw in embedded:
            if len(raw) <= 20_000_000:
                try:
                    bodies.append(json.loads(raw))
                except ValueError:
                    pass
        page.wait_for_timeout(2200)
        if not login.trusted(page) or login.inspect_page(page)["form_stage"] != "page":
            raise BeforeSubmit()
        return extract_posts(bodies)
    finally:
        page.remove_listener("response", response_received)


def normalized(post):
    user, caption = post.get("user") or {}, post.get("caption") or {}
    account, code = str(user.get("username") or ""), str(post.get("code") or "")
    stamp = post.get("taken_at")
    return {"id": str(post.get("pk") or post.get("id") or ""), "username": account,
            "body": str(caption.get("text") or ""),
            "timestamp": datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                if isinstance(stamp, (int, float)) else None,
            "reply_count": (post.get("text_post_app_info") or {}).get("direct_reply_count", 0),
            "like_count": post.get("like_count", 0),
            "permalink": f"{login.URL}/@{account}/post/{code}" if account and code else None}


def validate_command(command):
    if not isinstance(command, dict) or command.get("mode") not in {"publish", "verify", "read", "reconcile"}:
        raise BeforeSubmit()
    job = command.get("job") or {}
    account = command.get("account") if command["mode"] == "read" else job.get("account")
    if not isinstance(account, str) or account in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", account):
        raise BeforeSubmit()
    if command["mode"] != "read":
        if (job.get("platform") != "threads" or not frozen_payload_matches(job) or
                job.get("action") not in {"post", "reply"} or job.get("media_path") or
                not isinstance(job.get("body"), str) or not 1 <= len(job["body"]) <= 500 or
                not job["body"].strip()):
            raise BeforeSubmit()
        if job["action"] == "reply":
            if (not str(job.get("target_id") or "").isdigit() or
                    str(job.get("media_id") or "") != str(job["target_id"]) or
                    not valid_target(job.get("target_url"))):
                raise BeforeSubmit()
    elif (command.get("operation") not in {"me", "own", "replies", "target", "search", "feed"} or
            type(command.get("limit")) is not int or not 1 <= command["limit"] <= 25 or
            (command["operation"] in {"replies", "target"} and
             (not str(command.get("post_id") or "").isdigit() or not valid_target(command.get("target_url")))) or
            (command["operation"] == "search" and
             (not isinstance(command.get("query"), str) or
              not 2 <= len(command["query"].strip()) <= 120 or
              any(ord(char) < 32 for char in command["query"])))):
        raise BeforeSubmit()
    return account, job


def valid_target(target):
    return isinstance(target, str) and re.fullmatch(
        r"https://www\.threads\.com/@[A-Za-z0-9._]+/post/[A-Za-z0-9_-]+/?", target)


def receipt_on_page(posts, job, account, *, not_before=None, seen=()):
    """Return one exact observed receipt, including the reply's parent author."""
    matches = []
    target_author = job.get("target_url", "").split("/@")[-1].split("/")[0].casefold()
    for raw in posts:
        post = normalized(raw)
        if (post["id"] in seen or not post["id"].isdigit() or
                post["username"].casefold() != account.casefold() or
                post["body"].rstrip() != job["body"].rstrip() or
                not post["permalink"] or not re.fullmatch(
                    rf"{re.escape(login.URL)}/@{re.escape(account)}/post/[A-Za-z0-9_-]+",
                    post["permalink"])):
            continue
        stamp = raw.get("taken_at")
        if not_before is not None and (not isinstance(stamp, (int, float)) or stamp < not_before - 120):
            continue
        if job["action"] == "reply":
            info = raw.get("text_post_app_info") or {}
            parent = info.get("reply_to_author") or {}
            if info.get("is_reply") is not True or str(parent.get("username") or "").casefold() != target_author:
                continue
        matches.append(post)
    return matches[0] if len(matches) == 1 else None


def prepare_submit(page, job):
    """Fill a single empty editor, return exactly one submit gesture."""
    context = page.context
    if job["action"] == "post":
        page.goto(login.URL + "/", wait_until="domcontentloaded", timeout=45000)
        opener = None
        for _ in range(50):
            opener = login.visible(page, ['[aria-label="Create"]',
                '[role="button"]:has-text("New thread")', ':text-is("What\'s new?")',
                ':text-is("Start a thread")'])
            if opener is not None:
                break
            time.sleep(0.2)
        if opener is None:
            raise BeforeSubmit("post_control_missing")
        try:
            opener.click(timeout=10000, no_wait_after=True)
        except Exception:
            # Meta may open a new tab and leave Playwright waiting on the
            # original page. The composer presence check below decides.
            pass
        scope_selector = '[role="menu"]:has([contenteditable="true"])'
    else:
        page.goto(job["target_url"], wait_until="domcontentloaded", timeout=45000)
        # The post page has an inline editor for the target. Other Reply
        # buttons belong to nested replies and must not be chosen by position.
        target_author = job["target_url"].split("/@")[-1].split("/")[0]
        inline = page.locator(f'[contenteditable="true"][aria-placeholder="Reply to {target_author}..."]')
        expand = page.locator('[aria-label="Expand composer"]')
        inline.first.wait_for(state="visible", timeout=10000)
        if inline.count() != 1 or expand.count() != 1 or not expand.is_visible():
            raise BeforeSubmit("reply_control_missing")
        try:
            expand.click(timeout=10000, no_wait_after=True)
        except Exception:
            pass
        scope_selector = '[role="dialog"]:has([contenteditable="true"])'

    # Threads may move either composer into another browser tab. Bind the
    # editor and its Post control to the same menu/dialog, not to feed chrome.
    deadline = time.monotonic() + 10
    scope = None
    active_page = None
    while time.monotonic() < deadline:
        candidates = []
        for candidate in context.pages:
            if candidate.is_closed() or not login.trusted(candidate):
                continue
            panel = candidate.locator(scope_selector)
            if panel.count() == 1:
                candidates.append((candidate, panel))
        if len(candidates) == 1:
            active_page, scope = candidates[0]
            break
        if len(candidates) > 1:
            raise BeforeSubmit("multiple_composers")
        time.sleep(0.2)
    if scope is None:
        raise BeforeSubmit("composer_missing")
    if not login.trusted(active_page):
        raise BeforeSubmit("composer_site")
    editor = scope.locator('[contenteditable="true"]')
    editor.first.wait_for(state="visible", timeout=10000)
    if editor.count() != 1 or editor.first.inner_text().strip():
        raise BeforeSubmit("editor_not_empty")
    editor.first.fill(job["body"])
    if editor.first.inner_text() != job["body"]:
        raise BeforeSubmit("editor_readback")
    button = scope.get_by_role("button", name=re.compile(r"^post$", re.I))
    if button.count() != 1:
        raise BeforeSubmit("post_control_ambiguous")
    for _ in range(20):
        if button.is_enabled():
            break
        time.sleep(0.2)
    else:
        raise BeforeSubmit("post_control_disabled")
    return lambda: button.click(timeout=10000, no_wait_after=True)


def current_page(page):
    """Choose the surviving browser tab for receipt reads after the composer closes."""
    try:
        pages = [item for item in page.context.pages if not item.is_closed()]
    except TypeError:  # mock pages in isolated unit tests
        return page
    return pages[-1] if pages else page


def attempt_path(account_dir, job):
    identity = {key: job.get(key) for key in ("id", "account", "action", "body_sha256", "target_id")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return login.private_dir(account_dir / "attempts") / (digest + ".json")


def execute(page, command, account_dir, *, capture=capture_posts, identity=login.verify_identity,
            prepare=prepare_submit):
    account, job = validate_command(command)
    attempted = False
    journal = None
    phase = "identity"
    try:
        verified, cookies = identity(page, account)
        if not verified:
            return {"status": "auth_unavailable"}
        if command["mode"] == "verify":
            return {"status": "verified", "account": account}
        profile_url = f"{login.URL}/@{account}"
        if command["mode"] == "read":
            if command["operation"] == "me":
                user_id = next(c["value"] for c in cookies if c["name"] == "ds_user_id" and
                               c["domain"].lstrip(".") in {"threads.com", "www.threads.com"})
                return {"status": "ok", "result": {"id": user_id, "username": account}}
            operation = command["operation"]
            target = (profile_url if operation == "own" else
                      command["target_url"] if operation in {"replies", "target"} else
                      login.URL + "/" if operation == "feed" else
                      login.URL + "/search?q=" + quote(command["query"].strip(), safe=""))
            posts = [normalized(p) for p in capture(page, target)]
            if operation in {"replies", "target"}:
                parent = [p for p in posts if p["id"] == str(command["post_id"]) and
                          p["permalink"] == command["target_url"].rstrip("/")]
                if len(parent) != 1:
                    return {"status": "target_unavailable"}
                if operation == "target":
                    return {"status": "ok", "result": {"post": parent[0]}}
            posts = [p for p in posts if p["id"] and p["permalink"] and
                     (p["username"].casefold() == account.casefold() if operation == "own"
                      else p["id"] != str(command.get("post_id") or ""))][:command["limit"]]
            result = {"posts": posts, "next_cursor": ""}
            if operation == "replies":
                result["post_id"] = str(command["post_id"])
            if operation == "search":
                result["query"] = command["query"].strip()
            return {"status": "ok", "result": result}
        if command["mode"] == "reconcile":
            journal = attempt_path(account_dir, job)
            if not journal.exists():
                return {"status": "unconfirmed", "reason": "no_attempt_record"}
            old = login.private_json(journal)
            if old.get("body_sha256", job["body_sha256"]) != job["body_sha256"]:
                return {"status": "unconfirmed", "reason": "attempt_mismatch"}
            if old.get("status") == "published":
                return {k: old[k] for k in ("status", "provider_id", "permalink")}
            if old.get("status") != "attempted":
                return {"status": "unconfirmed", "reason": "no_attempt_record"}
            try:
                attempted_at = datetime.fromisoformat(old["at"]).timestamp()
                target = f"{login.URL}/@{account}" if job["action"] == "post" else job["target_url"]
                posts = capture(page, target)
                receipt = receipt_on_page(posts, job, account, not_before=attempted_at)
            except (ValueError, KeyError, TypeError):
                receipt = None
            if receipt is None:
                return {"status": "unconfirmed", "reason": "receipt_not_found"}
            result = {"status": "published", "provider_id": receipt["id"],
                      "permalink": receipt["permalink"]}
            login.save_json(journal, result)
            return result
        phase = "journal_check"
        journal = attempt_path(account_dir, job)
        if journal.exists():
            old = login.private_json(journal)
            if old.get("status") == "published":
                return {k: old[k] for k in ("status", "provider_id", "permalink")}
            return {"status": "possible_duplicate"}
        target = profile_url if job["action"] == "post" else job["target_url"]
        phase = "read_before"
        before = [normalized(p) for p in capture(page, target)]
        if job["action"] == "reply" and not any(p["id"] == str(job["target_id"]) and
                p["permalink"] == job["target_url"].rstrip("/") for p in before):
            return {"status": "pre_send_failure", "phase": "read_before",
                    "reason": "target_unavailable"}
        own = lambda posts: [p for p in posts if p["username"].casefold() == account.casefold()]
        matches = lambda posts: [p for p in own(posts) if p["body"].rstrip() == job["body"].rstrip()]
        if matches(before):
            return {"status": "possible_duplicate"}
        seen = {p["id"] for p in before}
        phase = "prepare_composer"
        try:
            original_pages = set(page.context.pages)
        except TypeError:  # isolated adapter tests use a mock context
            original_pages = {page}
        try:
            submit = prepare(page, job)
        except BeforeSubmit as exc:
            if str(exc) not in {"post_control_missing", "composer_missing"}:
                raise
            # No submit gesture or attempt journal exists yet. A delayed
            # Threads shell/composer can be reopened once with the same job.
            try:
                added_pages = set(page.context.pages) - original_pages
            except TypeError:
                added_pages = set()
            for extra in added_pages:
                if not extra.is_closed():
                    extra.close()
            page.wait_for_timeout(750)
            submit = prepare(page, job)
        if not public_window():
            return {"status": "quiet_hours"}
        # Durable intent precedes the only public gesture; a crash cannot resend.
        phase = "journal_write"
        login.save_json(journal, {"status": "attempted", "body_sha256": job["body_sha256"],
                                 "at": datetime.now(timezone.utc).isoformat()})
        attempted = True
        phase = "submit"
        submit()
        for _ in range(6):
            reader = current_page(page)
            reader.wait_for_timeout(1600)
            try:
                found = receipt_on_page(capture(current_page(page), target), job, account, seen=seen)
                if found:
                    result = {"status": "published", "provider_id": found["id"],
                              "permalink": found["permalink"]}
                    login.save_json(journal, result)
                    return result
            except Exception:
                pass
        return {"status": "uncertain"}
    except Exception as exc:
        result = {"status": "uncertain" if attempted else "pre_send_failure",
                  "phase": phase}
        # These are fixed local codes from prepare_submit, never provider text.
        if not attempted and isinstance(exc, BeforeSubmit) and str(exc) in {
                "post_control_missing", "reply_control_missing", "composer_missing",
                "multiple_composers", "composer_site", "editor_not_empty",
                "editor_readback", "post_control_ambiguous", "post_control_disabled"}:
            result["reason"] = str(exc)
        return result


def run(command, *, factory=None):
    account, _job = validate_command(command)
    account_dir = Path(os.environ.get("SOCIAL_FARM_THREADS_SCRAPLING_STATE") or login.BASE) / account
    if account_dir.is_symlink() or not account_dir.is_dir():
        return {"status": "auth_unavailable"}
    profile = account_dir / "profile"
    if profile.is_symlink() or not profile.is_dir():
        return {"status": "auth_unavailable"}
    result = {"status": "auth_unavailable"}
    with login.lock(account_dir / ".lock"):
        if factory is None:
            from scrapling.fetchers import StealthySession
            factory = StealthySession
        try:
            with factory(user_data_dir=str(profile), headless=True, locale="en-US",
                         timezone_id="America/New_York", retries=1, timeout=45000,
                         google_search=False, solve_cloudflare=False,
                         additional_args={"viewport": {"width": 1366, "height": 900}}) as session:
                def action(page):
                    nonlocal result
                    result = execute(page, command, account_dir)
                # An error in response conversion after execute must preserve its receipt.
                try:
                    session.fetch(login.URL + "/", page_action=action, network_idle=False)
                except Exception:
                    pass
        except Exception:
            pass
    return result


def main():
    os.umask(0o077)
    result = {"status": "pre_send_failure"}
    try:
        raw = sys.stdin.read(100_001)
        if len(raw) > 100_000:
            raise BeforeSubmit()
        command = json.loads(raw)
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            logging.disable(logging.CRITICAL)
            result = run(command)
    except BlockingIOError:
        result = {"status": "profile_busy"}
    except Exception:
        pass
    print(json.dumps(result))
    return 0 if result["status"] in {"published", "verified", "ok"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
