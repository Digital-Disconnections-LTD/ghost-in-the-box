"""The terminal contract every Ghost in the Box command follows.

A command prints one JSON document on stdout. A failure keeps any legacy keys
the command printed before (``status``, ``error_type``) and adds one envelope:
``{"ok": false, "error": {"code", "message", "hint"}}``. The hint is always a
concrete next step, usually the exact command to run. Exit codes are stable:

  0 ok, 1 runtime failure, 2 usage or refused request, 3 uncertain send,
  4 auth or missing dependency, 130 cancelled.

argparse usage errors keep argparse's own behaviour: usage on stderr, exit 2.
"""
from __future__ import annotations

import argparse
import functools
import json
from pathlib import Path
import re
import sqlite3
import sys

from farm.accounts import check_handle, handle_rule
from farm.adapters import (
    AuthUnavailable, DependencyUnavailable, PublishUncertain, ReadFailure, safe_reason,
)

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_UNCERTAIN = 3
EXIT_AUTH = 4
EXIT_CANCELLED = 130

DOCTOR = "ghost-in-the-box doctor"
THREADS_TEXT_LIMIT = 500
# Every platform caps a post far below this; anything larger is a wrong file.
MAX_BODY_BYTES = 64_000
# Coded refusals whose cause is live local state rather than the request itself.
RUNTIME_CODES = frozenset({
    "lease_lost", "job_leased", "schema_too_new", "registry_unreadable", "session_save_failed",
})
_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")


def command_help(command: str) -> str:
    return f"ghost-in-the-box {command} --help"


def error_object(code: str, message: str, hint: str | None, *, fallback_hint: str = DOCTOR) -> dict:
    return {"code": str(code), "message": str(message), "hint": hint or fallback_hint}


def failure(code: str, message: str, hint: str | None, /, **extra) -> dict:
    """A failed result: legacy keys first, then the shared error object."""
    return {"ok": False, **extra, "error": error_object(code, message, hint)}


def emit(payload, *, stream=None, indent: int | None = None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=indent), file=stream or sys.stdout)


def fail(code: str, message: str, hint: str | None, exit_code: int = EXIT_RUNTIME, /, *,
         stream=None, **extra) -> int:
    """Print one failure envelope and return its exit code."""
    emit(failure(code, message, hint, **extra), stream=stream)
    return exit_code


def succeed(result, *, stream=None, indent: int | None = None) -> int:
    emit({"ok": True, "result": result}, stream=stream, indent=indent)
    return EXIT_OK


class _Context(dict):
    def __missing__(self, key):
        return f"<{key}>"


def attach_error(result: dict, catalog: dict, *, fallback_hint: str = DOCTOR, **context) -> int:
    """Add the shared error envelope to a failed result and return its exit code.

    catalog maps a status code to (exit_code, message, hint). Message and hint
    are format strings filled from context and the result's own fields.
    """
    if result.get("ok") is True:
        return EXIT_OK
    result["ok"] = False
    code = str(result.get("status") or "operation_unavailable")
    exit_code, message, hint = catalog.get(
        code, (EXIT_RUNTIME, "The command did not complete ({code}).", None))
    if code == "uncertain":
        exit_code = EXIT_UNCERTAIN
    values = _Context({key: value for key, value in result.items()
                       if isinstance(value, (str, int, float))})
    values.update({key: value for key, value in context.items() if value is not None})
    values["code"] = code
    existing = result.get("error") if isinstance(result.get("error"), dict) else {}
    result["error"] = error_object(
        existing.get("code") or code,
        existing.get("message") or message.format_map(values),
        existing.get("hint") or (hint.format_map(values) if hint else None),
        fallback_hint=fallback_hint)
    return exit_code


# --- message text -----------------------------------------------------------

class BodyInputError(ValueError):
    """A fixed local code for a body that cannot be used."""

    def __init__(self, code: str, *, path: str | None = None):
        super().__init__(code)
        self.path = path


def text_limit_code(limit: int) -> str:
    return f"text_must_contain_1_to_{limit}_characters"


def body_messages(limit: int | None = THREADS_TEXT_LIMIT, path: str | None = None) -> dict[str, str]:
    messages = {
        "file_not_found": f"The --body-file {path or 'PATH'} does not exist.",
        "body_input_required": "Give the text with exactly one of --body TEXT or --body-file PATH "
                               "(use --body-file - to read stdin).",
        "body_file_unreadable": f"The --body-file could not be read as UTF-8 text of at most "
                                f"{MAX_BODY_BYTES} bytes.",
        "text_must_not_be_blank": "The text is empty.",
    }
    if limit is not None:
        messages[text_limit_code(limit)] = f"The text must contain 1 to {limit} characters."
    return messages


def read_body(body: str | None, body_file: str | None, *, stdin=None,
              limit: int | None = THREADS_TEXT_LIMIT, allow_blank: bool = False) -> str:
    """Return message text from exactly one of --body, --body-file PATH or --body-file -.

    ``limit`` is the platform's character cap; None leaves length checks to the caller.
    ``allow_blank`` accepts empty text, for captions that may be left empty.
    """
    if (body is None) == (body_file is None):
        raise BodyInputError("body_input_required")
    if body is not None:
        text = body
    elif body_file == "-":
        text = (stdin or sys.stdin).read(MAX_BODY_BYTES + 1)
        if limit is None and len(text) > MAX_BODY_BYTES:
            raise BodyInputError("body_file_unreadable")
    else:
        path = Path(body_file).expanduser()
        if not path.exists():
            raise BodyInputError("file_not_found", path=str(path))
        try:
            if not path.is_file() or path.stat().st_size > MAX_BODY_BYTES:
                raise BodyInputError("body_file_unreadable")
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise BodyInputError("body_file_unreadable") from None
    if not isinstance(text, str) or (not text.strip() and not allow_blank):
        raise BodyInputError(text_limit_code(limit) if limit is not None else "text_must_not_be_blank")
    if limit is not None and len(text) > limit:
        raise BodyInputError(text_limit_code(limit))
    return text


def add_body_arguments(parser, *, required: bool = True, noun: str = "text") -> None:
    source = parser.add_mutually_exclusive_group(required=required)
    source.add_argument("--body", metavar="TEXT", help=f"the {noun} itself")
    source.add_argument("--body-file", metavar="PATH",
                        help=f"UTF-8 file with the {noun}; - reads stdin")


# --- arguments --------------------------------------------------------------

def add_account_argument(parser, *subparsers, help: str, type=None, aliases: tuple = ()) -> None:
    """Accept --account before or after the subcommand.

    The top-level copy holds the value; subcommand copies only fill it in when
    given there, so `linkedin --account HANDLE me` and `linkedin me --account HANDLE`
    behave the same.
    """
    options = {"dest": "account", "metavar": "HANDLE", "help": help}
    if type is not None:
        options["type"] = type
    parser.add_argument("--account", *aliases, default=None, **options)
    for sub in subparsers:
        sub.add_argument("--account", *aliases, default=argparse.SUPPRESS, **options)


def require_account(parser, args, *, example: str) -> str:
    account = getattr(args, "account", None)
    if not account:
        parser.error(f"--account HANDLE is required, e.g. {example}")
    return account


def check_account(parser, account: str, *, flag: str = "--account", platform: str = "instagram") -> str:
    """Refuse handles the platform does not accept before any file or network work."""
    handle = account.removeprefix("@")
    try:
        return check_handle(platform, handle)
    except ValueError:
        parser.error(f"{flag} must be {handle_rule(platform)}")


def choice_error(flag: str, value: str, valid) -> tuple[str, str]:
    """Message and hint for a value outside a known set."""
    values = ", ".join(valid)
    return f"{flag} {value!r} is not one of: {values}", f"use {flag} with one of: {values}"


# --- exceptions -------------------------------------------------------------

def exception_failure(exc: BaseException, *, command: str, auth_hint: str | None = None,
                      reveal: bool = True, runtime_codes=frozenset(), database: str | None = None,
                      value_code: str = "invalid_input") -> tuple[int, dict]:
    """Map an exception to (exit code, failure envelope) without leaking provider text.

    ``reveal`` shows a plain ValueError's own message; commands whose ValueErrors
    may come from a provider library pass False and get the class name only.
    ``database`` names the SQLite store a command uses, for its error code, and
    ``value_code`` is the code a plain ValueError gets.
    """
    help_hint = command_help(command)
    error_type = type(exc).__name__

    def out(exit_code, code, message, hint):
        return exit_code, failure(code, message, hint, error_type=error_type)

    if isinstance(exc, BodyInputError):
        code = str(exc)
        limit = re.fullmatch(r"text_must_contain_1_to_(\d+)_characters", code)
        message = body_messages(int(limit[1]) if limit else None, getattr(exc, "path", None)).get(
            code, "The text could not be used.")
        hints = {
            "file_not_found": "check the path, or pass the text inline with --body TEXT",
            "body_file_unreadable": "save the file as UTF-8 text, or pass the text inline with --body TEXT",
            "text_must_not_be_blank": "write the text into the file, or pass it with --body TEXT",
        }
        if limit:
            hints[code] = f"shorten the text to {limit[1]} characters or fewer, then run the command again"
        return out(EXIT_USAGE, code, message, hints.get(code, help_hint))
    if isinstance(exc, DependencyUnavailable):
        return out(EXIT_AUTH, "dependency_unavailable", safe_reason(exc), exc.install_hint or DOCTOR)
    if isinstance(exc, AuthUnavailable):
        return out(EXIT_AUTH, "auth_unavailable", safe_reason(exc),
                   auth_hint or "ghost-in-the-box login start --platform PLATFORM --account HANDLE")
    if isinstance(exc, PublishUncertain):
        return out(EXIT_UNCERTAIN, "publish_uncertain", safe_reason(exc),
                   "check the account for the post, then: ghost-in-the-box outbox list --status uncertain")
    if isinstance(exc, ReadFailure):
        return out(EXIT_RUNTIME, "read_unavailable", safe_reason(exc),
                   f"{DOCTOR}; if the session is fine, retry later")
    code = getattr(exc, "code", None)
    # Commands run through runpy, so their own exception classes live in __main__.
    ours = type(exc).__module__.split(".")[0] in {"farm", "buzz", "scripts", "__main__"}
    if ours and isinstance(code, str) and _CODE.fullmatch(code):
        # Our own coded refusals carry a message and hint written to be shown.
        message = getattr(exc, "message", None) or str(exc) or code
        hint = getattr(exc, "hint", None)
        if type(exc).__name__ == "InstagramSessionError":
            exit_code = EXIT_RUNTIME if code in RUNTIME_CODES else EXIT_AUTH
        elif code in RUNTIME_CODES or code in runtime_codes:
            exit_code = EXIT_RUNTIME
        else:
            exit_code = EXIT_USAGE
        return out(exit_code, code, str(message)[:500], hint if isinstance(hint, str) else help_hint)
    if type(exc).__module__.split(".")[0] in ("requests", "urllib3"):
        # These are OSErrors too, but the cause is the network, not a local file.
        return out(EXIT_RUNTIME, "network_unavailable", f"a network request failed ({error_type})",
                   f"check your connection and retry; {DOCTOR} checks the local setup")
    if isinstance(exc, FileNotFoundError):
        return out(EXIT_USAGE, "file_not_found", f"file not found: {exc.filename or exc}",
                   f"pass an existing file path; see {help_hint}")
    if isinstance(exc, IsADirectoryError):
        return out(EXIT_USAGE, "not_a_file", f"expected a file, got a directory: {exc.filename}",
                   f"pass a file path; see {help_hint}")
    if isinstance(exc, PermissionError):
        return out(EXIT_RUNTIME, "permission_denied", f"permission denied: {exc.filename or 'local file'}",
                   DOCTOR)
    if isinstance(exc, OSError):
        return out(EXIT_RUNTIME, "local_state_unavailable",
                   f"{exc.strerror or error_type}: {exc.filename or 'local state'}", DOCTOR)
    if isinstance(exc, sqlite3.Error):
        name = f"the {database} database" if database else "a local database"
        return out(EXIT_RUNTIME, f"{database}_database_error" if database else "database_error",
                   f"{name} could not be used ({error_type})",
                   f"another process may hold it; retry shortly, or run {DOCTOR}")
    if isinstance(exc, UnicodeDecodeError):
        return out(EXIT_USAGE, "not_utf8_text", "a file is not UTF-8 text", f"save it as UTF-8; see {help_hint}")
    if isinstance(exc, json.JSONDecodeError):
        return out(EXIT_USAGE, "invalid_json", f"invalid JSON at line {exc.lineno} column {exc.colno}",
                   f"fix the JSON file; see {help_hint}")
    if isinstance(exc, ValueError):
        message = str(exc)[:300] if reveal and str(exc) else f"invalid input ({error_type})"
        return out(EXIT_USAGE, value_code, message, help_hint)
    return out(EXIT_RUNTIME, "internal_error",
               f"ghost-in-the-box {command} failed ({error_type}); details are withheld", DOCTOR)


def guarded(command: str, **options):
    """Wrap a main() so no exception escapes as a traceback.

    SystemExit passes through, so argparse usage errors still exit 2.
    """
    def decorate(main):
        @functools.wraps(main)
        def wrapper(*args, **kwargs):
            try:
                return main(*args, **kwargs)
            except SystemExit:
                raise
            except KeyboardInterrupt:
                return fail("cancelled", f"ghost-in-the-box {command} was interrupted",
                            f"rerun it when ready; see {command_help(command)}", EXIT_CANCELLED)
            except Exception as exc:
                code, payload = exception_failure(exc, command=command, **options)
                emit(payload)
                return code
        return wrapper
    return decorate
