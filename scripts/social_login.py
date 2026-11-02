#!/usr/bin/env python3
"""Choose your own logins, save local sessions, or move selected sessions to a server.

Exit status: 0 ok, 1 runtime failure, 2 usage error, 4 login failed or a
dependency is missing, 130 cancelled.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.accounts import AccountRegistry, RegistryError, check_handle, handle_rule
from farm.cli_output import (
    DOCTOR, EXIT_AUTH, EXIT_CANCELLED, EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, error_object, fail, guarded,
    succeed,
)
from farm.config import chromium_install_command, install_command, login_family, login_profile_dir
from farm.login import (
    BROWSER_CHOICES, LINKED, LoginError, LoginWindow, PROVIDERS, browser_login, export_accounts,
    forget_login_profile, import_accounts, login_browser_choice, save_login,
)

PROVIDER_MODULES = {"instagram": ("instagrapi",), "substack": ("substack",)}
# Menu numbers follow this order; the labels are what the wizard shows.
MENU = (("instagram", "Instagram"), ("threads", "Threads"), ("substack", "Substack"), ("linkedin", "LinkedIn"))
LABELS = dict(MENU)
MENU_CHOICES = {**{str(number): name for number, (name, _label) in enumerate(MENU, 1)}, **{name: name for name in LABELS}}
MENU_DONE = ("", "d", "done")
YES = ("", "y", "yes")
# Shown after one Meta login succeeds, so the other one reuses it.
OFFERS = {"instagram": "Threads uses the same Instagram login. Connect Threads now with it? [Y/n] ",
          "threads": "Instagram uses the same login as Threads. Connect Instagram now with it? [Y/n] "}
MENU_NOTES = {"threads": " (same login as Instagram)"}


def _error(code, message, hint=None):
    return error_object(code, message, hint)


def _failure(status, code, message, hint, exit_code, error_type=None):
    # `status` and `error_type` stay for callers that matched on them before the envelope.
    legacy = {"status": status, **({"error_type": error_type} if error_type else {})}
    return fail(code, message, hint, exit_code, **legacy)


def _parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example: ghost-in-the-box login start --platform instagram --account YOUR_HANDLE")
    sub = parser.add_subparsers(dest="command", metavar="{start,export,import,list,forget-browser}")
    start = sub.add_parser("start", help="open a visible browser for each selected provider",
                           description="Open a visible browser, sign in yourself, and save the session.")
    start.add_argument("--platform", choices=PROVIDERS, action="append", help="repeat for multiple logins")
    start.add_argument("--account", metavar="HANDLE",
                       help="handle for one selected platform; otherwise read from the session and confirmed")
    start.add_argument("--publication", metavar="URL", help="Substack publication HTTPS URL")
    start.add_argument("--browser", choices=BROWSER_CHOICES,
                       help="browser to open (default auto: Chrome, Edge, Brave, Chromium, then Playwright "
                            "Chromium); overrides SOCIAL_FARM_LOGIN_BROWSER")
    start.add_argument("--channel", dest="browser", choices=BROWSER_CHOICES, help=argparse.SUPPRESS)
    export = sub.add_parser("export", help="write an explicitly selected credential bundle",
                            description="Write the selected accounts' live sessions to one private bundle file.")
    export.add_argument("--account", action="append", required=True, metavar="PLATFORM:HANDLE",
                        help="account to include, e.g. instagram:YOUR_HANDLE; repeat to select more")
    export.add_argument("--output", type=Path, required=True, metavar="PATH", help="bundle file to write (contains live sessions)")
    export.add_argument("--replace", action="store_true", help="overwrite an existing bundle file")
    load = sub.add_parser("import", help="import a bundle and rewrite paths for this machine",
                          description="Import a bundle from login export and register its accounts here.")
    load.add_argument("bundle", type=Path, metavar="BUNDLE", help="file written by login export")
    load.add_argument("--replace", action="store_true", help="replace registrations that already exist here")
    sub.add_parser("list", help="list registered account references without reading sessions")
    forget = sub.add_parser("forget-browser", help="delete one platform's login browser profile",
                            description="Delete the browser profile the wizard keeps for one platform, "
                                        "so the next login starts clean. Saved account sessions stay.")
    forget.add_argument("--platform", choices=PROVIDERS, help="platform whose browser profile to delete")
    forget.add_argument("--yes", action="store_true", help="delete without asking")
    return parser


def _ask(prompt):
    # Prompts go to stderr so stdout stays one JSON document.
    print(prompt, end="", file=sys.stderr, flush=True)
    return input()


def _missing_dependency(provider):
    modules = ("playwright", *PROVIDER_MODULES.get(provider, ()))
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if not missing:
        return None
    extra = "login" + ("," + provider if provider in PROVIDER_MODULES else "")
    hint = f"{install_command(extra)} && {chromium_install_command()}"
    return {"platform": provider, "ok": False, "status": "dependency_unavailable",
            "error": _error("dependency_unavailable",
                            f"{provider} login needs {', '.join(missing)} (the '{extra}' extra)", hint)}


def _interactive():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _where(window, profile_dir):
    return {"browser": window.browser, "mode": window.mode,
            "profile_dir": str(window.profile_dir or profile_dir)}


def _login_one(provider, args, window, *, account=None, linked=False):
    """Run one browser login and save it; returns the per-platform result."""
    profile_dir = login_profile_dir(provider)
    missing = _missing_dependency(provider)
    if missing:
        print(f"{missing['error']['message']}. Install: {missing['error']['hint']}", file=sys.stderr)
        return {**missing, **_where(window, profile_dir)}
    publication = (args.publication or _ask("Publication URL: ").strip()) if provider == "substack" else None
    try:
        account, session = browser_login(provider, account, publication=publication, browser=args.browser,
                                         window=window, linked=linked)
        saved = save_login(provider, account, session)
        return {"platform": provider, "account": account, "ok": True, "session_file": saved["session_file"],
                **_where(window, profile_dir)}
    except (KeyboardInterrupt, EOFError):
        raise
    except LoginError as exc:
        # Our own messages: safe to show, and more useful than a generic failure.
        status = exc.code if exc.code in ("dependency_unavailable", "local_state_unavailable") else "login_failed"
        print(f"{provider} login was not saved: {exc.message}. Next: {exc.hint}", file=sys.stderr)
        return {"platform": provider, "ok": False, "status": status, "error_type": type(exc).__name__,
                "error": exc.to_error(), **_where(window, profile_dir)}
    except OSError as exc:
        print(f"{provider} login was not saved: the session could not be written.", file=sys.stderr)
        return {"platform": provider, "ok": False, "status": "local_state_unavailable",
                "error_type": type(exc).__name__,
                "error": _error("local_state_unavailable",
                                f"{provider} session could not be saved: {exc.strerror or exc}", DOCTOR),
                **_where(window, profile_dir)}
    except Exception as exc:
        # Browser/SDK exceptions can include cookies, URLs or page content.
        retry = f"ghost-in-the-box login start --platform {provider}" + (f" --account {account}" if account else "")
        print(f"{provider} login was not saved. Confirm the handle and finish provider security prompts.", file=sys.stderr)
        return {"platform": provider, "ok": False, "status": "login_failed",
                "error_type": type(exc).__name__,
                "error": _error("login_failed",
                                f"{provider} login was not verified for {account or 'that handle'}",
                                f"confirm the handle, finish any security prompts, then: {retry}"),
                **_where(window, profile_dir)}


def _connected_as(registered, platform, account):
    return any(entry["account"].casefold() == account.casefold() for entry in (registered or {}).get(platform, ()))


def _login_session(provider, args, *, account=None, linked=False, queued=None, registered=None):
    """One login, then the other Meta platform in the same browser window when wanted."""
    window = LoginWindow(args.browser)
    try:
        result = _login_one(provider, args, window, account=account, linked=linked)
        results = [result]
        if not result["ok"]:
            return results
        print(f"{LABELS[provider]} connected as @{result['account']}. Session saved.", file=sys.stderr)
        partner = LINKED.get(provider)
        if partner is None or _connected_as(registered, partner, result["account"]):
            return results
        if queued is not None and partner in queued:
            # Asked for both: the second one reuses the first login without another question.
            queued.remove(partner)
        elif _ask(OFFERS[provider]).strip().lower() not in YES:
            return results
        follow = _login_one(partner, args, window, account=result["account"], linked=True)
        results.append(follow)
        if follow["ok"]:
            print(f"{LABELS[partner]} connected as @{follow['account']}. Session saved.", file=sys.stderr)
        return results
    finally:
        window.close()


def _registered():
    """Registry entries grouped by platform; reads only."""
    grouped = {}
    for entry in AccountRegistry().list():
        grouped.setdefault(entry["platform"], []).append(entry)
    return grouped


def _registered_on(entry):
    try:
        # The registry stores UTC; show the day as the user lived it.
        return datetime.fromisoformat(str(entry.get("created_at") or "")).astimezone().date().isoformat()
    except ValueError:
        return None


def _connection(entries):
    if not entries:
        return "not connected"
    handles = []
    for entry in entries:
        day = _registered_on(entry)
        handles.append(f"@{entry['account']}" + (f" ({day})" if day else ""))
    return "logged in as " + ", ".join(handles)


def _menu(registered):
    lines = ["Which account do you want to connect?"]
    lines += [f"  {number}) {label:<15}{_connection(registered.get(name))}"
              + (MENU_NOTES.get(name, "") if not registered.get(name) else "")
              for number, (name, label) in enumerate(MENU, 1)]
    lines.append("  d) done")
    return "\n".join(lines)


def _menu_choice(answer):
    """Platforms picked at the menu: [] means done, None means the answer is not a choice."""
    text = answer.strip().lower()
    if text in MENU_DONE:
        return []
    tokens = text.replace(",", " ").split()
    if not tokens or any(token not in MENU_CHOICES for token in tokens):
        return None
    return list(dict.fromkeys(MENU_CHOICES[token] for token in tokens))


def _pick():
    while True:
        answer = _ask("Pick a number (or d): ")
        picked = _menu_choice(answer)
        if picked is not None:
            return picked
        print(f"Not a choice: {answer.strip()[:40]!r}. Enter 1-{len(MENU)}, a platform name, or d.",
              file=sys.stderr)


def _menu_session(args):
    # Each login is saved as it completes, so Ctrl+C here loses only the login in progress.
    results = []
    while True:
        registered = _registered()
        print(_menu(registered), file=sys.stderr)
        picked = _pick()
        if not picked:
            return results
        queue = list(picked)
        while queue:
            provider = queue.pop(0)
            label = LABELS[provider]
            existing = registered.get(provider)
            account, linked = None, False
            if existing:
                handles = ", ".join(f"@{entry['account']}" for entry in existing)
                again = _ask(f"{label} is already connected as {handles}. Log in again? [y/N] ")
                if again.strip().lower() not in ("y", "yes"):
                    print(f"Kept the existing {label} login.", file=sys.stderr)
                    continue
            else:
                partner = LINKED.get(provider)
                handles = [entry["account"] for entry in registered.get(partner, ())] if partner else []
                if len(handles) == 1:
                    account, linked = handles[0], True
                    print(f"{label} uses your {LABELS[partner]} login. Continuing as @{account}.", file=sys.stderr)
            results += _login_session(provider, args, account=account, linked=linked, queued=queue,
                                      registered=registered)


def _start(args, parser):
    # The browser login always ends at a terminal prompt, so refuse before opening anything.
    if not _interactive():
        return _failure("login_or_transfer_failed", "not_interactive",
                        "login start needs an interactive terminal to finish the browser sign-in",
                        "run from an interactive terminal, or use `login import` for a saved bundle",
                        EXIT_USAGE)
    # A bad SOCIAL_FARM_LOGIN_BROWSER is a usage error before any browser opens.
    login_browser_choice(args.browser)
    payload = {}
    if args.platform:
        queue, results = list(dict.fromkeys(args.platform)), []
        while queue:
            results += _login_session(queue.pop(0), args, account=args.account, queued=queue)
    else:
        results = _menu_session(args)
        payload["registered"] = [{key: entry.get(key) for key in
                                  ("platform", "account", "account_key", "created_at", "updated_at")}
                                 for entry in AccountRegistry().list()]
    failures = [r for r in results if not r["ok"]]
    payload = {"ok": not failures, "accounts": results, **payload}
    if failures:
        payload["error"] = failures[0]["error"]
    print(json.dumps(payload))
    if not failures:
        return EXIT_OK
    if any(r["status"] == "local_state_unavailable" for r in failures):
        return EXIT_RUNTIME
    return EXIT_AUTH


def _forget_browser(args):
    if not args.platform:
        return _failure("login_or_transfer_failed", "platform_required",
                        "login forget-browser needs --platform so only that browser profile is deleted",
                        "ghost-in-the-box login forget-browser --platform linkedin", EXIT_USAGE)
    profile = login_profile_dir(args.platform)
    shared = " and ".join(LABELS[name] for name, _label in MENU
                          if login_family(name) == login_family(args.platform))
    if profile.exists() and not args.yes:
        if not _interactive():
            return _failure("login_or_transfer_failed", "not_interactive",
                            "login forget-browser asks before deleting and needs an interactive terminal",
                            f"ghost-in-the-box login forget-browser --platform {args.platform} --yes", EXIT_USAGE)
        answer = _ask(f"Delete the {shared} login browser profile at {profile}? It holds a signed-in browser "
                      "session. Saved account sessions stay. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Kept the browser profile.", file=sys.stderr)
            return succeed({"platform": args.platform, "profile_dir": str(profile), "deleted": False,
                            "status": "kept"})
    return succeed(forget_login_profile(args.platform))


@guarded("login")
def main(argv=None):
    parser = _parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["start"]
    args = parser.parse_args(argv)
    if args.command == "start" and args.account and len(args.platform or []) != 1:
        parser.error("--account requires exactly one --platform")
    if args.command == "start" and args.account:
        try:
            args.account = check_handle(args.platform[0], args.account.removeprefix("@"))
        except ValueError:
            parser.error(f"--account must be {handle_rule(args.platform[0])}")
    try:
        if args.command == "export":
            result = export_accounts(args.account, args.output, replace=args.replace)
        elif args.command == "import":
            result = import_accounts(args.bundle, replace=args.replace)
        elif args.command == "list":
            result = AccountRegistry().list()
        elif args.command == "forget-browser":
            return _forget_browser(args)
        else:
            return _start(args, parser)
        return succeed(result)
    except (KeyboardInterrupt, EOFError):
        return _failure("cancelled", "cancelled", "login was cancelled; nothing further was saved",
                        "ghost-in-the-box login start --platform PLATFORM --account HANDLE", EXIT_CANCELLED)
    except (LoginError, RegistryError) as exc:
        exit_code = EXIT_RUNTIME if exc.code == "registry_unreadable" else EXIT_USAGE
        return _failure("login_or_transfer_failed", exc.code, exc.message, exc.hint, exit_code,
                        type(exc).__name__)
    except ValueError as exc:
        return _failure("login_or_transfer_failed", "invalid_input", str(exc), "ghost-in-the-box login --help",
                        EXIT_USAGE, type(exc).__name__)
    except OSError as exc:
        target = exc.filename or "local account state"
        return _failure("login_or_transfer_failed", "local_state_unavailable",
                        f"{exc.strerror or type(exc).__name__}: {target}", DOCTOR,
                        EXIT_RUNTIME, type(exc).__name__)
    except Exception as exc:
        return _failure("login_or_transfer_failed", "unexpected_error",
                        f"{type(exc).__name__} while running login {args.command}", DOCTOR,
                        EXIT_RUNTIME, type(exc).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
