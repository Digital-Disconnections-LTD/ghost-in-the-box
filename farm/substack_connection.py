"""Persistent sessions and compact publication receipts for python-substack.

Publishing and Markdown rendering stay in the upstream SDK. This connector
keeps refreshed cookies across MCP process exits and records successful posts.
"""
from __future__ import annotations

from farm.config import STATE_ROOT



from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


DEFAULT_CONFIG = STATE_ROOT / 'substack/connection.json'


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".substack-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SubstackConnection:
    def __init__(self, config_path: Path | str | None = None, *, api_factory=None):
        self.config_path = Path(config_path or os.environ.get("SOCIAL_FARM_SUBSTACK_CONFIG") or DEFAULT_CONFIG).expanduser()
        self.api_factory = api_factory

    def status(self):
        if not self.config_path.is_file():
            return {"configured": False, "stage": "account_setup"}
        config = json.loads(self.config_path.read_text())
        session_saved = Path(config.get("cookies_path", "")).is_file()
        ready = bool(config.get("ready") and config.get("publication_url")
                     and session_saved)
        stage = "configured" if ready else "publication_setup" if session_saved else "account_setup"
        result = {"configured": ready, "stage": stage, "session_saved": session_saved,
                  "publication_url": config.get("publication_url"),
                  "editorial_phase": config.get("editorial_phase", "draft"),
                  "timezone": config.get("timezone", "America/New_York")}
        receipt = self.config_path.parent / "last-published.json"
        if receipt.is_file():
            result["last_publication"] = json.loads(receipt.read_text())
        return result

    def get_api(self):
        if not self.config_path.is_file():
            raise RuntimeError("Substack setup needs a connection file and a signed-in publication.")
        config = json.loads(self.config_path.read_text())
        publication = config.get("publication_url")
        cookie_file = Path(config.get("cookies_path", ""))
        if not config.get("ready") or not publication or not cookie_file.is_file():
            raise RuntimeError("Substack account setup is pending: complete browser sign-in and select the publication.")
        if urlparse(publication).scheme != "https":
            raise ValueError("Substack publication_url must use HTTPS.")
        factory = self.api_factory
        if factory is None:
            from substack import Api
            factory = Api
        api = factory(cookies_path=str(cookie_file), publication_url=publication, timeout=45)
        original = api._session.request
        receipt_file = self.config_path.parent / "last-published.json"

        def save_cookies():
            private_json(cookie_file, api._session.cookies.get_dict())

        def request(method, url, *args, **kwargs):
            try:
                response = original(method, url, *args, **kwargs)
                match = re.search(r"/drafts/(\d+)/publish/?$", urlparse(url).path)
                if method.upper() == "POST" and match and response.ok:
                    now = datetime.now(timezone.utc)
                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                    if not isinstance(body, dict):
                        body = {}
                    private_json(receipt_file, {
                        "account": config.get("account", "publication"),
                        "publication_url": publication,
                        "draft_id": int(match.group(1)),
                        "url": body.get("canonical_url") or body.get("url"),
                        "published_at": now.isoformat(),
                        "publication_date": now.astimezone(ZoneInfo(config.get("timezone", "America/New_York"))).date().isoformat(),
                    })
                return response
            finally:
                # Requests has applied Set-Cookie by the time request returns,
                # unlike a response hook, which runs before jar extraction.
                save_cookies()

        api._session.request = request
        save_cookies()  # Include any cookies refreshed during SDK initialization.
        return api
