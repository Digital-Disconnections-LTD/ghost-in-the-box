"""Bounded LinkedIn Voyager calls using an owner-created browser session.

These are unofficial LinkedIn web routes and may change. Writes remain
experimental until the operator verifies them against their account; no
automatic send retries.
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
    ReadFailure, definite_rejection, request_never_sent,
)
from farm.outbox import frozen_payload_matches
from farm.session_files import read_session, write_session

BASE = "https://www.linkedin.com"
API = BASE + "/voyager/api/"


class LinkedInReadFailure(ReadFailure):
    """A bounded LinkedIn read failed; response bodies stay private."""


__all__ = [
    "AuthUnavailable", "DependencyUnavailable", "LinkedInAdapter", "LinkedInReadFailure",
    "PreSendFailure", "PublishRejected", "PublishUncertain", "identity", "little_text",
]


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
        try:
            me = self.me(job["account"])
        except LinkedInReadFailure:
            raise PreSendFailure("LinkedIn identity could not be verified") from None
        payload = {"author": me["author_urn"], "commentary": little_text(job["body"]),
                   "visibility": "PUBLIC", "lifecycleState": "PUBLISHED",
                   "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                                    "thirdPartyDistributionChannels": []}}
        attempted = False
        try:
            with self._session(job["account"]) as headers:
                if before_send:
                    before_send()
                attempted = True
                try:
                    response = self.http.post(API + "feed/updates", json=payload, headers=headers,
                                              timeout=30, allow_redirects=False)
                except Exception as exc:
                    if request_never_sent(exc):
                        raise PreSendFailure(
                            f"LinkedIn request never left this machine ({type(exc).__name__})") from None
                    raise
                if definite_rejection(response.status_code):
                    raise PublishRejected(f"LinkedIn rejected the post with HTTP {response.status_code}",
                                          status_code=response.status_code)
                provider_id = response.headers.get("x-restli-id", "")
                if response.status_code not in (200, 201) or not re.fullmatch(r"urn:li:(?:share|ugcPost|activity):\d+", provider_id):
                    raise PublishUncertain("LinkedIn did not return a confirmed post receipt")
                receipt = {"provider_id": provider_id, "permalink": BASE + "/feed/update/" + provider_id}
            return {**receipt, "session_saved": self.session_saved}
        except (PreSendFailure, PublishUncertain):
            raise
        except Exception:
            if attempted:
                raise PublishUncertain("LinkedIn send outcome requires reconciliation") from None
            raise
