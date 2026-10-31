#!/usr/bin/env python3
"""Build a private visual reference shelf from scored public Lemmy posts.

The manifest is reproducible source research. Downloaded third-party images
stay outside the git checkout and are references for original carousel work.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import requests


SOURCES = (
    ("https://programming.dev", "programmer_humor", 240, False),
    ("https://lemmy.world", "linuxmemes", 180, False),
    ("https://lemmy.world", "programmerhumor", 40, False),
    ("https://lemmy.world", "microblogmemes", 25, True),
)
TECH_TERMS = re.compile(
    r"\b(ai|algorithm|app|apple|browser|cloud|code|computer|data|digital|"
    r"facebook|google|internet|linux|microsoft|model|openai|phone|privacy|"
    r"program|reddit|software|subscription|surveillance|tech|twitter|windows)\b",
    re.I,
)
SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
USER_AGENT = "Batbox visual research/1.0 (+https://digitaldisconnections.com/)"
MAX_BYTES = 8_000_000


def image_suffix(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    suffix = Path(parsed.path).suffix.lower()
    return suffix if suffix in SUFFIXES else None


def fetch_candidates(session: requests.Session) -> list[dict]:
    candidates, seen = [], set()
    for host, community, quota, need_term in SOURCES:
        source = []
        for page in range(1, 11):
            response = session.get(
                host + "/api/v3/post/list",
                params={"community_name": community, "sort": "TopAll", "limit": 50, "page": page},
                timeout=20,
            )
            response.raise_for_status()
            rows = response.json().get("posts", [])
            if not rows:
                break
            for rank, item in enumerate(rows, (page - 1) * 50 + 1):
                post, counts = item["post"], item["counts"]
                image_url = post.get("url") or ""
                title = post.get("name") or ""
                if (post.get("removed") or post.get("deleted") or post.get("nsfw")
                        or not image_suffix(image_url) or (need_term and not TECH_TERMS.search(title))):
                    continue
                key = image_url.split("?", 1)[0]
                if key in seen:
                    continue
                seen.add(key)
                source.append({
                    "post_id": post["id"], "post_url": post.get("ap_id") or f"{host}/post/{post['id']}",
                    "index_url": f"{host}/post/{post['id']}", "image_url": image_url,
                    "thumbnail_url": post.get("thumbnail_url"), "title": title,
                    "community": item["community"]["actor_id"],
                    "creator": item["creator"].get("actor_id"),
                    "published": post.get("published"), "score": counts.get("score", 0),
                    "upvotes": counts.get("upvotes", 0), "comments": counts.get("comments", 0),
                    "source_rank": rank, "reuse_status": "reference_only_rights_unverified",
                })
            if len(source) >= quota:
                break
        source.sort(key=lambda row: (row["score"], row["comments"]), reverse=True)
        candidates.extend(source[:quota])
    return candidates


def image_kind(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def download(row: dict, image_dir: Path) -> dict | None:
    try:
        with requests.get(row["image_url"], timeout=25, stream=True,
                          headers={"User-Agent": USER_AGENT}) as response:
            response.raise_for_status()
            parts, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    return None
                parts.append(chunk)
        body = b"".join(parts)
        suffix = image_kind(body)
        if not suffix or len(body) < 1024:
            return None
        digest = hashlib.sha256(body).hexdigest()
        path = image_dir / (digest + suffix)
        if not path.exists():
            path.write_bytes(body)
        return {**row, "sha256": digest, "bytes": len(body),
                "local_path": "images/" + path.name}
    except (requests.RequestException, OSError, ValueError):
        return None


def write_gallery(rows: list[dict], path: Path) -> None:
    cards = []
    for row in rows:
        cards.append(
            '<article><a href="' + html.escape(row["post_url"], quote=True) + '">'
            '<img loading="lazy" src="' + html.escape(row["local_path"], quote=True) + '"></a>'
            '<p><strong>' + html.escape(row["title"]) + '</strong></p>'
            '<small>' + str(row["score"]) + ' score · ' + str(row["comments"]) +
            ' comments · ' + html.escape(row["community"]) + '</small>' +
            ('<details><summary>Words in image (OCR)</summary><p>' +
             html.escape(row.get("ocr_text", "")) + '</p></details>'
             if row.get("ocr_text") else "") + '</article>'
        )
    path.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Tech meme references</title>'
        '<style>body{font:16px system-ui;background:#111;color:#eee;margin:2rem}'
        'main{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:1rem}'
        'article{background:#222;padding:1rem;border-radius:8px}img{width:100%;height:240px;'
        'object-fit:contain;background:#000}a{color:#9cf}small{color:#aaa}</style>'
        '<h1>Technology meme reference shelf</h1><p>Scored public examples for studying formats and'
        ' audience response. Credit and permissions need review before public reuse.</p><main>' +
        "".join(cards) + '</main></html>', encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path.home() / ".batbox/media-reference/tech-memes")
    parser.add_argument("--limit", type=int, default=300)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    image_dir = args.out / "images"
    image_dir.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    candidates = fetch_candidates(session)
    rows, seen_hashes = [], set()
    manifest = args.out / "manifest.jsonl"
    cached = {}
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            prior = json.loads(line)
            if (args.out / prior["local_path"]).is_file():
                cached[prior["image_url"]] = prior
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(download, row, image_dir): row for row in candidates
                   if row["image_url"] not in cached}
        for row in candidates:
            prior = cached.get(row["image_url"])
            if prior and prior["sha256"] not in seen_hashes:
                seen_hashes.add(prior["sha256"])
                rows.append({**row, "sha256": prior["sha256"], "bytes": prior["bytes"],
                             "local_path": prior["local_path"],
                             "ocr_text": prior.get("ocr_text", "")})
        for future in as_completed(futures):
            row = future.result()
            if row and row["sha256"] not in seen_hashes:
                seen_hashes.add(row["sha256"])
                rows.append(row)
    rows.sort(key=lambda row: (row["score"], row["comments"]), reverse=True)
    rows = rows[:args.limit]
    manifest.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")
    write_gallery(rows, args.out / "index.html")
    report = {"collected_at": datetime.now(timezone.utc).isoformat(),
              "candidates": len(candidates), "images": len(rows),
              "sources": {source[1]: sum(source[1] in row["community"] for row in rows)
                          for source in SOURCES}, "manifest": str(manifest)}
    (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
