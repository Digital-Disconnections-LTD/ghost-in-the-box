#!/usr/bin/env python3
"""Inspect one local image with the on-box vision model; return a compact answer.

The image bytes go to your configured vision endpoint and are never written to a
Batbox transcript or the tool's terminal output. Validate an image, encode it as a typed base64 image part, and send its
question to the configured vision endpoint.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys

import requests


API = os.environ.get("SOCIAL_FARM_VISION_URL", "http://127.0.0.1:8001/v1/chat/completions")
MODEL = os.environ.get("SOCIAL_FARM_VISION_MODEL", "your-vision-model")
MAX_BYTES = 8_000_000
DEFAULT_QUESTION = (
    "Describe what is visually happening in this image, read the visible words, "
    "and explain the joke or argument in three concise sentences."
)


def media_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("unsupported image bytes; use JPEG, PNG, or WebP")


def inspect(path: Path, question: str, *, max_tokens: int = 700,
            endpoint: str = API, model: str = MODEL,
            session: requests.Session | None = None) -> dict:
    path = path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("image path is not a regular file")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("image exceeds 8 MB; select a smaller image")
    data = path.read_bytes()
    mime = media_type(data)
    image_url = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": question.strip() or DEFAULT_QUESTION},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": False,
    }
    client = session or requests.Session()
    response = client.post(endpoint, json=payload, timeout=150)
    response.raise_for_status()
    body = response.json()
    answer = body["choices"][0]["message"].get("content") or ""
    return {
        "ok": bool(answer.strip()), "image": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "answer": answer.strip(),
        "finish_reason": body["choices"][0].get("finish_reason"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--max-tokens", type=int, default=700)
    parser.add_argument("--endpoint", default=API)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args(argv)
    try:
        if not 64 <= args.max_tokens <= 2000:
            raise ValueError("max-tokens must be 64..2000")
        result = inspect(args.image, args.question, max_tokens=args.max_tokens,
                         endpoint=args.endpoint, model=args.model)
    except (OSError, ValueError, requests.RequestException, KeyError, IndexError, TypeError) as exc:
        result = {"ok": False, "error": str(exc)[:300]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
