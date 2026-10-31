"""Private Instagram research records and bounded downloads, separate from publishing."""

from __future__ import annotations

from farm.config import STATE_ROOT


from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit


DEFAULT_ROOT = STATE_ROOT / 'instagram-references'
MAX_IMAGE_BYTES = 8_000_000  # Also accepted by the existing local vision helper.
MAX_BATCH_BYTES = 64_000_000
MAX_DOWNLOAD_SECONDS = 90
STATES = ("new", "selected", "skipped")


class ReferenceError(ValueError):
    """Safe fixed local failure text; never contains provider responses."""


def identifier(value):
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,40}", value):
        raise ReferenceError("invalid provider identifier")
    return value


def canonical_post_url(value):
    """Only canonical Instagram post/reel URLs; no redirect or arbitrary URL fetch."""
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname in
                 ("instagram.com", "www.instagram.com", "m.instagram.com") and
                 not parsed.username and not parsed.password and parsed.port in (None, 443))
    except ValueError:
        valid = False
    match = re.fullmatch(r"/(p|reel|tv)/([A-Za-z0-9_-]{1,100})/?", parsed.path) if valid else None
    if not match:
        raise ReferenceError("use a canonical HTTPS Instagram /p/, /reel/ or /tv/ post URL")
    return f"https://www.instagram.com/{match[1]}/{match[2]}/"


def _image_suffix(prefix):
    if prefix.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return ".webp"
    raise ReferenceError("download did not contain a JPEG, PNG or WebP image")


def _cdn_url(value):
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        valid = (parsed.scheme == "https" and not parsed.username and not parsed.password and
                 parsed.port in (None, 443) and any(host == base or host.endswith("." + base)
                    for base in ("cdninstagram.com", "fbcdn.net")))
    except ValueError:
        valid = False
    if not valid:
        raise ReferenceError("Instagram did not return a supported image CDN URL")
    return value


def fetch_image(url, destination, byte_limit, *, deadline=None, session_factory=None):
    """Stream only provider CDN images; no cookies, redirects or auth are forwarded."""
    url = _cdn_url(url)
    if session_factory is None:
        import requests
        session_factory = requests.Session
    limit = min(byte_limit, MAX_IMAGE_BYTES)
    if limit < 1:
        raise ReferenceError("download byte budget exhausted")
    deadline = deadline or time.monotonic() + MAX_DOWNLOAD_SECONDS
    client = session_factory()
    client.trust_env = False
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReferenceError("download time budget exhausted")
        with client.get(url, stream=True, allow_redirects=False,
                        timeout=(min(10, remaining), min(25, remaining))) as response:
            if response.status_code != 200:
                raise ReferenceError("image CDN did not return a successful image")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared = int(declared)
                except (TypeError, ValueError):
                    raise ReferenceError("invalid image content length") from None
                if declared < 1 or declared > limit:
                    raise ReferenceError("image exceeds the remaining byte budget")
            size = 0
            digest = hashlib.sha256()
            prefix = b""
            with open(destination, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if time.monotonic() >= deadline:
                        raise ReferenceError("download time budget exhausted")
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        raise ReferenceError("image exceeds the remaining byte budget")
                    prefix = (prefix + chunk)[:16]
                    digest.update(chunk)
                    stream.write(chunk)
                if not size or (declared is not None and size != declared):
                    raise ReferenceError("image download was incomplete")
                suffix = _image_suffix(prefix)
                stream.flush()
                os.fsync(stream.fileno())
            return {"size_bytes": size, "sha256": digest.hexdigest(), "suffix": suffix}
    except Exception:
        Path(destination).unlink(missing_ok=True)
        raise
    finally:
        client.close()


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


class ReferenceStore:
    def __init__(self, account, root=DEFAULT_ROOT):
        if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", account):
            raise ReferenceError("invalid reference account")
        self.root = Path(root).expanduser().resolve() / account
        if self.root.is_symlink():
            raise ReferenceError("reference account directory must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.downloads = self.root / "downloads"
        if self.downloads.is_symlink():
            raise ReferenceError("download root must not be a symlink")
        self.downloads.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.downloads, 0o700)
        path = self.root / "references.sqlite3"
        if path.is_symlink():
            raise ReferenceError("reference database must not be a symlink")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
                media_pk TEXT PRIMARY KEY, record TEXT NOT NULL,
                author TEXT NOT NULL, caption TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'new', note TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS discoveries (
                media_pk TEXT NOT NULL, hashtag TEXT NOT NULL, mode TEXT NOT NULL,
                PRIMARY KEY(media_pk,hashtag,mode));
            CREATE TABLE IF NOT EXISTS images (
                media_pk TEXT NOT NULL, slide_index INTEGER NOT NULL, slide_id TEXT NOT NULL,
                kind TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                downloaded_at TEXT NOT NULL, PRIMARY KEY(media_pk,slide_index));
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.db.close()

    def remember(self, record, *, hashtag=None, mode=None):
        pk = identifier(record["provider_id"])
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute("""INSERT INTO posts(media_pk,record,author,caption,first_seen,last_seen)
                VALUES (?,?,?,?,?,?) ON CONFLICT(media_pk) DO UPDATE SET record=excluded.record,
                author=excluded.author,caption=excluded.caption,last_seen=excluded.last_seen""",
                (pk, json.dumps(record, ensure_ascii=False), record["author"], record["caption"], now, now))
            if hashtag:
                self.db.execute("INSERT OR IGNORE INTO discoveries VALUES (?,?,?)", (pk, hashtag, mode))
        return self.get(pk)

    def get(self, media_pk):
        pk = identifier(media_pk)
        row = self.db.execute("SELECT * FROM posts WHERE media_pk=?", (pk,)).fetchone()
        if row is None:
            raise ReferenceError("post has not been saved in the reference store")
        images = [dict(item) for item in self.db.execute(
            "SELECT * FROM images WHERE media_pk=? ORDER BY slide_index", (pk,))]
        sources = [dict(item) for item in self.db.execute(
            "SELECT hashtag,mode FROM discoveries WHERE media_pk=? ORDER BY hashtag,mode", (pk,))]
        return {**json.loads(row["record"]), "state": row["state"], "note": row["note"],
                "first_seen": row["first_seen"], "last_seen": row["last_seen"],
                "discovered_via": sources, "downloads": images,
                "image_paths": [item["path"] for item in images]}

    def list(self, *, state="new", hashtag=None, query=None, limit=20, offset=0):
        if state not in (*STATES, "all") or not 1 <= limit <= 50 or not 0 <= offset <= 10000:
            raise ReferenceError("invalid reference list bounds")
        where, params = [], []
        if state != "all":
            where.append("state=?")
            params.append(state)
        if hashtag:
            where.append("media_pk IN (SELECT media_pk FROM discoveries WHERE hashtag=?)")
            params.append(hashtag)
        if query:
            where.append("(instr(lower(caption),lower(?))>0 OR instr(lower(author),lower(?))>0)")
            params.extend([query, query])
        clause = " WHERE " + " AND ".join(where) if where else ""
        total = self.db.execute("SELECT COUNT(*) FROM posts" + clause, params).fetchone()[0]
        rows = self.db.execute("SELECT media_pk FROM posts" + clause +
            " ORDER BY last_seen DESC,media_pk DESC LIMIT ? OFFSET ?", (*params, limit, offset))
        result = [self.get(row["media_pk"]) for row in rows]
        return {"posts": result, "total": total,
                "next_offset": offset + len(result) if offset + len(result) < total else None}

    def curate(self, media_pk, state, note):
        if state not in STATES:
            raise ReferenceError("invalid curation state")
        pk = identifier(media_pk)
        with self.db:
            changed = self.db.execute("UPDATE posts SET state=?,note=? WHERE media_pk=?", (state, note, pk))
            if changed.rowcount != 1:
                raise ReferenceError("post has not been saved in the reference store")
        return self.get(pk)

    @contextmanager
    def _download_lock(self, pk):
        directory = self.downloads / pk
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise ReferenceError("download directory must not be a symlink")
        os.chmod(directory, 0o700)
        fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield directory
        finally:
            os.close(fd)

    def verified_image(self, path):
        original = Path(path)
        if not original.is_absolute() or original.is_symlink():
            raise ReferenceError("use an absolute downloaded slide path")
        path = original.resolve(strict=True)
        if not path.is_relative_to(self.downloads) or not path.is_file():
            raise ReferenceError("image must be in this account's private Instagram downloads")
        row = self.db.execute("SELECT * FROM images WHERE path=?", (str(path),)).fetchone()
        if (row is None or path.stat().st_size != row["size_bytes"] or
                path.stat().st_size > MAX_IMAGE_BYTES or _digest(path) != row["sha256"]):
            raise ReferenceError("downloaded image is missing, changed or unregistered")
        return dict(row)

    def _manifest(self, directory, pk):
        fd, temporary = tempfile.mkstemp(prefix=".metadata-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.get(pk), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / "metadata.json")
        finally:
            Path(temporary).unlink(missing_ok=True)

    def download(self, record, *, max_images=10, start_index=1, fetcher=fetch_image):
        """Keep each complete image durable; failures return explicit partial progress."""
        if not 1 <= max_images <= 20 or not 1 <= start_index <= 100:
            raise ReferenceError("invalid image download bounds")
        pk = identifier(record["provider_id"])
        self.remember(record)
        resources = record["slides"] if record["media_type"] == 8 else [{
            "provider_id": pk, "media_type": record["media_type"], "image_url": record["image_url"]}]
        if not resources or len(resources) > 100 or start_index > len(resources):
            raise ReferenceError("media has no images at the requested start index")
        selected = resources[start_index - 1:start_index - 1 + max_images]
        downloaded, failure, used = [], None, 0
        deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
        with self._download_lock(pk) as directory:
            for index, slide in enumerate(selected, start_index):
                temporary = None
                try:
                    slide_id = identifier(slide["provider_id"])
                    prior = self.db.execute("SELECT * FROM images WHERE media_pk=? AND slide_index=?",
                                            (pk, index)).fetchone()
                    if prior and prior["slide_id"] == slide_id:
                        try:
                            verified = self.verified_image(prior["path"])
                            downloaded.append({**verified, "cached": True})
                            continue
                        except (OSError, ReferenceError):
                            pass
                    if time.monotonic() >= deadline:
                        raise ReferenceError("download time budget exhausted")
                    if not slide.get("image_url"):
                        raise ReferenceError("slide has no downloadable preview image")
                    fd, temporary = tempfile.mkstemp(prefix=".image-", dir=directory)
                    os.close(fd)
                    receipt = fetcher(slide["image_url"], Path(temporary),
                                      min(MAX_IMAGE_BYTES, MAX_BATCH_BYTES - used), deadline=deadline)
                    size = Path(temporary).stat().st_size
                    if size != receipt["size_bytes"] or size > min(MAX_IMAGE_BYTES, MAX_BATCH_BYTES - used):
                        raise ReferenceError("image download receipt violates byte bounds")
                    with open(temporary, "rb") as stream:
                        suffix = _image_suffix(stream.read(16))
                    destination = directory / f"slide-{index:02d}-{slide_id}{suffix}"
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, destination)
                    receipt = {"media_pk": pk, "slide_index": index, "slide_id": slide_id,
                        "kind": "image" if slide["media_type"] == 1 else "video_preview",
                        "path": str(destination), "sha256": _digest(destination), "size_bytes": size,
                        "downloaded_at": datetime.now(timezone.utc).isoformat()}
                    with self.db:
                        self.db.execute("""INSERT INTO images VALUES (?,?,?,?,?,?,?,?)
                            ON CONFLICT(media_pk,slide_index) DO UPDATE SET slide_id=excluded.slide_id,
                            kind=excluded.kind,path=excluded.path,sha256=excluded.sha256,
                            size_bytes=excluded.size_bytes,downloaded_at=excluded.downloaded_at""",
                            tuple(receipt[key] for key in ("media_pk", "slide_index", "slide_id", "kind",
                                "path", "sha256", "size_bytes", "downloaded_at")))
                    used += size
                    downloaded.append({**receipt, "cached": False})
                except Exception as error:
                    failure = {"slide_index": index, "error": str(error) if isinstance(error, ReferenceError)
                               else "image download unavailable", "error_type": type(error).__name__}
                    break
                finally:
                    if temporary:
                        Path(temporary).unlink(missing_ok=True)
            self._manifest(directory, pk)
            retained = {row["slide_index"]: row for row in self.get(pk)["downloads"]}
            complete = all(index in retained and retained[index]["slide_id"] == str(slide["provider_id"])
                           and Path(retained[index]["path"]).is_file()
                           for index, slide in enumerate(resources, 1))
        next_index = start_index + len(downloaded)
        return {"ok": failure is None, "provider_id": pk, "media_id": record["media_id"],
                "source_url": record["url"], "author": record["author"], "author_id": record["author_id"],
                "caption": record["caption"], "directory": str(directory),
                "metadata_path": str(directory / "metadata.json"), "total_slides": len(resources),
                "downloads": downloaded, "image_paths": [item["path"] for item in downloaded],
                "downloaded_bytes": used, "complete": failure is None and complete,
                "next_start_index": next_index if next_index <= len(resources) else None,
                "failure": failure}
