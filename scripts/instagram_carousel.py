#!/usr/bin/env python3
"""Freeze 2–10 image slides and create one Instagram carousel outbox draft."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from farm.config import STATE_ROOT
from farm.outbox import Outbox, file_sha256

ROOT = STATE_ROOT / 'carousels'
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


@contextmanager
def _draft_lock(root: Path):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    descriptor = os.open(root / ".draft.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def create_draft(*, account: str, body: str, images: list[Path],
                 crosspost_threads: bool = False, source_urls: list[str] | None = None,
                 root: Path = ROOT, box_factory=Outbox,
                 idempotency_key: str | None = None) -> dict:
    """Snapshot slides once; repeating a key requires identical caption and bytes."""
    if not 2 <= len(images) <= 10:
        raise ValueError("a carousel needs 2..10 images")
    sources = [Path(p).expanduser().resolve(strict=True) for p in images]
    if any(not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES for p in sources):
        raise ValueError("carousel slides must be JPG, PNG, or WebP files")
    sources_urls = list(dict.fromkeys(source_urls or []))
    with _draft_lock(root):
        if idempotency_key:
            with box_factory() as box:
                existing = box.db.execute("SELECT id FROM jobs WHERE idempotency_key=?",
                                          (idempotency_key,)).fetchone()
                if existing:
                    job = box.get(existing["id"])
                    if (job["platform"] != "instagram" or job["account"] != account or
                            job["action"] != "post" or job["body"] != body or
                            bool(job["crosspost_threads"]) != crosspost_threads or
                            job["source_urls"] != sources_urls or not job["media_path"]):
                        raise ValueError("idempotency key already belongs to different content")
                    manifest = json.loads(Path(job["media_path"]).read_text(encoding="utf-8"))
                    wanted = [{"filename": f"slide-{i:02d}{path.suffix.lower()}",
                               "sha256": file_sha256(str(path))}
                              for i, path in enumerate(sources, 1)]
                    if (manifest.get("kind") != "instagram_carousel" or
                            manifest.get("account") != account or manifest.get("version") != 1 or
                            manifest.get("items") != wanted):
                        raise ValueError("idempotency key already belongs to different slides")
                    return job
        return _snapshot_draft(account=account, body=body, sources=sources,
            crosspost_threads=crosspost_threads, source_urls=sources_urls,
            root=root, box_factory=box_factory, idempotency_key=idempotency_key)


def _snapshot_draft(*, account, body, sources, crosspost_threads, source_urls,
                    root, box_factory, idempotency_key):
    directory = root / str(uuid.uuid4())
    directory.mkdir(mode=0o700)
    try:
        items = []
        for index, source in enumerate(sources, 1):
            filename = f"slide-{index:02d}{source.suffix.lower()}"
            destination = directory / filename
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
            items.append({"filename": filename, "sha256": file_sha256(str(destination))})
        manifest = directory / "carousel.json"
        fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"kind": "instagram_carousel", "version": 1,
                       "account": account, "items": items},
                      handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        with box_factory() as box:
            return box.create_draft(platform="instagram", account=account,
                body=body, action="post", media_path=str(manifest),
                crosspost_threads=crosspost_threads, source_urls=source_urls,
                idempotency_key=idempotency_key)
    except Exception:
        shutil.rmtree(directory)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("draft")
    draft.add_argument("--account", default="example_creator")
    draft.add_argument("--body-file", required=True)
    draft.add_argument("--image", action="append", required=True)
    draft.add_argument("--crosspost-threads", action="store_true")
    draft.add_argument("--source-url", action="append", default=[])
    draft.add_argument("--idempotency-key")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        job = create_draft(account=args.account,
            body=Path(args.body_file).read_text(encoding="utf-8"),
            images=[Path(value) for value in args.image],
            crosspost_threads=args.crosspost_threads,
            source_urls=args.source_url, idempotency_key=args.idempotency_key)
        print(json.dumps({"ok": True, "job_id": job["id"],
                          "status": job["status"], "slides": len(args.image)}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
