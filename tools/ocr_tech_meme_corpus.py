#!/usr/bin/env python3
"""Add searchable OCR text to the private technology meme reference shelf."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from build_tech_meme_corpus import write_gallery


def read_text(image: Path) -> str:
    env = os.environ.copy()
    env["OMP_THREAD_LIMIT"] = "1"
    try:
        result = subprocess.run(
            ["tesseract", str(image), "stdout", "--psm", "11"],
            capture_output=True, text=True, timeout=25, env=env, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode:
        return ""
    text = re.sub(r"[ \t]+", " ", result.stdout)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:1200]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path.home() / ".batbox/media-reference/tech-memes")
    args = parser.parse_args()
    if not shutil.which("tesseract"):
        parser.error("tesseract is required")
    path = args.root / "manifest.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    with ThreadPoolExecutor(max_workers=4) as pool:
        texts = list(pool.map(lambda row: read_text(args.root / row["local_path"]), rows))
    for row, body in zip(rows, texts):
        row["ocr_text"] = body
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8")
    (args.root / "search.tsv").write_text(
        "".join(f"{row['sha256']}\t{row['title'].replace(chr(9), ' ')}\t"
                f"{row['ocr_text'].replace(chr(9), ' ').replace(chr(10), ' ')}\n"
                for row in rows), encoding="utf-8",
    )
    write_gallery(rows, args.root / "index.html")
    print(json.dumps({"images": len(rows),
                      "with_ocr": sum(bool(row["ocr_text"]) for row in rows),
                      "manifest": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
