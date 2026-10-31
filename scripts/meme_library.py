#!/usr/bin/env python3
"""Collect, search, categorize, and prepare the private technology meme library.

python3 scripts/meme_library.py collect --count 250
python3 scripts/meme_library.py search --category ai --limit 12
python3 scripts/meme_library.py prepare-carousel ID1 ID2 --out /tmp/meme-carousel
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path.home() / ".batbox/media-reference/tech-memes"
RUNTIME = Path(sys.executable)
USER_AGENT = "Batbox meme library/2.0 (+https://digitaldisconnections.com/)"
MAX_BYTES = 12_000_000
SOURCE_FIELDS = (
    "post_id", "post_url", "index_url", "image_url", "thumbnail_url", "title",
    "community", "creator", "published", "score", "upvotes", "comments",
    "source_rank", "source_sort", "source_query", "source_api_url", "engagement_observed_at",
    "platform", "likes", "score_kind", "caption", "imported_from",
)
CATEGORY_PATTERNS = {
    "ai": r"\b(ai|a\.i\.|chatgpt|gpt\d*|gpt-\d+|llms?|openai|claude|anthropic|copilot|co-pilot|deepseek|gemini|ollama|prompt[ -](?:engineer\w*|injection)|system prompts?|hallucinat\w*|vibe[ -]?cod\w*|machine learning|neural networks?|artificial intelligence)\b",
    "software-dev": r"\b(programm\w*|cod(?:e|er|ers|es|ing)|developer\w*|software|javascript|typescript|python|rust|java|golang|compiler\w*|function\w*|variables?|recursion|algorithms?|sql|database\w*|git|github|stack overflow|code review|pull requests?|merge|frontend|backend|regex|html|css|api|null|undefined|c\+\+)\b",
    "debugging": r"\b(bugs?|debug\w*|tests?|testing|errors?|exception\w*|stack trace|segfault|crash\w*|works on my machine|fix(?:es|ing)?|broken|troubleshoot\w*|undefined)\b",
    "devops": r"\b(devops|deploy\w*|production|docker|kubernetes|k8s|containers?|servers?|sysadmin|uptime|outages?|ci/cd|aws|cloud|on[ -]?call|infrastructure|dns|backups?)\b",
    "ownership-privacy": r"\b(privacy|private (?:keys?|data|cloud|repos?|servers?)|surveill\w*|tracking|telemetry|cookies?|consent|ownership|owning|you own|subscriptions?|subscribe|rent(?:ing|al)?|drm|piracy|pirat\w*|licens\w*|right to repair|data broker\w*|personal data|selling your data|paywall\w*|perpetual)\b",
    "open-source-linux": r"\b(linux|unix|ubuntu|debian|arch|fedora|gentoo|nixos|gnu|foss|open[ -]?source|libre|kde|gnome|systemd|sudo|bash|terminal|distro\w*|wayland)\b",
    "work-career": r"\b(boss|managers?|management|meetings?|standup|jira|agile|scrum|sprints?|deadlines?|hiring|interviews?|resume|salary|salaries|layoffs?|coworker\w*|co-worker\w*|junior|senior|interns?|internship|corporate|recruiter\w*)\b",
    "hardware": r"\b(hardware|gpu|cpu|ram|nvidia|amd|intel|graphics cards?|motherboard|ssd|hard drives?|raspberry pi|printers?|keyboards?|usb)\b",
    "platforms-enshittification": r"\b(enshittification|advertis\w*|ads|paywall\w*|subscriptions?|big tech|microsoft|google|facebook|meta|amazon|apple|reddit|twitter|tiktok|social media|bloatware)\b",
    "general-tech": r"\b(technology|tech|internet|computer\w*|browser\w*|windows|macos|android|ios|phones?|smartphones?|digital|websites?)\b",
}
PATTERNS = {name: re.compile(pattern, re.I) for name, pattern in CATEGORY_PATTERNS.items()}
PRIMARY_ORDER = ("ai", "ownership-privacy", "software-dev", "devops", "open-source-linux",
                 "platforms-enshittification", "hardware", "work-career", "debugging", "general-tech", "review-needed")
MEME_COMMUNITIES = {"programmer_humor", "programmerhumor", "tech_memes", "linuxmemes",
                    "microblogmemes", "memes", "humor", "programminghumor", "aigeneratedmemes"}
DEFAULT_SOURCES = {
    "communities": [
        {"host": "https://programming.dev", "name": "programmer_humor"},
        {"host": "https://lemmy.world", "name": "programmerhumor"},
        {"host": "https://lemmy.world", "name": "tech_memes"},
        {"host": "https://lemmy.world", "name": "linuxmemes"},
        {"host": "https://lemmy.world", "name": "microblogmemes", "require_tech_term": True},
    ],
    "search_host": "https://lemmy.world",
    "queries": ["AI", "ChatGPT", "vibe coding", "privacy", "subscription", "ownership", "open source"],
    "sorts": ["TopMonth", "TopYear", "TopAll"],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".meme-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


@contextmanager
def library_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".library.lock").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def load_rows(root: Path) -> list[dict]:
    manifest = root / "manifest.jsonl"
    if not manifest.exists():
        return []
    return [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line]


def source_record(row: dict) -> dict:
    return {key: row[key] for key in SOURCE_FIELDS if key in row}


def add_source(row: dict, candidate: dict) -> None:
    """Retain provenance for exact reposts and refresh an existing post's counts."""
    sources = {s["post_url"]: s for s in row.get("sources", [source_record(row)])}
    sources[candidate["post_url"]] = source_record(candidate)
    row["sources"] = list(sources.values())
    best = max(row["sources"], key=lambda item: (item.get("score", 0), item.get("comments", 0)))
    row.update(best)


def text_for(row: dict) -> str:
    return "\n".join(str(row.get(key) or "") for key in ("title", "body", "ocr_text"))


def categories_for(row: dict) -> list[str]:
    text = text_for(row)
    result = {name for name, pattern in PATTERNS.items() if pattern.search(text)}
    # Tiny isolated OCR fragments often misread artwork as "AI". Production
    # notes about an AI-generated template do not describe the meme's subject.
    ocr = re.sub(r"(?im)^\W*a[iIlL]\W*$", "", row.get("ocr_text") or "")
    ocr = re.sub(r"\bOpenAl\b", "OpenAI", ocr)
    ocr = re.sub(r"\bAl\b", "AI", ocr)
    body = re.sub(r"[^.!?\n]*(?:template|image|meme)[^.!?\n]*AI[ -]generated[^.!?\n]*[.!?]?", "",
                  row.get("body") or "", flags=re.I)
    ai_text = "\n".join((row.get("title") or "", body, ocr))
    if PATTERNS["ai"].search(ai_text):
        result.add("ai")
    else:
        result.discard("ai")
    community = row.get("community", "").rsplit("/", 1)[-1].lower()
    if community in {"programmer_humor", "programmerhumor", "programminghumor"}:
        result.add("software-dev")
    if community == "linuxmemes":
        result.add("open-source-linux")
    if not result:
        result.add("general-tech" if community == "tech_memes" else "review-needed")
    return [name for name in PRIMARY_ORDER if name in result]


def image_url_ok(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return False
    if Path(parsed.path).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
        return True
    # Lemmy image_proxy URLs can lack a suffix while wrapping a real image URL.
    wrapped = parse_qs(parsed.query).get("url", [])
    return parsed.path.endswith("/image_proxy") and bool(wrapped) and image_url_ok(wrapped[0])


def make_candidate(item: dict, host: str, sort: str, query: str | None, api_url: str,
                   rank: int, min_score: int, need_term: bool = False) -> dict | None:
    post, counts, community = item["post"], item["counts"], item["community"]
    image_url = post.get("url") or ""
    if (post.get("removed") or post.get("deleted") or post.get("nsfw") or
            community.get("nsfw") or not image_url_ok(image_url) or counts.get("score", 0) < min_score):
        return None
    row = {
        "post_id": post["id"], "post_url": post.get("ap_id") or f"{host}/post/{post['id']}",
        "index_url": f"{host}/post/{post['id']}", "image_url": image_url,
        "thumbnail_url": post.get("thumbnail_url"), "title": post.get("name", ""),
        "body": (post.get("body") or "")[:6000], "community": community["actor_id"],
        "creator": item.get("creator", {}).get("actor_id"), "published": post.get("published"),
        "score": counts.get("score", 0), "upvotes": counts.get("upvotes", 0),
        "comments": counts.get("comments", 0), "source_rank": rank, "source_sort": sort,
        "source_query": query, "source_api_url": api_url, "engagement_observed_at": now(),
        "reuse_status": "reference_only_rights_unverified",
    }
    if need_term and not any(pattern.search(text_for(row)) for pattern in PATTERNS.values()):
        return None
    return row


def fetch_stream(task: dict, pages: int, min_score: int) -> tuple[list[dict], list[dict]]:
    import requests
    rows, errors = [], []
    host, sort, query = task["host"], task["sort"], task.get("query")
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    for page in range(1, pages + 1):
        endpoint = "/api/v3/search" if query is not None else "/api/v3/post/list"
        params = {"sort": sort, "limit": 50, "page": page}
        if query is not None:
            params.update(q=query, type_="Posts")
        else:
            params["community_name"] = task["name"]
        api_url = host + endpoint + "?" + urlencode(params)
        try:
            response = session.get(api_url, timeout=25)
            response.raise_for_status()
            items = response.json().get("posts", [])
            if not items:
                break
            for rank, item in enumerate(items, 50 * (page - 1) + 1):
                if query is not None and item["community"]["name"].lower() not in MEME_COMMUNITIES:
                    continue
                row = make_candidate(item, host, sort, query, api_url, rank, min_score,
                                     need_term=query is not None or task.get("require_tech_term", False))
                if row:
                    rows.append(row)
            time.sleep(0.1)
        except (requests.RequestException, ValueError, KeyError) as error:
            errors.append({"url": api_url, "error": str(error)[:250]})
            break
    session.close()
    return rows, errors


def fetch_candidates(config: dict, pages: int, min_score: int,
                     queries: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    tasks = []
    if queries is None:
        tasks.extend({**community, "sort": sort} for community in config["communities"]
                     for sort in config["sorts"])
    tasks.extend({"host": config["search_host"], "query": query, "sort": sort}
                 for query in (queries if queries is not None else config["queries"])
                 for sort in ("TopYear", "TopAll"))
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for batch, failures in pool.map(lambda task: fetch_stream(task, pages, min_score), tasks):
            rows.extend(batch)
            errors.extend(failures)
    # Deduplicate repeated discovery while retaining the individual source post.
    unique = {row["post_url"]: row for row in rows}
    def priority(row):
        cats = categories_for(row)
        group = 0 if "ai" in cats else 1 if "software-dev" in cats else 2 if "ownership-privacy" in cats else 3
        return group, -row["score"], -row["comments"]
    return sorted(unique.values(), key=priority), errors


def inspect_image(body: bytes) -> dict:
    from PIL import Image
    with Image.open(io.BytesIO(body)) as picture:
        if picture.width * picture.height > 40_000_000 or min(picture.size) < 120:
            raise ValueError("image too large or too small")
        formats = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "AVIF": ".avif"}
        suffix = formats.get(picture.format)
        if suffix is None:
            raise ValueError("unsupported image format")
        picture.load()
        rgb = picture.convert("RGB")
        digest = hashlib.sha256(str(rgb.size).encode() + rgb.tobytes()).hexdigest()
        return {"width": picture.width, "height": picture.height, "suffix": suffix,
                "pixel_sha256": digest, "animated": getattr(picture, "is_animated", False)}


def download(candidate: dict, root: Path) -> tuple[dict | None, str | None]:
    import requests
    try:
        with requests.get(candidate["image_url"], headers={"User-Agent": USER_AGENT},
                          timeout=25, stream=True) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("image exceeds 12 MB")
                chunks.append(chunk)
        body = b"".join(chunks)
        metadata = inspect_image(body)
        digest = hashlib.sha256(body).hexdigest()
        path = root / "images" / (digest + metadata.pop("suffix"))
        was_cached = path.is_file()
        if not was_cached:
            # Content-addressed paths make a repeated identical download harmless.
            path.write_bytes(body)
        return {**candidate, **metadata, "sha256": digest, "bytes": len(body),
                "local_path": str(path.relative_to(root)), "downloaded_at": now(),
                "_new_file": not was_cached}, None
    except Exception as error:
        return None, str(error)[:250]


def ocr_image(path: Path) -> str:
    if not shutil.which("tesseract"):
        return ""
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    try:
        # Pillow supplies a PNG to support WebP/AVIF and oriented originals alike.
        from PIL import Image, ImageOps
        with Image.open(path) as picture:
            picture = ImageOps.exif_transpose(picture).convert("RGB")
            picture.thumbnail((2200, 2600))
            blob = io.BytesIO()
            picture.save(blob, "PNG")
        result = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "11"],
                                input=blob.getvalue(), capture_output=True, timeout=25, env=env)
        text = result.stdout.decode("utf-8", "replace") if result.returncode == 0 else ""
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()[:3000]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


def enrich(rows: list[dict], root: Path, *, redo_ocr: bool = False) -> None:
    pending = [row for row in rows if redo_ocr or "ocr_text" not in row]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for row, text in zip(pending, pool.map(lambda item: ocr_image(root / item["local_path"]), pending)):
            row["ocr_text"] = text
            row["ocr_observed_at"] = now()
    for row in rows:
        if not row.get("pixel_sha256") and (root / row["local_path"]).is_file():
            metadata = inspect_image((root / row["local_path"]).read_bytes())
            metadata.pop("suffix")
            row.update(metadata)
        row["id"] = row["sha256"][:12]
        row["categories"] = categories_for(row)
        row["primary_category"] = row["categories"][0]
        row.setdefault("sources", [source_record(row)])


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:75] or "meme"


def write_gallery(rows: list[dict], path: Path, counts: dict,
                  title: str = "Tech meme library", home_href: str | None = None) -> None:
    cards = []
    escape = lambda value: html.escape(str(value), quote=True)
    for row in rows:
        search = " ".join([row["id"], row["title"], row.get("ocr_text", ""), " ".join(row["categories"])]).lower()
        metric = "likes" if row.get("score_kind") == "likes" else "score"
        engagement = ("engagement unavailable" if row.get("score_kind") == "unavailable" else
                      f'{row.get("score", 0)} {metric} · {row.get("comments", 0)} comments')
        cards.append(
            f'<article data-search="{escape(search)}" data-categories="{escape(" ".join(row["categories"]))}" data-score="{row.get("score", 0)}">'
            f'<a href="{escape(row["local_path"])}"><img loading="lazy" src="{escape(row["local_path"])}" alt="{escape(row["title"])}"></a>'
            f'<p><strong>{escape(row["title"])}</strong></p><p>{escape(", ".join(row["categories"]))}</p>'
            f'<p><a href="{escape(row["post_url"])}">{escape(engagement)} · source</a></p>'
            f'<code>{row["id"]}</code><details><summary>Image text and file</summary><p>{escape(row.get("ocr_text", ""))}</p>'
            f'<small>{escape(row["local_path"])}</small></details></article>'
        )
    options = '<option value="">All topics</option>' + "".join(
        f'<option value="{name}">{name} ({counts[name]})</option>' for name in PRIMARY_ORDER)
    home = f'<a href="{escape(home_href)}">All topics</a>' if home_href else ""
    atomic_write(path, '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(title)}</title><style>body{{font:16px system-ui;background:#14161a;color:#eee;margin:24px}}header{{position:sticky;top:0;background:#14161a;padding:12px 0;z-index:2}}'
        'input,select{font:inherit;padding:9px;margin:4px;max-width:90%}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px}'
        'article{background:#23272d;padding:14px;border-radius:8px}article[hidden]{display:none}img{width:100%;height:300px;object-fit:contain;background:#08090b}'
        'a{color:#9cd7ff}small{overflow-wrap:anywhere}code{color:#ffd69c}details{margin-top:10px}</style>'
        f'<header>{home}<h1>{escape(title)} · {len(rows)} images</h1><input id="query" type="search" placeholder="Search titles, image text, IDs">'
        f'<select id="category">{options}</select><label>Minimum score <input id="score" type="number" min="0" value="0" style="width:80px"></label><span id="count"></span></header>'
        '<p>Open an image for its original. Topic folders use links to the same cached files. Source links and engagement travel with prepared carousels.</p><main>' + "".join(cards) +
        '</main><script>const q=document.querySelector("#query"),c=document.querySelector("#category"),s=document.querySelector("#score"),cards=[...document.querySelectorAll("article")];'
        'function filter(){let n=0;for(const card of cards){const ok=q.value.toLowerCase().split(/\\s+/).every(t=>card.dataset.search.includes(t))&&(!c.value||card.dataset.categories.split(" ").includes(c.value))&&Number(card.dataset.score)>=Number(s.value);card.hidden=!ok;if(ok)n++}document.querySelector("#count").textContent=` ${n} visible`}'
        '[q,c,s].forEach(e=>e.addEventListener("input",filter));filter();</script></html>')


def write_indexes(rows: list[dict], root: Path) -> dict:
    rows.sort(key=lambda row: (row.get("score", 0), row.get("comments", 0)), reverse=True)
    counts = {name: 0 for name in PRIMARY_ORDER}
    primary = {name: 0 for name in PRIMARY_ORDER}
    category_rows = {name: [] for name in PRIMARY_ORDER}
    for row in rows:
        primary[row["primary_category"]] += 1
        for name in row["categories"]:
            counts[name] += 1
            category_rows[name].append(row)
    for name, members in category_rows.items():
        folder = root / "categories" / name
        folder.mkdir(parents=True, exist_ok=True)
        wanted = {}
        for row in members:
            filename = f'{slug(row["title"])}--{row["id"]}{Path(row["local_path"]).suffix}'
            wanted[filename] = Path("../..") / row["local_path"]
        # Never delete regular files a user may have placed in a topic folder.
        for old in folder.iterdir():
            if old.is_symlink() and old.name not in wanted:
                old.unlink()
        for filename, target in wanted.items():
            link = folder / filename
            if link.is_symlink() and os.readlink(link) != str(target):
                link.unlink()
            if not link.exists() and not link.is_symlink():
                link.symlink_to(target)
        atomic_write(folder / "index.jsonl", jsonl([{
            "id": row["id"], "title": row["title"], "score": row.get("score", 0),
            "image": str((root / row["local_path"]).resolve()), "source_url": row["post_url"],
        } for row in members]))
        local_rows = [{**row, "local_path": "../../" + row["local_path"]} for row in members]
        local_counts = {topic: sum(topic in row["categories"] for row in members) for topic in PRIMARY_ORDER}
        write_gallery(local_rows, folder / "index.html", local_counts,
                      title=f"{name} memes", home_href="../../index.html")
    atomic_write(root / "manifest.jsonl", jsonl(rows))
    atomic_write(root / "search.tsv", "id\tcategories\tscore\ttitle\timage\tsource_url\tocr_text\n" + "".join(
        "\t".join(str(value).replace("\t", " ").replace("\n", " ") for value in (
            row["id"], ",".join(row["categories"]), row.get("score", 0), row["title"],
            str(root / row["local_path"]), row["post_url"], row.get("ocr_text", ""))) + "\n" for row in rows))
    report = {"indexed_at": now(), "images": len(rows), "with_ocr": sum(bool(r.get("ocr_text")) for r in rows),
              "category_counts": counts, "primary_category_counts": primary,
              "source_posts": sum(len(r["sources"]) for r in rows), "manifest": str(root / "manifest.jsonl")}
    atomic_write(root / "catalog.json", json.dumps(report, indent=2) + "\n")
    write_gallery(rows, root / "index.html", counts)
    command = f"python3 {Path(__file__).resolve()}"
    atomic_write(root / "README.md", "# Technology meme library\n\n"
        f"{len(rows)} cached images. Open `index.html` to filter by topic, title, OCR text, or engagement. "
        "`categories/` contains readable symlinks; originals live once in `images/`. Counts overlap across topics.\n\n"
        "```bash\n" + command + " collect --count 250\n" + command + " search --category ai --limit 12\n" +
        command + " search 'debugging' --category software-dev\n" + command +
        " prepare-carousel ID1 ID2 ID3 --out /tmp/operator-memes\n```\n\n"
        "Collection appends new unique images and automatically downloads, validates, records source URLs and observed votes, OCRs, categorizes, and refreshes this gallery. "
        "Use `collect --query 'vibe coding' --count 50` for targeted discovery. Repeated collection skips cached URLs and merges repost provenance. "
        "A smaller `--count` never truncates the library. `organize` refreshes folders without using the network.\n\n"
        "`prepare-carousel` accepts IDs from search or cached paths, preserves input order, pads images without cropping to 1080×1350 JPEG, and returns `images` and `source_urls` for `instagram_draft_carousel`. "
        "It preserves original images and creates no public post.\n\n"
        "## Topics\n\n" + "\n".join(f"- {name}: {count}" for name, count in counts.items()) + "\n\n"
        "`manifest.jsonl` retains creators, original URLs, source posts, engagement snapshots, and image text. "
        "`sources.json` controls collection communities. Third-party rights status remains as recorded in each source row.\n")
    return report


def collect(root: Path, count: int, pages: int, min_score: int, queries: list[str] | None) -> dict:
    with library_lock(root):
        (root / "images").mkdir(exist_ok=True)
        config_path = root / "sources.json"
        if not config_path.exists():
            atomic_write(config_path, json.dumps(DEFAULT_SOURCES, indent=2) + "\n")
        rows = load_rows(root)
        before = len(rows)
        # One migration snapshot preserves the pre-library metadata exactly.
        original = root / "research" / "manifest-before-library.jsonl"
        if rows and not original.exists():
            original.parent.mkdir(exist_ok=True)
            shutil.copyfile(root / "manifest.jsonl", original)
        enrich(rows, root)
        hashes = {row["sha256"]: row for row in rows}
        pixels = {row["pixel_sha256"]: row for row in rows if row.get("pixel_sha256")}
        urls = {}
        for row in rows:
            for source in row.get("sources", [row]):
                if source.get("image_url"):
                    urls[source["image_url"]] = row
        candidates, source_errors = fetch_candidates(json.loads(config_path.read_text()), pages, min_score, queries)
        print(json.dumps({"event": "discovered", "candidates": len(candidates), "existing": before,
                          "source_errors": len(source_errors)}), file=sys.stderr, flush=True)
        pending, seen_urls = [], set()
        for candidate in candidates:
            if candidate["image_url"] in urls and (root / urls[candidate["image_url"]]["local_path"]).is_file():
                add_source(urls[candidate["image_url"]], candidate)
            elif candidate["image_url"] not in seen_urls:
                seen_urls.add(candidate["image_url"])
                pending.append(candidate)
        downloaded, duplicates, failures, attempted, old_cached = 0, 0, [], 0, 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            offset = 0
            while offset < len(pending) and len(rows) - before < count:
                batch = pending[offset:offset + min(12, count - (len(rows) - before))]
                offset += len(batch)
                for candidate, (row, error) in zip(batch, pool.map(lambda item: download(item, root), batch)):
                    attempted += 1
                    if row is None:
                        failures.append({"image_url": candidate["image_url"], "error": error})
                        continue
                    was_new = row.pop("_new_file")
                    existing = hashes.get(row["sha256"]) or pixels.get(row["pixel_sha256"])
                    if existing:
                        add_source(existing, row)
                        urls[row["image_url"]] = existing
                        duplicates += 1
                        if row["sha256"] != existing["sha256"]:
                            # Same decoded pixels with different encoding: keep one file.
                            (root / row["local_path"]).unlink(missing_ok=True)
                        continue
                    row["sources"] = [source_record(row)]
                    rows.append(row)
                    hashes[row["sha256"]] = row
                    pixels[row["pixel_sha256"]] = row
                    urls[row["image_url"]] = row
                    downloaded += int(was_new)
                    old_cached += int(not was_new)
                if attempted % 60 == 0:
                    print(json.dumps({"event": "downloaded", "added": len(rows) - before,
                                      "attempted": attempted}), file=sys.stderr, flush=True)
        # Include alternate post URLs pointing at an image encountered later in the batch.
        for candidate in candidates:
            if candidate["image_url"] in urls:
                add_source(urls[candidate["image_url"]], candidate)
        print(json.dumps({"event": "ocr_and_index", "images": len(rows)}), file=sys.stderr, flush=True)
        enrich(rows, root)
        report = write_indexes(rows, root)
        report.update({"before": before, "added_images": len(rows) - before,
                       "new_files_downloaded": downloaded, "download_attempts": attempted,
                       "previously_unindexed_cached_files": old_cached,
                       "duplicates_merged": duplicates, "candidates": len(candidates),
                       "download_failures": failures, "source_errors": source_errors,
                       "requested_additions": count, "min_score": min_score})
        atomic_write(root / "report.json", json.dumps(report, indent=2) + "\n")
        receipt = root / "research" / ("collection-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        atomic_write(receipt, json.dumps(report, indent=2) + "\n")
        return report


def organize(root: Path = ROOT, redo_ocr: bool = False) -> dict:
    """Refresh categories and indexes without fetching remote sources."""
    with library_lock(root):
        rows = load_rows(root)
        enrich(rows, root, redo_ocr=redo_ocr)
        return write_indexes(rows, root)


def import_local(root: Path, image: Path, source_metadata: dict) -> dict:
    """Add one downloaded image with provenance; no network request or public post.

    Metadata: source_url (required), image_url, title/caption, author, published,
    likes, comments, platform (default instagram). Bounds match remote collection.
    """
    image = Path(image).expanduser().resolve(strict=True)
    if not image.is_file() or image.stat().st_size > MAX_BYTES:
        raise ValueError("local import must be an image file no larger than 12 MB")
    source_url = source_metadata.get("source_url") or source_metadata.get("post_url")
    if not isinstance(source_url, str) or not source_url.startswith("https://") or not urlsplit(source_url).hostname:
        raise ValueError("source_metadata.source_url must be an HTTPS post URL")
    if len(source_url) > 4000:
        raise ValueError("source URL is too long")
    body = image.read_bytes()
    metadata = inspect_image(body)
    suffix = metadata.pop("suffix")
    digest = hashlib.sha256(body).hexdigest()
    caption = str(source_metadata.get("caption") or "")[:6000]
    author = str(source_metadata.get("author") or "")[:250]
    platform = str(source_metadata.get("platform") or "instagram")[:100]
    likes = source_metadata.get("likes")
    comments = source_metadata.get("comments")
    if any(value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
           for value in (likes, comments)):
        raise ValueError("likes and comments must be nonnegative integers when provided")
    row = {
        "sha256": digest, "bytes": len(body), **metadata, "local_path": "images/" + digest + suffix,
        "post_url": source_url, "index_url": source_url, "image_url": source_metadata.get("image_url"),
        "title": str(source_metadata.get("title") or caption[:180] or f"{platform} image by {author or 'unknown author'}")[:500],
        "body": caption, "caption": caption, "community": platform, "creator": author,
        "published": source_metadata.get("published"), "platform": platform, "likes": likes,
        "score": likes or 0, "score_kind": "likes" if likes is not None else "unavailable",
        "comments": comments or 0, "engagement_observed_at": now(), "imported_at": now(),
        "imported_from": str(image), "reuse_status": "reference_only_rights_unverified",
    }
    with library_lock(root):
        rows = load_rows(root)
        enrich(rows, root)
        existing = next((item for item in rows if item["sha256"] == digest or
                         item.get("pixel_sha256") == row["pixel_sha256"]), None)
        if existing:
            add_source(existing, row)
            # Caption can reveal a topic missing from an image-only source post.
            existing["body"] = "\n".join(dict.fromkeys([existing.get("body", ""), caption])).strip()[:6000]
            row = existing
        else:
            (root / "images").mkdir(exist_ok=True)
            destination = root / row["local_path"]
            if not destination.exists():
                destination.write_bytes(body)
            row["sources"] = [source_record(row)]
            rows.append(row)
        enrich([row], root)
        report = write_indexes(rows, root)
        return {"added": existing is None, "id": row["id"],
                "image": str((root / row["local_path"]).resolve()), "categories": row["categories"],
                "source_urls": [s["post_url"] for s in row["sources"]], "library_images": report["images"]}


def search(root: Path, query: str, category: str | None, limit: int, min_score: int) -> list[dict]:
    words = query.lower().split()
    rows = sorted(load_rows(root), key=lambda row: row.get("score", 0), reverse=True)
    return [{"id": row.get("id", row["sha256"][:12]), "title": row["title"],
             "categories": row.get("categories", categories_for(row)), "score": row.get("score", 0),
             "score_kind": row.get("score_kind", "net_votes"), "likes": row.get("likes"),
             "comments": row.get("comments", 0), "image": str((root / row["local_path"]).resolve()),
             "source_urls": [s["post_url"] for s in row.get("sources", [row])],
             "ocr_text": row.get("ocr_text", "")} for row in rows
            if (not category or category in row.get("categories", categories_for(row)))
            and row.get("score", 0) >= min_score
            and all(word in (text_for(row) + " " + row["sha256"]).lower() for word in words)][:limit]


def resolve_selection(root: Path, rows: list[dict], item: str) -> dict:
    path = Path(item).expanduser()
    paths = {path.resolve(), (root / path).resolve()}
    matches = [row for row in rows if row["sha256"].startswith(item) or
               (root / row["local_path"]).resolve() in paths]
    if len(matches) != 1:
        raise ValueError(f"selection must identify one cached image: {item}")
    return matches[0]


def prepare_carousel(root: Path, items: list[str], destination: Path, background: str = "#111111") -> dict:
    from PIL import Image, ImageColor, ImageOps
    if not 2 <= len(items) <= 10:
        raise ValueError("select 2..10 cached images for a carousel")
    rows = load_rows(root)
    selected = [resolve_selection(root, rows, item) for item in items]
    if len({row["sha256"] for row in selected}) != len(selected):
        raise ValueError("each selected carousel image must be different")
    color = ImageColor.getrgb(background)
    destination = destination.expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("output directory must be new or empty to preserve existing slides")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".meme-carousel-", dir=destination.parent))
    images, sources, slides = [], [], []
    try:
        for index, row in enumerate(selected, 1):
            source = root / row["local_path"]
            with Image.open(source) as picture:
                picture = ImageOps.exif_transpose(picture).convert("RGBA")
                picture = ImageOps.contain(picture, (1080, 1350), Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", (1080, 1350), color)
                canvas.paste(picture, ((1080 - picture.width) // 2, (1350 - picture.height) // 2), picture)
                filename = f"slide-{index:02d}.jpg"
                canvas.save(staging / filename, "JPEG", quality=95, subsampling=0)
            urls = [s["post_url"] for s in row.get("sources", [row])]
            images.append(str(destination / filename))
            sources.extend(urls)
            slides.append({"file": filename, "original": str(source.resolve()), "id": row["sha256"][:12],
                           "title": row["title"], "source_urls": urls, "sha256": hashlib.sha256((staging / filename).read_bytes()).hexdigest()})
        result = {"images": images, "source_urls": list(dict.fromkeys(sources)),
                  "manifest": str(destination / "preparation.json")}
        atomic_write(staging / "preparation.json", json.dumps({**result, "prepared_at": now(),
                     "size": [1080, 1350], "background": background, "slides": slides}, indent=2) + "\n")
        if destination.exists():
            destination.rmdir()
        staging.rename(destination)
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def ensure_runtime() -> None:
    try:
        import PIL  # noqa: F401
        import requests  # noqa: F401
    except ImportError:
        if RUNTIME.is_file() and os.path.abspath(sys.executable) != str(RUNTIME):
            os.execv(str(RUNTIME), [str(RUNTIME), str(Path(__file__).resolve()), *sys.argv[1:]])
        raise SystemExit("Install media dependencies with pip install -e .[media]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    gathering = commands.add_parser("collect", help="append new memes, OCR, and refresh topic folders")
    gathering.add_argument("--count", type=int, default=250, help="maximum NEW unique images, never a total cap")
    gathering.add_argument("--pages", type=int, default=3, help="pages of 50 per source/sort/search")
    gathering.add_argument("--min-score", type=int, default=25)
    gathering.add_argument("--query", action="append", help="targeted search instead of default source feeds; repeatable")
    organizing = commands.add_parser("organize", help="refresh topic symlinks, OCR, and indexes offline")
    organizing.add_argument("--redo-ocr", action="store_true")
    looking = commands.add_parser("search", help="find cached images and return paths and provenance")
    looking.add_argument("query", nargs="?", default="")
    looking.add_argument("--category", choices=PRIMARY_ORDER)
    looking.add_argument("--limit", type=int, default=12)
    looking.add_argument("--min-score", type=int, default=0)
    preparation = commands.add_parser("prepare-carousel", help="pad 2..10 selections into ordered 1080x1350 JPEGs")
    preparation.add_argument("items", nargs="+", help="short image IDs or cached image paths")
    preparation.add_argument("--out", type=Path, required=True)
    preparation.add_argument("--background", default="#111111")
    importing = commands.add_parser("import-local", help="import a downloaded image with source metadata")
    importing.add_argument("image", type=Path)
    importing.add_argument("--metadata", type=Path, help="JSON object with source_url, author, caption, likes, etc.")
    importing.add_argument("--source-url")
    importing.add_argument("--title")
    importing.add_argument("--caption")
    importing.add_argument("--author")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.command != "search":
        ensure_runtime()
    try:
        if args.command == "collect":
            if not 1 <= args.count <= 5000 or not 1 <= args.pages <= 20 or args.min_score < 0:
                parser.error("count must be 1..5000, pages 1..20, and minimum score nonnegative")
            result = collect(root, args.count, args.pages, args.min_score, args.query)
        elif args.command == "organize":
            result = organize(root, redo_ocr=args.redo_ocr)
        elif args.command == "search":
            if args.limit < 1:
                parser.error("limit must be positive")
            result = search(root, args.query, args.category, args.limit, args.min_score)
        elif args.command == "prepare-carousel":
            result = prepare_carousel(root, args.items, args.out, args.background)
        else:
            metadata = json.loads(args.metadata.read_text(encoding="utf-8")) if args.metadata else {}
            for key in ("source_url", "title", "caption", "author"):
                if getattr(args, key):
                    metadata[key] = getattr(args, key)
            result = import_local(root, args.image, metadata)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
