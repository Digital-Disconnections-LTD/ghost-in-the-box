"""Instagram delivery through the locally saved instagrapi session."""

from __future__ import annotations

from farm.config import STATE_ROOT


import os
import json
from pathlib import Path
import re
import shutil
import tempfile
import fcntl

from farm.accounts import AccountRegistry
from farm.outbox import file_sha256, frozen_payload_matches
from farm.instagram_session import InstagramSessionError, read_with_session


class PreSendFailure(Exception):
    """No public publish call was attempted."""


class AuthUnavailable(PreSendFailure):
    """Saved Instagram auth could not be used; keep the queued job."""


class PublishUncertain(Exception):
    """A publish call began; provider state needs reconciliation."""


class InstagramAdapter:
    def __init__(self, registry: AccountRegistry | None = None, client_factory=None,
                 session_refresh=None):
        self.registry = registry or AccountRegistry()
        self.client_factory = client_factory
        self.session_refresh = session_refresh
        self.client = None
        self.session_path: Path | None = None
        self.session_mtime_ns: int | None = None

    def _new_client(self):
        if self.client_factory is not None:
            return self.client_factory()
        from instagrapi import Client
        return Client()

    def _prepare(self, job: dict):
        if job["platform"] != "instagram":
            raise PreSendFailure("job is not for Instagram")
        if not frozen_payload_matches(job):
            raise PreSendFailure("queued text or media changed before send")
        entry = self.registry.status("instagram", job["account"])
        if not entry or entry["kind"] != "instagrapi_settings":
            raise AuthUnavailable("Instagram account session is not registered")
        if not entry["session_exists"] or not entry["session_private"]:
            raise AuthUnavailable("Instagram account session is missing or not private")
        try:
            client = self._new_client()
        except ImportError as exc:
            raise AuthUnavailable("Instagram client dependency unavailable") from exc
        session = Path(entry["session_file"])
        try:
            client.load_settings(session)
            info = client.account_info()
        except Exception as exc:
            if type(exc).__name__ not in ("LoginRequired", "ClientLoginRequired"):
                raise AuthUnavailable(f"saved Instagram session could not be validated ({type(exc).__name__})") from exc
            try:
                client = read_with_session(job["account"], lambda connected: connected,
                    registry=self.registry, client_factory=self._new_client,
                    refresh=self.session_refresh)
                info = None  # read_with_session already checked the account identity
            except InstagramSessionError as recovery_error:
                if str(recovery_error) == "account_mismatch":
                    raise PreSendFailure("saved Instagram session belongs to a different account") from None
                raise AuthUnavailable(f"Instagram session recovery unavailable ({recovery_error})") from None
            except Exception as recovery_error:
                raise AuthUnavailable(f"Instagram session recovery unavailable ({type(recovery_error).__name__})") from None
        if info is not None and info.username.casefold() != job["account"].casefold():
            raise PreSendFailure("saved Instagram session belongs to a different account")
        if job["action"] == "reply":
            if not job.get("media_id") or not str(job.get("target_id", "")).isdigit():
                raise PreSendFailure("reply needs media_id and numeric parent comment ID")
        elif job["action"] == "post":
            path = Path(job["media_path"]) if job.get("media_path") else None
            if path is None or (path.suffix.lower() not in (".jpg", ".jpeg", ".mp4")
                                and path.name != "carousel.json"):
                raise PreSendFailure("Instagram post needs a photo, clip, or carousel manifest")
        else:
            raise PreSendFailure("unsupported Instagram action")
        self.client = client
        self.session_path = session
        self.session_mtime_ns = session.stat().st_mtime_ns

    def _single_write_attempt(self) -> None:
        """Disable the installed client's implicit replay of ambiguous POSTs."""
        client = self.client
        if hasattr(client, '_configure_private_session_retry'):
            client.session_retry_total = 0
            client._configure_private_session_retry()
        if not hasattr(client, '_send_private_request') or not hasattr(client, 'private_request'):
            return
        normal_request = client.private_request

        def once(endpoint, data=None, params=None, login=False, with_signature=True,
                 headers=None, extra_sig=None, domain=None):
            if not data:
                return normal_request(endpoint, data=data, params=params, login=login,
                                      with_signature=with_signature, headers=headers,
                                      extra_sig=extra_sig, domain=domain)
            request_headers = dict(headers or {})
            if getattr(client, 'authorization', None) and 'authorization' not in request_headers:
                request_headers['Authorization'] = client.authorization
            client.private_requests_count += 1
            return client._send_private_request(endpoint, data=data, params=params,
                login=login, with_signature=with_signature, headers=request_headers,
                extra_sig=extra_sig, domain=domain)

        client.private_request = once

    def publish(self, job: dict) -> dict:
        snapshot_dir = None
        send_started = False
        try:
            media = None
            if job['action'] == 'post' and job.get('media_path'):
                snapshot_root = STATE_ROOT
                snapshot_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                snapshot_dir = tempfile.TemporaryDirectory(prefix='.ig-send-',
                    dir=snapshot_root)
                original = Path(job['media_path'])
                media = Path(snapshot_dir.name) / ('media' + original.suffix.lower())
                shutil.copyfile(original, media)
                os.chmod(media, 0o600)
                if file_sha256(str(media)) != job.get('media_sha256'):
                    raise PreSendFailure('queued media changed before private snapshot')
                if original.name == 'carousel.json':
                    try:
                        manifest = json.loads(media.read_text(encoding='utf-8'))
                        items = manifest['items']
                        if (manifest.get('kind') != 'instagram_carousel' or
                                manifest.get('version') != 1 or
                                manifest.get('account') != job['account'] or
                                not isinstance(items, list) or not 2 <= len(items) <= 10):
                            raise ValueError('manifest identity or count')
                        slides = []
                        seen = set()
                        for item in items:
                            name = item['filename']
                            digest = item['sha256']
                            if (not isinstance(name, str) or
                                    not re.fullmatch(r'slide-\d{2}\.(?:jpg|jpeg|png|webp)', name) or
                                    name in seen or not isinstance(digest, str) or
                                    not re.fullmatch(r'[0-9a-f]{64}', digest)):
                                raise ValueError('manifest slide')
                            seen.add(name)
                            snapshot = Path(snapshot_dir.name) / name
                            shutil.copyfile(original.parent / name, snapshot)
                            os.chmod(snapshot, 0o600)
                            if file_sha256(str(snapshot)) != digest:
                                raise ValueError('manifest slide changed')
                            slides.append(snapshot)
                        media = slides
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        raise PreSendFailure(f'carousel snapshot invalid ({type(exc).__name__})') from exc
            self._prepare(job)
            self._single_write_attempt()
            client = self.client
            if job["action"] == "reply":
                send_started = True
                try:
                    result = client.media_comment(
                    str(job["media_id"]), job["body"],
                    replied_to_comment_id=int(job["target_id"]),
                    )
                except Exception as exc:
                    raise PublishUncertain(type(exc).__name__) from exc
                provider_id = str(result.pk)
                base = str(job.get("target_url") or "").split("?")[0].rstrip("/")
                permalink = f"{base}/?comment_id={provider_id}" if base else None
                return {"provider_id": provider_id, "permalink": permalink}
            if isinstance(media, list):
                send_started = True
                try:
                    result = client.album_upload(
                        media, caption=job["body"],
                        share_to_threads=bool(job.get("crosspost_threads")),
                    )
                except Exception as exc:
                    raise PublishUncertain(type(exc).__name__) from exc
                surface = "p"
            elif media.suffix.lower() in (".jpg", ".jpeg"):
                send_started = True
                try:
                    result = client.photo_upload(
                        media, caption=job["body"],
                        share_to_threads=bool(job.get("crosspost_threads")),
                    )
                except Exception as exc:
                    raise PublishUncertain(type(exc).__name__) from exc
                surface = "p"
            else:
                send_started = True
                try:
                    result = client.clip_upload(
                        media, caption=job["body"],
                        share_to_threads=bool(job.get("crosspost_threads")),
                    )
                except Exception as exc:
                    raise PublishUncertain(type(exc).__name__) from exc
                surface = "reel"
            provider_id = str(result.pk)
            code = getattr(result, "code", None)
            permalink = f"https://www.instagram.com/{surface}/{code}/" if code else None
            return {"provider_id": provider_id, "permalink": permalink}
        except (PreSendFailure, PublishUncertain):
            raise
        except Exception as exc:
            if send_started:
                raise PublishUncertain(type(exc).__name__) from exc
            raise PreSendFailure(type(exc).__name__) from exc
        finally:
            if snapshot_dir is not None:
                snapshot_dir.cleanup()

    def save_session(self) -> None:
        if self.client is None or self.session_path is None or self.session_mtime_ns is None:
            return
        lock_path = self.session_path.with_name(self.session_path.name + ".lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if self.session_path.stat().st_mtime_ns != self.session_mtime_ns:
                return  # A newer IG/Threads login already rotated this shared session.
            fd, name = tempfile.mkstemp(prefix=".ig-session-", dir=self.session_path.parent)
            os.close(fd)
            try:
                if not self.client.dump_settings(name):
                    raise RuntimeError("Instagram session settings were not saved")
                os.chmod(name, 0o600)
                os.replace(name, self.session_path)
                self.session_mtime_ns = self.session_path.stat().st_mtime_ns
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
