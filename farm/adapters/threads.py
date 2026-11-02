"""Bounded Threads private API access using the registered Instagram session.

The read endpoints and write payload follow teslashibe/threads-go. These are
unofficial endpoints and may change. No method here logs or saves credentials.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from types import SimpleNamespace

import requests

from farm.accounts import AccountRegistry, account_key_matches
from farm.adapters import (
    AuthUnavailable, DependencyUnavailable, PreSendFailure, PublishRejected, PublishUncertain,
    ReadFailure, definite_rejection, request_never_sent,
)
from farm.outbox import frozen_payload_matches
from farm.threads_auth import DEFAULT_AUTH_DIR, ThreadsAuthError, load_token, verify_bearer_identity


READ_BASE = "https://www.threads.com"
WRITE_BASE = "https://i.instagram.com"
APP_ID = "238260118697367"


class ThreadsReadFailure(ReadFailure):
    """A bounded Threads read failed; response bodies are kept private."""


__all__ = [
    "AuthUnavailable", "DependencyUnavailable", "PreSendFailure", "PublishRejected",
    "PublishUncertain", "ThreadsAdapter", "ThreadsReadFailure",
]


def _post(item: dict) -> dict:
    user = item.get("user") or {}
    code = str(item.get("code") or "")
    username = str(user.get("username") or "")
    info = item.get("text_post_app_info") or {}
    timestamp = item.get("taken_at")
    if isinstance(timestamp, (int, float)):
        timestamp = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    return {
        "id": str(item.get("pk") or item.get("id") or item.get("strong_id__") or ""),
        "username": username,
        "body": str((item.get("caption") or {}).get("text") or ""),
        "timestamp": timestamp,
        "reply_count": info.get("direct_reply_count", 0),
        "permalink": f"{READ_BASE}/@{username}/post/{code}" if username and code else None,
    }


def _flatten(threads: list, limit: int) -> list[dict]:
    """Keep a complete provider page before returning its page-end cursor."""
    posts = []
    for thread in threads:
        for item in thread.get("thread_items") or []:
            # Threads REST wraps each post as {"post": {...}}. Keep direct
            # records readable for older or alternate response shapes.
            record = item.get("post") if isinstance(item, dict) else None
            post = _post(record if isinstance(record, dict) else item)
            if post["id"]:
                posts.append(post)
                if len(posts) > 500:
                    raise ThreadsReadFailure("Threads page exceeds local item bound")
    return posts


class ThreadsAdapter:
    # The mobile publisher asks Threads who the bearer belongs to before every send.
    LIVE_IDENTITY_CHECK = True

    def __init__(self, registry: AccountRegistry | None = None, http=None,
                 client_factory=None, auth_dir: Path = DEFAULT_AUTH_DIR):
        self.registry = registry or AccountRegistry()
        self.http = http or requests.Session()
        self.client_factory = client_factory
        self.auth_dir = auth_dir

    def _settings(self, account: str) -> dict:
        threads = self.registry.status("threads", account)
        instagram = self.registry.status("instagram", account)
        if not threads or not instagram:
            raise ThreadsReadFailure("Threads and Instagram account registrations are required")
        if (threads["kind"] != "instagrapi_settings" or
                instagram["kind"] != "instagrapi_settings" or
                not account_key_matches(instagram, threads["account_key"]) or
                threads["session_file"] != instagram["session_file"]):
            raise ThreadsReadFailure("Threads registration must share the Instagram session and account key")
        if not threads["session_exists"] or not threads["session_private"]:
            raise ThreadsReadFailure("shared account session is missing or not private")
        try:
            settings = json.loads(Path(threads["session_file"]).read_text(encoding="utf-8"))
            saved_cookies = settings.get("cookies") or {}
            authorization = settings.get("authorization_data") or {}
            if (saved_cookies.get("ds_user_id") and authorization.get("ds_user_id") and
                    str(saved_cookies["ds_user_id"]) != str(authorization["ds_user_id"])):
                raise ValueError("user ID mismatch")
            sessionid = authorization.get("sessionid") or saved_cookies.get("sessionid")
            user_id = authorization.get("ds_user_id") or saved_cookies.get("ds_user_id")
            if not sessionid or not user_id:
                raise ValueError("session cookies")
            if not settings["mid"] or not settings["uuids"]["uuid"]:
                raise ValueError("device cookies")
            settings["_threads_cookies"] = {
                "sessionid": sessionid, "ds_user_id": user_id,
                "csrftoken": saved_cookies.get("csrftoken") or "",
            }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ThreadsReadFailure("shared account session has incomplete settings") from exc
        return settings

    def _read(self, settings: dict, path: str, params: dict | None = None) -> dict:
        cookies = {name: value for name, value in settings["_threads_cookies"].items()
                   if value}
        cookies.update(mid=settings["mid"], ig_did=settings["uuids"]["uuid"])
        headers = {
            "User-Agent": "Barcelona 289.0.0.14.109 Android",
            "Accept": "application/json, text/plain, */*",
            "X-IG-App-ID": APP_ID,
            "X-ASBD-ID": "129477",
            "X-IG-WWW-Claim": "0",
            "Referer": READ_BASE + "/",
        }
        if cookies.get("csrftoken"):
            headers["X-CSRFToken"] = cookies["csrftoken"]
        try:
            response = self.http.get(READ_BASE + path, params=params, headers=headers,
                                     cookies=cookies, timeout=15, allow_redirects=False)
            if response.status_code != 200:
                raise ThreadsReadFailure(f"Threads read returned HTTP {response.status_code}")
            data = response.json()
            if not isinstance(data, dict) or data.get("status") == "fail":
                raise ThreadsReadFailure("Threads read returned a failure payload")
            return data
        except requests.RequestException as exc:
            raise ThreadsReadFailure(f"Threads read transport failed ({type(exc).__name__})") from exc
        except ValueError as exc:
            raise ThreadsReadFailure("Threads read returned non-JSON data") from exc

    def me(self, account: str) -> dict:
        settings = self._settings(account)
        data = self._read(settings, "/api/v1/accounts/current_user/", {"edit": "true"})
        user = data.get("user") or {}
        if str(user.get("username") or "").casefold() != account.casefold():
            raise ThreadsReadFailure("Threads session belongs to a different account")
        user_id = str(user.get("pk") or user.get("pk_id") or user.get("strong_id__") or "")
        if not user_id.isdigit():
            raise ThreadsReadFailure("Threads identity response has no numeric user ID")
        return {"id": user_id, "username": user["username"]}

    def own_posts(self, account: str, *, limit: int = 10, cursor: str = "") -> dict:
        if not 1 <= limit <= 25:
            raise ValueError("limit must be 1..25")
        identity = self.me(account)
        settings = self._settings(account)
        params = {"count": limit}
        if cursor:
            params["max_id"] = cursor
        data = self._read(settings, f"/api/v1/text_feed/{identity['id']}/profile/", params)
        return {"posts": _flatten(data.get("threads") or [], limit),
                "next_cursor": str(data.get("next_max_id") or "")}

    def replies(self, account: str, post_id: str, *, limit: int = 10,
                cursor: str = "") -> dict:
        if not str(post_id).isdigit() or not 1 <= limit <= 25:
            raise ValueError("numeric post ID and limit 1..25 are required")
        self.me(account)
        settings = self._settings(account)
        params = {"count": limit}
        if cursor:
            params["paging_token"] = cursor
        data = self._read(settings, f"/api/v1/text_feed/{post_id}/replies/", params)
        return {"post_id": post_id,
                "posts": _flatten(data.get("reply_threads") or [], limit),
                "next_cursor": str((data.get("paging_tokens") or {}).get("downwards") or "")}

    def _new_client(self):
        if self.client_factory is not None:
            return self.client_factory()
        from instagrapi import Client
        return Client()

    def _preflight(self, job: dict) -> tuple[dict, object, dict]:
        if job.get("platform") != "threads":
            raise PreSendFailure("job is not for Threads")
        if not frozen_payload_matches(job):
            raise PreSendFailure("queued text or media changed before send")
        if job.get("media_path"):
            raise PreSendFailure("standalone Threads publisher currently supports text only")
        body = str(job.get("body") or "")
        if not body.strip() or len(body) > 500:
            raise PreSendFailure("Threads text must contain 1..500 characters")
        if job.get("action") not in ("post", "reply"):
            raise PreSendFailure("unsupported Threads action")
        if job["action"] == "reply" and (not str(job.get("target_id") or "").isdigit()
                                           or str(job.get("media_id") or "") != str(job["target_id"])):
            raise PreSendFailure("Threads reply needs matching numeric media_id and target_id")
        registration = self.registry.status("threads", job["account"])
        instagram = self.registry.status("instagram", job["account"])
        if registration and registration["kind"] == "cookie_jar":
            raise AuthUnavailable("portable browser session selected")
        if not registration:
            raise AuthUnavailable("Threads account is not registered")
        if not instagram or not account_key_matches(registration, job.get("account_key")):
            raise PreSendFailure("Threads job account key does not match registration")
        try:
            settings = self._settings(job["account"])
            token = load_token(job["account"], self.auth_dir)
            bearer = "Bearer " + token.token
            device_id = str((settings.get("uuids") or {}).get("android_device_id") or "")
            if (token.device_id != device_id or
                    token.user_id != str(settings["_threads_cookies"]["ds_user_id"])):
                raise AuthUnavailable("Threads token identity does not match linked Instagram session")
            identity = {"id": token.user_id, "username": token.username}
            client = SimpleNamespace(user_id=token.user_id, android_device_id=device_id)
            headers = {
                "User-Agent": "Barcelona 289.0.0.77.109 Android", "Accept": "*/*",
                "X-IG-App-ID": APP_ID, "X-IG-Capabilities": "3brTvx0=",
                "X-IG-Connection-Type": "WIFI", "Authorization": bearer,
            }
            probe = self.http.get(
                WRITE_BASE + "/api/v1/feed/text_post_app_timeline/",
                params={"feed_type": "for_you", "feed_view_info": "[]",
                        "reason": "cold_start_fetch", "client_session_id": device_id},
                headers=headers, timeout=15, allow_redirects=False,
            )
            if probe.status_code != 200:
                if probe.status_code in (401, 403, 429):
                    raise AuthUnavailable(f"Threads bearer preflight returned HTTP {probe.status_code}")
                raise PreSendFailure(f"Threads bearer preflight returned HTTP {probe.status_code}")
            probe_data = probe.json()
            if (isinstance(probe_data, dict) and
                    probe_data.get("message") == "login_required"):
                raise AuthUnavailable("Threads bearer session unavailable")
            if not isinstance(probe_data, dict) or probe_data.get("status") == "fail":
                raise PreSendFailure("Threads bearer preflight returned failure")
            # Local files only prove the token matches the session file; a copied
            # token for another account would pass them, so the server decides.
            try:
                verify_bearer_identity(token, http=self.http, headers=headers)
            except ThreadsAuthError as exc:
                if exc.status == "identity_mismatch":
                    raise PreSendFailure("Threads bearer belongs to a different account") from exc
                if exc.status == "rate_limited":
                    raise AuthUnavailable("Threads identity check returned HTTP 429") from exc
                raise AuthUnavailable(f"Threads identity check failed ({exc.status})") from exc
            return identity, client, headers
        except PreSendFailure:
            raise
        except ThreadsReadFailure as exc:
            if "HTTP 401" in str(exc) or "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                raise AuthUnavailable("Threads identity read is unavailable") from exc
            raise PreSendFailure(str(exc)) from exc
        except ThreadsAuthError as exc:
            raise AuthUnavailable(exc.status) from exc
        except Exception as exc:
            if type(exc).__name__ in ("LoginRequired", "ClientLoginRequired",
                                      "ChallengeRequired", "ClientThrottledError"):
                raise AuthUnavailable("linked Instagram session unavailable") from exc
            raise PreSendFailure(f"Threads account preflight failed ({type(exc).__name__})") from exc

    def publish(self, job: dict, *, before_send=None) -> dict:
        _, client, headers = self._preflight(job)
        payload = {
            "caption": job["body"], "publish_mode": "text_post",
            "text_post_app_info": {"reply_control": "everyone"},
            "upload_id": str(time.time_ns()), "timezone_offset": "0",
            "source_type": "4", "_uid": str(client.user_id),
            "device_id": str(client.android_device_id),
        }
        if job["action"] == "reply":
            payload["text_post_app_info"]["reply_id"] = str(job["target_id"])
        signed_body = "SIGNATURE." + json.dumps(payload, separators=(",", ":"))
        if before_send is not None:
            before_send()
        try:
            response = self.http.post(
                WRITE_BASE + "/api/v1/media/configure_text_only_post/",
                data={"signed_body": signed_body},
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=20, allow_redirects=False,
            )
        except Exception as exc:
            if request_never_sent(exc):
                raise PreSendFailure(
                    f"Threads configure request never left this machine ({type(exc).__name__})") from exc
            raise PublishUncertain(f"Threads configure outcome unknown ({type(exc).__name__})") from exc
        try:
            if definite_rejection(response.status_code):
                raise PublishRejected(f"Threads configure rejected with HTTP {response.status_code}",
                                      status_code=response.status_code)
            if response.status_code != 200:
                raise PublishUncertain(f"Threads configure returned HTTP {response.status_code}")
            result = response.json()
            if not isinstance(result, dict) or result.get("status") != "ok":
                raise PublishUncertain("Threads configure did not confirm success")
            media = result.get("media") or {}
            provider_id = str(media.get("pk") or media.get("id") or "")
            if not provider_id:
                raise PublishUncertain("Threads configure returned no post ID")
            user = media.get("user") or {}
            code = media.get("code")
            username = user.get("username") or job["account"]
            permalink = f"{READ_BASE}/@{username}/post/{code}" if code else None
            return {"provider_id": provider_id, "permalink": permalink}
        except (PublishRejected, PublishUncertain):
            raise
        except Exception as exc:
            raise PublishUncertain(f"Threads configure answer unreadable ({type(exc).__name__})") from exc
