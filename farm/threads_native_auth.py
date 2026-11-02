"""Experimental Threads-app Bloks login, separate from Instagram session auth.

Protocol and constants adapted from teslashibe/threads-go auth.go/client.go at
09b38f44cea8042a69799bc04e36361d1d002047 (2026-10-26):
https://github.com/teslashibe/threads-go/blob/09b38f44cea8042a69799bc04e36361d1d002047/auth.go
https://github.com/teslashibe/threads-go/blob/09b38f44cea8042a69799bc04e36361d1d002047/client.go

MIT License
Copyright (c) 2026 Tesla Shibe

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

from datetime import datetime, timezone
from farm import locking as fcntl
import json
import os
from pathlib import Path
import re
from typing import Callable

from farm.threads_auth import (
    DEFAULT_AUTH_DIR, THREADS_TOKEN_SOURCE, LoginInputs, ThreadsAuthError,
    ThreadsToken, _ACCOUNT, _DEVICE, _private_file, _quiet_provider, save_token,
)


WRITE_BASE = "https://i.instagram.com"
APP_ID = "238260118697367"
BLOKS_VERSION = "00ba6fa565c3c707243ad976fa30a071a625f2a3d158d9412091176fe35027d8"
USER_AGENT = "Barcelona 289.0.0.77.109 Android"
LOGIN_APP = "com.bloks.www.bloks.caa.login.async.send_login_request"
TWO_FACTOR_APP = "com.bloks.www.bloks.caa.login.async.send_two_factor_login_request"
_MAX_RESPONSE_BYTES = 32 << 20
_BEARER = re.compile(r"Bearer (IGT:2:[A-Za-z0-9:_~+./=-]{24,})(?=[\"\\\s,}\]]|$)")


def _validate_code(code: str) -> str:
    if (not isinstance(code, str) or not code.isascii() or
            not code.isdecimal() or len(code) not in (6, 8)):
        raise ThreadsAuthError("invalid_code_format", needs_code=True)
    return code


def _unescape_bloks(body: str) -> str:
    """Scan the small known fields through Bloks' nested JSON escaping."""
    for _ in range(6):
        decoded = body.replace(r"\\", "\\").replace(r'\"', '"').replace(r"\/", "/")
        if decoded == body:
            break
        body = decoded
    return body


def _fields(body: str, name: str) -> set[str]:
    pattern = re.compile(r'"' + re.escape(name) +
                         r'"\s*:\s*(?:"([^"\\]*)"|([0-9]+)(?=\s*[,}\]]|$))')
    return {match.group(1) if match.group(1) is not None else match.group(2)
            for match in pattern.finditer(_unescape_bloks(body))}


def _two_factor_required(body: str) -> bool:
    return (bool(re.search(r'"two_factor_required"\s*:\s*true\b', _unescape_bloks(body))) or
            "two_factor_required" in _fields(body, "error_type"))


def _failure(body: str, *, code_submitted: bool = False) -> ThreadsAuthError | None:
    """Return fixed statuses only; provider text never becomes an exception."""
    if "checkpoint_required" in body or "challenge_required" in body:
        return ThreadsAuthError("checkpoint_required")
    if "rate_limit" in body or "please_wait_a_few_minutes" in body:
        return ThreadsAuthError("rate_limited")
    if ("login_required" in body or "8" in _fields(body, "logout_reason")):
        return ThreadsAuthError("session_suspended")
    if code_submitted and (_two_factor_required(body) or any(marker in body for marker in (
            "invalid_code", "incorrect_code", "code_rejected"))):
        return ThreadsAuthError("code_rejected", needs_code=True)
    if "fail" in _fields(body, "status"):
        return ThreadsAuthError("code_rejected" if code_submitted else "login_rejected",
                                needs_code=code_submitted)
    return None


def _request(http, app: str, client_input: dict, server_params: dict,
             *, code_submitted: bool = False) -> str:
    form = {
        "params": json.dumps({"client_input_params": client_input,
                              "server_params": server_params}, separators=(",", ":")),
        "bk_client_context": json.dumps({"bloks_version": BLOKS_VERSION,
                                         "styles_id": "instagram"}, separators=(",", ":")),
        "bloks_versioning_id": BLOKS_VERSION,
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": APP_ID,
        "X-IG-Capabilities": "3brTvx0=",
        "X-IG-Connection-Type": "WIFI",
    }
    try:
        with _quiet_provider():
            response = http.post(WRITE_BASE + "/api/v1/bloks/apps/" + app + "/",
                                 data=form, headers=headers, timeout=(10, 30),
                                 allow_redirects=False, stream=True)
            try:
                parts = []
                size = 0
                for part in response.iter_content(chunk_size=65536):
                    size += len(part)
                    if size > _MAX_RESPONSE_BYTES:
                        raise ThreadsAuthError("response_too_large")
                    parts.append(part)
                body = b"".join(parts).decode("utf-8")
                status = response.status_code
            finally:
                response.close()
    except ThreadsAuthError:
        raise
    except Exception:
        raise ThreadsAuthError("request_failed") from None

    if status == 429:
        raise ThreadsAuthError("rate_limited")
    if status >= 500:
        raise ThreadsAuthError("provider_unavailable")
    if 300 <= status < 400:
        raise ThreadsAuthError("login_redirected")
    failure = _failure(body, code_submitted=code_submitted)
    # A 2xx Bloks challenge is consumed by login; failed HTTP requests never
    # trigger automatic retries or second-factor submissions.
    if not 200 <= status < 300:
        raise failure or ThreadsAuthError("login_rejected")
    if failure and not (failure.status == "login_rejected" and not code_submitted and
                        _fields(body, "two_factor_identifier")):
        raise failure
    return body


def _check_inputs(inputs: LoginInputs) -> None:
    """Recheck the linked device/user identity while holding the IG lock."""
    try:
        if (not _ACCOUNT.fullmatch(inputs.username) or
                not isinstance(inputs.password, str) or not inputs.password or
                not _DEVICE.fullmatch(inputs.device_id) or
                not inputs.user_id.isascii() or not inputs.user_id.isdecimal() or
                not _private_file(inputs.session_path)):
            raise ValueError("inputs")
        saved = json.loads(inputs.session_path.read_text(encoding="utf-8"))
        if (saved["uuids"]["android_device_id"] != inputs.device_id or
                str(saved["authorization_data"]["ds_user_id"]) != inputs.user_id):
            raise ThreadsAuthError("identity_mismatch")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ThreadsAuthError("local_inputs_invalid") from None


def login(inputs: LoginInputs, *, verification_code: str = "",
          code_provider: Callable[[], str] | None = None,
          http=None, auth_dir: Path = DEFAULT_AUTH_DIR) -> ThreadsToken:
    """Make one Threads login and at most one authenticator-app submission.

    code_provider is called only after a two-factor identifier is returned, so
    the CLI can read a current code from stdin without persisting it. This flow
    does not refresh or write the saved Instagram session.
    """
    if verification_code:
        _validate_code(verification_code)
    owned_http = http is None
    lock_fd = None
    try:
        lock_path = inputs.session_path.with_name(inputs.session_path.name + ".lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _check_inputs(inputs)
        if http is None:
            try:
                import requests
            except ImportError:
                raise ThreadsAuthError("auth_backend_unavailable") from None
            http = requests.Session()
        body = _request(http, LOGIN_APP, {
            "password": inputs.password,
            "contact_point": inputs.username,
            "device_id": inputs.device_id,
        }, {"credential_type": "password", "device_id": inputs.device_id})
        identifiers = _fields(body, "two_factor_identifier") - {""}
        second_factor_submitted = False
        if identifiers:
            if len(identifiers) != 1:
                raise ThreadsAuthError("two_factor_unavailable")
            identifier = next(iter(identifiers))
            if not verification_code and code_provider is not None:
                verification_code = _validate_code(code_provider())
            if not verification_code:
                raise ThreadsAuthError("two_factor_required", needs_code=True)
            body = _request(http, TWO_FACTOR_APP, {
                "two_factor_identifier": identifier,
                "verification_code": verification_code,
                "verification_method": "3",
                "device_id": inputs.device_id,
                "username": inputs.username,
            }, {"device_id": inputs.device_id, "two_factor_identifier": identifier},
                            code_submitted=True)
            second_factor_submitted = True
        elif _two_factor_required(body):
            raise ThreadsAuthError("two_factor_unavailable")

        # Inspect fully unescaped token text so an escaped slash cannot look
        # like the end of a shorter bearer in an outer JSON layer.
        token_text = _unescape_bloks(body)
        tokens = {match.group(1) for match in _BEARER.finditer(token_text)}
        if not tokens:
            if second_factor_submitted and _fields(body, "two_factor_identifier") - {""}:
                raise ThreadsAuthError("code_rejected", needs_code=True)
            raise ThreadsAuthError("token_missing")
        if len(tokens) != 1:
            raise ThreadsAuthError("identity_mismatch")
        user_ids = _fields(body, "user_id") | _fields(body, "ig_user_id")
        usernames = _fields(body, "user_name")
        if (user_ids != {inputs.user_id} or
                any(name.casefold() != inputs.username.casefold() for name in usernames)):
            raise ThreadsAuthError("identity_mismatch")
        token = ThreadsToken(next(iter(tokens)), inputs.user_id, inputs.device_id,
                             inputs.username, datetime.now(timezone.utc).isoformat(),
                             source=THREADS_TOKEN_SOURCE)
        save_token(token, auth_dir)
        return token
    except ThreadsAuthError:
        raise
    except Exception:
        raise ThreadsAuthError("local_auth_failed") from None
    finally:
        if owned_http and http is not None:
            try:
                with _quiet_provider():
                    http.close()
            except Exception:
                pass
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
