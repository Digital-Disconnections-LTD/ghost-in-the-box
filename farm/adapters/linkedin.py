"""Bounded LinkedIn Voyager calls using an owner-created browser session.

These are unofficial LinkedIn web routes and may change. Text posts use the
share call of LinkedIn's current web client (Voyager GraphQL
``voyagerContentcreationDashShares``). LinkedIn rotates that query id, so
SOCIAL_FARM_LINKEDIN_SHARE_QUERY_ID can override it without a release. A send is
never retried automatically.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
from urllib.parse import quote
import uuid

import requests

from farm.accounts import AccountRegistry, account_key_matches
from farm import locking
from farm.adapters import (
    AuthUnavailable, DependencyUnavailable, PreSendFailure, PublishRejected, PublishUncertain,
    ReadFailure, definite_rejection, request_never_sent, safe_excerpt,
)
from farm.outbox import frozen_payload_matches
from farm.session_files import read_session, write_session

BASE = "https://www.linkedin.com"
API = BASE + "/voyager/api/"
SHARE_QUERY_ENV = "SOCIAL_FARM_LINKEDIN_SHARE_QUERY_ID"
SHARE_QUERY_ID = "voyagerContentcreationDashShares.279996efa5064c01775d5aff003d9377"
SHARE_OPERATION = "voyagerContentcreationDashShares"
SHARE_RESULT = "createContentcreationDashShares"
_QUERY_HASH = re.compile(r"[0-9a-f]{32}")
_SHARE_URN = re.compile(r"(?:urn:li:fsd_share:)?(urn:li:(?:share|ugcPost):\d+)")
_LOGIN_PATHS = ("/login", "/uas/", "/checkpoint/", "/authwall", "/signup")


class LinkedInReadFailure(ReadFailure):
    """A bounded LinkedIn read failed; response bodies stay private."""


__all__ = [
    "AuthUnavailable", "DependencyUnavailable", "LinkedInAdapter", "LinkedInReadFailure",
    "PreSendFailure", "PublishRejected", "PublishUncertain", "identity", "little_text",
    "share_query_id",
]


def share_query_id() -> str:
    """Return the GraphQL share query id, honouring the environment override."""
    configured = (os.environ.get(SHARE_QUERY_ENV) or "").strip()
    if not configured:
        return SHARE_QUERY_ID
    if _QUERY_HASH.fullmatch(configured):
        return SHARE_OPERATION + "." + configured
    operation, _, digest = configured.partition(".")
    if operation == SHARE_OPERATION and _QUERY_HASH.fullmatch(digest):
        return configured
    raise PreSendFailure(f"{SHARE_QUERY_ENV} must be {SHARE_OPERATION}.<32 hex> or the 32 hex hash")


def share_payload(text: str, query_id: str) -> dict:
    return {"variables": {"post": {
                "allowedCommentersScope": "ALL", "intendedShareLifeCycleState": "PUBLISHED",
                "origin": "FEED", "visibilityDataUnion": {"visibilityType": "ANYONE"},
                "commentary": {"text": text, "attributesV2": []}}},
            "queryId": query_id, "includeWebMetadata": True}


def share_receipt(data) -> tuple[str | None, list[str]]:
    """Return the created share URN (if any) and any GraphQL error messages."""
    if not isinstance(data, dict):
        return None, []
    errors = []
    outer = data.get("data")
    for holder in (data, outer if isinstance(outer, dict) else {}):
        found = holder.get("errors")
        if isinstance(found, list):
            for error in found:
                message = error.get("message") if isinstance(error, dict) else error
                errors.append(message if isinstance(message, str) and message.strip() else "unnamed error")
    inner = outer.get("data") if isinstance(outer, dict) else None
    result = inner.get(SHARE_RESULT) if isinstance(inner, dict) else None
    if isinstance(result, dict):
        for key in ("resourceKey", "*entity", "entityUrn"):
            value = result.get(key)
            match = _SHARE_URN.fullmatch(value) if isinstance(value, str) else None
            if match:
                return match.group(1), errors
    return None, errors


def identity(data: dict) -> dict:
    root = data.get("data", data)
    if not isinstance(root, dict):
        raise LinkedInReadFailure("LinkedIn identity is unavailable")
    profile = root.get("miniProfile")
    if not isinstance(profile, dict):
        ref = root.get("*miniProfile")
        profile = next((p for p in data.get("included", [])
                        if isinstance(p, dict) and p.get("entityUrn") == ref), {})
    username = profile.get("publicIdentifier")
    urn = profile.get("entityUrn", "")
    if not isinstance(username, str) or not re.fullmatch(r"urn:li:(?:fs_miniProfile|person):[\w-]+", urn):
        raise LinkedInReadFailure("LinkedIn identity is unavailable")
    return {"username": username, "id": urn.rsplit(":", 1)[1],
            "author_urn": "urn:li:person:" + urn.rsplit(":", 1)[1]}


def little_text(text: str) -> str:
    result = []
    for pos, char in enumerate(text):
        hashtag = (char == "#" and pos + 1 < len(text) and text[pos + 1].isalpha()
                   and (pos == 0 or not (text[pos - 1].isalnum() or text[pos - 1] in "/=&")))
        result.append(("\\" if char in "\\[]|{}()<>@#*_~" and not hashtag else "") + char)
    return "".join(result)


class LinkedInAdapter:
    def __init__(self, registry=None, http=None):
        self.registry = registry or AccountRegistry()
        self.http = http or requests.Session()
        self.page_instance = str(uuid.uuid4())

    @contextmanager
    def _session(self, account):
        entry = self.registry.status("linkedin", account)
        if not entry or entry["kind"] != "cookie_jar" or not entry["session_exists"]:
            raise AuthUnavailable("LinkedIn browser login is required")
        path = Path(entry["session_file"])
        fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            locking.flock(fd, locking.LOCK_EX)
            try:
                saved = read_session(path)
                if saved.get("platform") != "linkedin" or saved.get("account", "").casefold() != account.casefold():
                    raise ValueError("identity mismatch")
                cookies = saved["cookies"]
                self.http.cookies.clear()
                for c in cookies:
                    if c["domain"].lstrip(".") not in {"linkedin.com", "www.linkedin.com"}:
                        raise ValueError("unexpected cookie domain")
                    self.http.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"),
                                          secure=c.get("secure", True),
                                          expires=int(c["expires"]) if c.get("expires", -1) > 0 else None)
                values = {c.name: c.value for c in self.http.cookies}
                if not values.get("li_at") or not values.get("JSESSIONID"):
                    raise ValueError("incomplete cookies")
                headers = {"User-Agent": saved["user_agent"], "Csrf-Token": values["JSESSIONID"].strip('"'),
                           "X-Li-Page-Instance": self.page_instance, "X-Restli-Protocol-Version": "2.0.0",
                           "Accept": "application/vnd.linkedin.normalized+json+2.1", "X-Li-Lang": "en_US"}
            except (ValueError, OSError, KeyError, TypeError):
                raise AuthUnavailable("LinkedIn session is unavailable") from None
            try:
                yield headers
            finally:
                saved["cookies"] = [{"name": c.name, "value": c.value, "domain": c.domain,
                                      "path": c.path, "secure": c.secure, "expires": c.expires or -1}
                                     for c in self.http.cookies]
                try:
                    write_session(path, saved)
                    self.session_saved = True
                except Exception:
                    self.session_saved = False
        finally:
            locking.flock(fd, locking.LOCK_UN)
            os.close(fd)

    def _get(self, account, path, params=None):
        try:
            with self._session(account) as headers:
                response = self.http.get(API + path, params=params, headers=headers, timeout=20, allow_redirects=False)
                if response.status_code in (301, 302, 303, 401, 403, 429):
                    raise AuthUnavailable("LinkedIn session expired, challenged or throttled")
                if response.status_code != 200:
                    raise LinkedInReadFailure(f"LinkedIn read returned HTTP {response.status_code}")
                data = response.json()
                if not isinstance(data, dict) or data.get("status", 200) not in (200, "ok"):
                    raise LinkedInReadFailure("LinkedIn returned an unexpected payload")
                return data
        except (requests.RequestException, ValueError):
            raise LinkedInReadFailure("LinkedIn read failed") from None

    def me(self, account):
        me = identity(self._get(account, "me"))
        if me["username"].casefold() != account.casefold():
            raise LinkedInReadFailure("LinkedIn session belongs to a different account")
        return me

    def profile(self, account, handle):
        self.me(account)
        return self._get(account, "identity/profiles/" + quote(handle, safe="") + "/profileView")

    def own_posts(self, account, *, limit=10, start=0):
        self._bounds(limit, start)
        me = self.me(account)
        return self._get(account, "feed/updates", {"q": "memberShareFeed", "moduleKey": "member-share",
                                                   "profileId": me["id"], "count": limit, "start": start})

    def conversations(self, account, *, limit=10, start=0):
        self._bounds(limit, start)
        self.me(account)
        return self._get(account, "messaging/conversations", {"keyVersion": "LEGACY_INBOX", "count": limit, "start": start})

    def messages(self, account, conversation_id, *, limit=10, start=0):
        self._bounds(limit, start)
        self.me(account)
        return self._get(account, "messaging/conversations/" + quote(conversation_id, safe="") + "/events",
                         {"count": limit, "start": start})

    @staticmethod
    def _bounds(limit, start):
        if not 1 <= limit <= 25 or not 0 <= start <= 10000:
            raise ValueError("limit must be 1..25 and start 0..10000")

    def publish(self, job, *, before_send=None):
        if job.get("platform") != "linkedin" or not frozen_payload_matches(job):
            raise PreSendFailure("LinkedIn job changed before send")
        if (job.get("action") != "post" or job.get("media_path") or job.get("media_id") or
                job.get("target_id") or not job.get("body", "").strip() or len(job["body"]) > 3000):
            raise PreSendFailure("LinkedIn supports text posts of 1..3000 characters")
        entry = self.registry.get("linkedin", job["account"])
        if not entry:
            raise AuthUnavailable("LinkedIn browser login is required")
        if not account_key_matches(entry, job.get("account_key")):
            raise PreSendFailure("LinkedIn job account key mismatch")
        query_id = share_query_id()
        try:
            self.me(job["account"])
        except LinkedInReadFailure:
            raise PreSendFailure("LinkedIn identity could not be verified") from None
        payload = share_payload(job["body"], query_id)
        attempted = False
        try:
            with self._session(job["account"]) as session_headers:
                headers = {**session_headers, "Content-Type": "application/json; charset=UTF-8",
                           "X-Li-Graphql-Pegasus-Client": "true",
                           "X-Li-Pem-Metadata": "Voyager - Sharing - CreateShare=sharing-create-content",
                           "Origin": BASE, "Referer": BASE + "/feed/"}
                secrets = [v for c in self.http.cookies if c.value for v in (c.value, c.value.strip('"'))]
                if before_send:
                    before_send()
                attempted = True
                try:
                    response = self.http.post(API + "graphql", params={"action": "execute", "queryId": query_id},
                                              json=payload, headers=headers, timeout=30, allow_redirects=False)
                except Exception as exc:
                    if request_never_sent(exc):
                        raise PreSendFailure(
                            f"LinkedIn request never left this machine ({type(exc).__name__})") from None
                    raise
                receipt = self._share_outcome(response, secrets)
            return {**receipt, "session_saved": self.session_saved}
        except (PreSendFailure, PublishUncertain):
            raise
        except Exception:
            if attempted:
                raise PublishUncertain("LinkedIn send outcome requires reconciliation") from None
            raise

    @staticmethod
    def _share_outcome(response, secrets) -> dict:
        status = response.status_code
        excerpt = safe_excerpt(getattr(response, "text", ""), secrets=secrets)
        if status in (401, 403):
            raise AuthUnavailable(f"LinkedIn refused the session with HTTP {status}")
        if 300 <= status < 400:
            location = (response.headers.get("Location") or "").split("?", 1)[0]
            if any(part in location for part in _LOGIN_PATHS):
                raise AuthUnavailable(f"LinkedIn redirected the post to sign-in (HTTP {status})")
            raise PublishUncertain(f"LinkedIn answered the post with a redirect (HTTP {status})",
                                   status_code=status, excerpt=excerpt)
        if definite_rejection(status):
            raise PublishRejected(f"LinkedIn rejected the post with HTTP {status}",
                                  status_code=status, excerpt=excerpt)
        if status not in (200, 201):
            raise PublishUncertain(f"LinkedIn answered the post with HTTP {status}",
                                   status_code=status, excerpt=excerpt)
        try:
            data = response.json()
        except ValueError:
            raise PublishUncertain(f"LinkedIn post answer was unreadable (HTTP {status})",
                                   status_code=status, excerpt=excerpt) from None
        provider_id, errors = share_receipt(data)
        if provider_id:
            return {"provider_id": provider_id, "permalink": BASE + "/feed/update/" + provider_id + "/"}
        if errors:
            raise PublishRejected(f"LinkedIn rejected the post (HTTP {status} with errors)",
                                  status_code=status, excerpt=safe_excerpt("; ".join(errors), secrets=secrets))
        raise PublishUncertain("LinkedIn did not return a confirmed post receipt",
                               status_code=status, excerpt=excerpt)
