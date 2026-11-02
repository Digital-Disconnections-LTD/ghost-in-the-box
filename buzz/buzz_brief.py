#!/usr/bin/env python3
"""Create source-linked briefs and draft body files for social platforms.

The brief records sources, dates, lane limits, and content hashes. Its output
is an editorial artifact, rather than evidence that a post was delivered.
Review a body file, then stage it through the outbox or the Substack tools.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from farm.cli_output import guarded

SCHEMA = 1
COMMAND = "brief"
# Lanes the outbox can publish; the rest are briefs you post yourself.
OUTBOX_LANES = ("threads", "instagram", "linkedin")
EPILOG = """\
Topic pack (--json FILE): a JSON object; see examples/topic-pack.json.
  schema   1
  title    the topic, one line
  why      list of dated, cited "why now" facts (at least one)
  angle    list of on-mission turns (at least one)
  hooks    list of candidate lead lines
  sources  list of {"label", "url", "date"?, "claim"?}
  lanes    list of: threads, instagram, linkedin, facebook, tiktok, youtube, substack
  voice    list of voice notes
  drafts   object of LANE: proposed body text
Flags add to the pack's lists. Example:
  ghost-in-the-box brief --json examples/topic-pack.json --out out/demo --emit
"""

# A lane's default format + cap; the author's voice rules are carried on the pack.
LANES = {
    "threads":   {"label": "Threads",   "format": "text post / thread",   "cap": 500},
    "instagram": {"label": "Instagram", "format": "carousel or caption",   "cap": 2200},
    "linkedin":  {"label": "LinkedIn",  "format": "professional text",     "cap": 3000},
    "facebook":  {"label": "Facebook",  "format": "text + image",          "cap": 63206},
    "tiktok":    {"label": "TikTok",    "format": "video / short",         "cap": 4000},
    "youtube":   {"label": "YouTube",   "format": "video + description",   "cap": 5000},
    "substack":  {"label": "Substack",  "format": "newsletter letter",     "cap": None},
}


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(s: str, maxlen: int = 50) -> str:
    s = (s or "topic").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:maxlen].rstrip("-") or "topic"


def _word_count(s: str) -> int:
    return len(s.split())


def _load_json_pack(path: str) -> dict:
    raw = Path(path).read_text(encoding="utf-8")
    pack = json.loads(raw)
    if pack.get("schema", SCHEMA) not in (SCHEMA,):
        sys.stderr.write(f"[buzz] warning: unexpected topic-pack schema {pack.get('schema')}\n")
    return pack


class BriefInputError(ValueError):
    """A topic pack that cannot become a brief; the message says what is missing."""

    def __init__(self, message: str, *, code: str, hint: str):
        super().__init__(message)
        self.code = code
        self.hint = hint


def _merge_pack(args) -> dict:
    pack = _load_json_pack(args.json) if args.json else {}

    pack.setdefault("title", args.title)
    why = list(pack.get("why", []))
    for t in (args.why or []):
        why.append(t)
    pack["why"] = why

    angles = list(pack.get("angle", []))
    for t in (args.angle or []):
        if isinstance(t, str):
            angles.append(t)
    pack["angle"] = angles if len(angles) >= 1 else []

    hooks = list(pack.get("hooks", []))
    for h in (args.hook or []):
        hooks.append(h)
    pack["hooks"] = hooks

    lanes = list(pack.get("lanes", []))
    for L in (args.lane or []):
        if L.lower() not in lanes:
            lanes.append(L.lower())
    pack["lanes"] = lanes

    # sources: pack may be list of {label,url,date?,claim?}; also accept --source / --claim
    sources = list(pack.get("sources", []))
    # build a source list from repeated --source / --claim args
    for spec in (args.source or []):
        sources.append(_parse_source(spec))
    pack["sources"] = sources
    # --claim applies to the most recent source without a claim (or the last)
    claims = list(args.claim or [])
    # attach positional claims to matching indices (simple: by count)
    for i, c in enumerate(claims):
        if i < len(sources) and not sources[i].get("claim"):
            sources[i]["claim"] = c
    # voice notes
    voice = list(pack.get("voice", []))
    for v in (args.voice or []):
        voice.append(v)
    pack["voice"] = voice

    drafts = dict(pack.get("drafts", {}))
    for d in (args.draft or []):
        lane, sep, body = d.partition("=")
        if not sep or not lane.strip():
            sys.stderr.write(f"[buzz] skipping bad --draft (want LANE=TEXT): {d!r}\n")
            continue
        drafts[lane.strip().lower()] = body
    pack["drafts"] = drafts
    return pack


def _parse_source(spec: str) -> dict:
    spec = spec.strip()
    if re.match(r"https?://", spec):
        return {"url": spec, "label": ""}
    label, sep, url = spec.partition("=")
    if not sep:
        return {"url": spec, "label": ""}
    return {"label": label, "url": url}


def render(pack: dict) -> tuple[str, dict, list[dict]]:
    """Render a Markdown brief, its manifest, and per-lane receipts."""
    title = (pack.get("title") or "Untitled topic").strip()
    ts = _utc()
    slug = _slug(pack.get("slug") or pack.get("draft_slug") or title)
    requested = [str(L).lower() for L in pack.get("lanes", [])]
    unknown = [L for L in requested if L not in LANES]
    if unknown:
        raise BriefInputError(f"unknown lane {', '.join(unknown)}; lanes are: {', '.join(LANES)}",
                              code="unknown_lane", hint="use --lane with one of: " + ", ".join(LANES))
    lanes = list(dict.fromkeys(requested))
    if not lanes:
        raise BriefInputError("at least one lane is required", code="lane_required",
                              hint="add --lane threads (or set \"lanes\" in the topic pack)")
    why = [w.strip() for w in pack.get("why", []) if w and w.strip()]
    angles = [a.strip() if isinstance(a, str) else str(a) for a in pack.get("angle", []) if a and str(a).strip()]
    hooks = [h.strip() for h in pack.get("hooks", []) if h and h.strip()]
    sources = pack.get("sources", [])
    voice = [v.strip() for v in pack.get("voice", []) if v and v.strip()]
    drafts = pack.get("drafts", {}) or {}

    if not why:
        raise BriefInputError("at least one dated 'why now' fact is required", code="why_required",
                              hint="add --why 'DATE: what happened (source)' or a \"why\" list in the pack")
    if not angles:
        raise BriefInputError("at least one angle (the on-mission turn) is required", code="angle_required",
                              hint="add --angle TEXT or an \"angle\" list in the pack")

    lines: list[str] = []
    lines.append(f"# Brief — {title}")
    lines.append("")
    lines.append(f"Topic-pack receipt: `{ts}` · slug `{slug}` · schema v{SCHEMA}")
    lines.append("")

    lines.append("## The turn (the on-mission hook)")
    lines.append("_The one sentence the audience should remember and share._")
    for a in angles:
        lines.append(f"- {a}")
    lines.append("")

    lines.append("## Why now (dated, cited)")
    for i, w in enumerate(why, 1):
        lines.append(f"{i}. {w}")
    lines.append("")

    if hooks:
        lines.append("## Candidate lead lines")
        for h in hooks:
            lines.append(f"- “{h}”")
        lines.append("")

    lines.append("## Lanes")
    line_receipts = []
    for L in lanes:
        meta = LANES[L]
        cap = meta.get("cap")
        body = (drafts.get(L) or "").strip()
        if not body:
            body = _scaffold(title, angles, hooks, L)
            status = "scaffold — author writes the final copy (house voice)"
        else:
            status = "operator-drafted — on-voice review pending"
        wc = _word_count(body)
        over = cap is not None and len(body) > cap
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        tags = _hashtags_for(L, hooks + angles + [title])

        lines.append(f"### {meta['label']}")
        lines.append(f"- Format: {meta['format']}" + (f" · cap {cap}" if cap else ""))
        lines.append(f"- Body: {len(body)} characters · {wc} words" +
                     (f"  ⚠ OVER CAP ({cap} characters)" if over else ""))
        lines.append(f"- Body bytes SHA-256: `{sha[:12]}…` ({len(body.encode('utf-8'))} B)")
        lines.append(f"- Status: {status}")
        if tags:
            lines.append(f"- Hashtag set (0–3, specific-first): {' '.join(tags)}")
        lines.append("")
        lines.append(body)
        lines.append("")
        line_receipts.append({
            "lane": L,
            "label": meta["label"],
            "format": meta["format"],
            "cap": cap,
            "word_count": wc,
            "character_count": len(body),
            "over_cap": bool(over),
            "status": status,
            "body_sha256": sha,
            "body_bytes": len(body.encode("utf-8")),
            "hashtags": tags,
        })

    lines.append("## Voice notes (keep it in-house)")
    if voice:
        for v in voice:
            lines.append(f"- {v}")
    else:
        lines.append("- House voice: bold, witty, one idea per post; no hashtags-stuffing; "
                     "the anti-rent, own-your-thing frame. The author (operator) finalizes all copy.")
    lines.append("")

    lines.append("## Sources")
    if sources:
        for i, s in enumerate(sources, 1):
            url = s.get("url", "")
            label = s.get("label") or url or f"(source {i})"
            claim = s.get("claim")
            date = s.get("date") or ""
            bits = [label, f"({date})" if date else "", f"[claim: {claim}]" if claim else "", "→", url]
            lines.append(f"{i}. " + " ".join(b for b in bits if b))
    else:
        lines.append("- (no sources recorded — a brief should cite its facts; add --source)")
    lines.append("")

    lines.append("---")
    lines.append(f"Composed by the ghost-in-the-box briefing engine ({_utc()}). "
                 "A brief is a dated, byte-identified artifact; it is not proof of publication.")

    # --- receipt manifest ---
    receipt = {
        "schema": SCHEMA,
        "ts_utc": ts,
        "title": title,
        "slug": slug,
        "angles": angles,
        "why": why,
        "hooks": hooks,
        "voice": voice,
        "source_count": len(sources),
        "sources": sources,
        "lanes": line_receipts,
        "manifest_sha256": "",
    }
    # hash over the canonical projection of everything but the manifest field
    proj = {k: v for k, v in receipt.items() if k != "manifest_sha256"}
    receipt_bytes = json.dumps(proj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    receipt["manifest_sha256"] = hashlib.sha256(receipt_bytes.encode("utf-8")).hexdigest()
    brief_text = "\n".join(lines) + "\n"
    return brief_text, receipt, line_receipts


def _scaffold(title: str, angles: list, hooks: list, lane: str) -> str:
    hook = hooks[0] if hooks else ""
    turn = (angles[0] if angles else "")
    body = (
        f"[draft: {LANES[lane]['label']} — {title}]\n"
        f"Lead: {hook or '<write the lead line>'}\n"
        f"\n"
        f"<author: one idea per post, in the house voice>\n"
    )
    if turn:
        body += f"Turn to land: {turn}"
    return body.rstrip()


def _hashtags_for(lane: str, tokens: list[str]) -> list[str]:
    # conservative, specific-first; the author owns the final choice
    stop = {"the", "a", "an", "of", "in", "on", "to", "and", "your", "our", "for", "we", "is", "are"}
    words = re.findall(r"[A-Za-z]{3,}", " ".join(tokens))
    seen, out = set(), []
    for w in words:
        lw = w.lower()
        if lw in stop or lw in seen:
            continue
        seen.add(lw)
        out.append("#" + lw)
        if len(out) >= 3:
            break
    return out


def next_steps(staging: dict, sources: list[dict]) -> list[str]:
    """The commands to run after reviewing the emitted body files, ready to paste."""
    source_args = "".join(" --source-url " + shlex.quote(source["url"])
                          for source in sources if source.get("url"))
    lines = ["", "After reviewing a body file, save it as a local draft (nothing is sent):"]
    brief_only = []
    for lane, body_path in staging.items():
        body = shlex.quote(body_path)
        if lane == "substack":
            lines.append(f"  Substack: use the substack-mcp draft tools with {body}")
        elif lane == "instagram":
            lines.append(f"  ghost-in-the-box outbox draft --platform instagram --account YOUR_HANDLE "
                         f"--body-file {body} --media YOUR_IMAGE.jpg{source_args}")
            lines.append("    (Instagram needs an image; for 2-10 slides use: ghost-in-the-box carousel draft --help)")
        elif lane in OUTBOX_LANES:
            lines.append(f"  ghost-in-the-box outbox draft --platform {lane} --account YOUR_HANDLE "
                         f"--body-file {body}{source_args}")
        else:
            brief_only.append(f"  {LANES[lane]['label']}: post {body} yourself (brief-only platform)")
    lines += brief_only
    if any(lane in OUTBOX_LANES for lane in staging):
        lines += ["Then find its JOB_ID, preview exactly what would be sent (sends nothing), "
                  "and queue only the draft you chose:",
                  "  ghost-in-the-box outbox list",
                  "  ghost-in-the-box publish --dry-run JOB_ID",
                  "  ghost-in-the-box outbox queue JOB_ID"]
    return lines


@guarded(COMMAND)
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--json", metavar="FILE", help="topic-pack JSON file (format below)")
    ap.add_argument("--title", metavar="TEXT", help="inline topic")
    ap.add_argument("--why", action="append", metavar="TEXT", help="dated, cited 'why now' fact (repeat)")
    ap.add_argument("--angle", action="append", metavar="TEXT", help="the on-mission turn (repeat)")
    ap.add_argument("--hook", action="append", metavar="TEXT", help="candidate lead line (repeat)")
    ap.add_argument("--source", action="append", metavar="LABEL=URL", help="source link, or a bare URL (repeat)")
    ap.add_argument("--claim", action="append", metavar="TEXT",
                    help="the claim a source supports; the Nth --claim goes with the Nth source")
    ap.add_argument("--lane", action="append", metavar="LANE", help="target lane, repeatable: " + ", ".join(LANES))
    ap.add_argument("--voice", action="append", metavar="TEXT", help="voice constraint note (repeat)")
    ap.add_argument("--draft", action="append", metavar="LANE=TEXT", help="proposed body for one lane (repeat)")
    ap.add_argument("--draft-slug", dest="slug", metavar="WORDS", help="filename slug words")
    ap.add_argument("--out", default=None, metavar="DIR", help="output directory (default ./out/briefs)")
    ap.add_argument("--emit", action="store_true", help="also write per-lane .body staging files")
    ap.add_argument("--quiet", action="store_true", help="print one tab-separated summary line")
    args = ap.parse_args(argv)

    pack = _merge_pack(args)

    brief_text, receipt, lane_receipts = render(pack)
    ts_compact = datetime.now(timezone.utc).strftime("%Y%m%d")
    # A pip install has no repository, so the default lives under the working directory.
    out = Path(args.out) if args.out else Path.cwd() / "out" / "briefs"
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = f"{ts_compact}-{receipt['slug']}"
    brief_path = out / f"{base}.brief.md"
    rec_path = out / f"{base}.receipt.json"

    brief_path.write_text(brief_text, encoding="utf-8")
    rec_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")

    staging = {}
    if args.emit:
        for r in lane_receipts:
            lane = r["lane"]
            drafts = pack.get("drafts", {})
            body = ((drafts.get(lane) or "").strip() or
                    _scaffold(receipt["title"], receipt["angles"], receipt["hooks"], lane))
            staging_path = out / f"{base}.{lane}.body"
            staging_path.write_text(body + "\n", encoding="utf-8")
            staging[lane] = str(staging_path)

    if args.quiet:
        labels = ','.join(r['label'] for r in receipt['lanes'])
        print(f"{base}\t{receipt['manifest_sha256'][:16]}\tlanes={labels}\t{brief_path.name}")
    else:
        print("BRIEF RECEIPT")
        print(f"  file       : {brief_path}")
        print(f"  receipt    : {rec_path}")
        print(f"  manifest   : {receipt['manifest_sha256'][:16]}…")
        print(f"  why now    : {len(receipt['why'])} dated fact(s)")
        print(f"  sources    : {receipt['source_count']}")
        print(f"  lanes      : {', '.join(l['label'] for l in receipt['lanes'])}")
        for L in staging.values():
            print(f"  staging    : {L}")
        if staging:
            print("\n".join(next_steps(staging, receipt["sources"])))
        else:
            print("\nAdd --emit to write per-lane body files you can turn into drafts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
