#!/usr/bin/env python3
"""Ask your own vision model one question about one local image.

The image is sent only to the configured OpenAI-compatible endpoint (a local
server by default) and never printed. Exit codes: 0 ok, 1 runtime failure,
2 usage.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
from pathlib import Path
import sys

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.cli_output import EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, emit, fail


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


COMMAND = "vision"
ENDPOINT_HINT = ("start your vision model, or point --endpoint (or SOCIAL_FARM_VISION_URL) at its "
                 "OpenAI-compatible /v1/chat/completions URL")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example: ghost-in-the-box vision --image slide.jpg --question 'What does the chart show?'")
    parser.add_argument("--image", type=Path, required=True, metavar="PATH", help="JPEG, PNG or WebP, up to 8 MB")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="what to ask about the image")
    parser.add_argument("--max-tokens", type=int, default=700, metavar="N", help="64..2000 (default 700)")
    parser.add_argument("--endpoint", default=API, metavar="URL",
                        help="chat completions URL (default SOCIAL_FARM_VISION_URL or a local server)")
    parser.add_argument("--model", default=MODEL, help="model name (default SOCIAL_FARM_VISION_MODEL)")
    args = parser.parse_args(argv)
    if not 64 <= args.max_tokens <= 2000:
        parser.error("--max-tokens must be 64..2000")
    try:
        result = inspect(args.image, args.question, max_tokens=args.max_tokens,
                         endpoint=args.endpoint, model=args.model)
    except FileNotFoundError:
        return fail("file_not_found", f"image not found: {args.image}", "pass an existing image path with --image",
                    EXIT_USAGE)
    except ValueError as exc:
        return fail("invalid_image", str(exc)[:300], "pass a JPEG, PNG or WebP image under 8 MB", EXIT_USAGE)
    except requests.RequestException as exc:
        # requests errors are OSErrors too, so this must come before the file case.
        return fail("vision_unavailable", f"the vision endpoint did not answer ({type(exc).__name__})",
                    ENDPOINT_HINT, EXIT_RUNTIME, error_type=type(exc).__name__)
    except OSError as exc:
        return fail("image_unreadable", f"{exc.strerror or type(exc).__name__}: {args.image}",
                    "check the image path and its permissions", EXIT_RUNTIME)
    except (KeyError, IndexError, TypeError):
        return fail("vision_unreadable", "the vision endpoint's answer was not a chat completion",
                    ENDPOINT_HINT, EXIT_RUNTIME)
    if not result.get("ok"):
        return fail("empty_answer", "the vision model returned an empty answer",
                    "ask a more specific --question, or raise --max-tokens", EXIT_RUNTIME,
                    **{key: value for key, value in result.items() if key != "ok"})
    emit(result)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
