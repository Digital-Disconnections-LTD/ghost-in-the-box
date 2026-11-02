"""One entry point for Ghost in the Box's existing local tools."""

from __future__ import annotations

import argparse
import difflib
from datetime import datetime, timedelta, timezone
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import runpy
import shutil
import sqlite3
import sys

from farm import config
from farm.config import STATE_ROOT

COMMANDS = {
    "login": ("scripts.social_login", "Choose logins and export/import portable account sessions"),
    "linkedin": ("scripts.linkedin", "Verify LinkedIn identity and read profiles, posts, and messages"),
    "accounts": ("scripts.social_accounts", "Register and inspect your account sessions"),
    "outbox": ("scripts.social_outbox", "Draft, queue, inspect, and reconcile deliveries"),
    "publish": ("scripts.social_publish_job", "Publish one exact job (--dry-run previews it) or due jobs (--due)"),
    "brief": ("buzz.buzz_brief", "Turn a source-linked topic pack into platform briefs"),
    "instagram-mcp": ("scripts.instagram_mcp", "Run Instagram tools over MCP stdio"),
    "instagram-inbox": ("scripts.ig_sync_inbox", "Read a bounded Instagram inbox"),
    "instagram-import": ("scripts.ig_import_own", "Import your own writing for voice context"),
    "instagram-login": ("scripts.ig_probe", "Save your own Instagram session"),
    "carousel": ("scripts.instagram_carousel", "Prepare an Instagram carousel draft"),
    "threads": ("scripts.threads_agent", "Queue Threads posts and replies, read, and check sessions"),
    "threads-setup": ("scripts.threads_browser_setup", "Set up the optional Threads browser transport"),
    "threads-notifications": ("scripts.threads_notifications", "Read and handle Threads inbox updates"),
    "memes": ("scripts.meme_library", "Manage your local visual reference library"),
    "vision": ("scripts.vision_inspect", "Inspect one image with your configured vision model"),
    "substack-mcp": ("scripts.substack_mcp_server", "Run the optional Substack MCP connector"),
    "mcp-bridge": ("scripts.mcp_stdio_bridge", "Connect framed MCP clients to JSONL servers"),
}
BUILTINS = {
    "doctor": "Check this machine's setup and say exactly what to fix",
    "config": "Show the state directory, every path and SOCIAL_FARM_* setting",
}
GROUPS = (
    ("Setup", ("doctor", "config", "login", "accounts")),
    ("Write and publish", ("brief", "outbox", "publish", "carousel", "memes", "vision")),
    ("Instagram", ("instagram-login", "instagram-import", "instagram-inbox", "instagram-mcp")),
    ("Threads", ("threads", "threads-setup", "threads-notifications")),
    ("LinkedIn", ("linkedin",)),
    ("Substack and MCP", ("substack-mcp", "mcp-bridge")),
)

# One table for optional extras, so doctor and the command gates can never disagree.
FEATURES = {
    "login": {"modules": ("playwright",), "purpose": "browser login wizard and portable Threads publishing"},
    "instagram": {"modules": ("instagrapi",), "purpose": "Instagram login, inbox, import and publishing"},
    "media": {"modules": ("PIL",), "purpose": "image checks for carousels and memes"},
    "browser": {"modules": ("scrapling",), "purpose": "Scrapling Threads reading transport"},
    "substack": {"modules": ("substack", "substack_mcp"), "purpose": "Substack login and substack-mcp"},
}
COMMAND_FEATURES = {"instagram-login": "instagram", "substack-mcp": "substack"}
OVERDUE_GRACE = timedelta(minutes=15)
DEFAULT_MIN_PYTHON = (3, 11)


def _all_commands() -> list[str]:
    return [*BUILTINS, *COMMANDS]


def print_help(stream=None) -> None:
    stream = stream or sys.stdout
    descriptions = {**BUILTINS, **{name: text for name, (_, text) in COMMANDS.items()}}
    lines = [
        "usage: ghost-in-the-box [--version] [-h] COMMAND [ARGS...]",
        "",
        "Grow a conversation. Your accounts, your voice, your tools.",
    ]
    for title, names in GROUPS:
        lines += ["", f"{title}:"]
        lines += [f"  {name:23} {descriptions[name]}" for name in names]
    lines += [
        "",
        "Options:",
        f"  {'-h, --help':23} Show this help",
        f"  {'--version':23} Print the installed version",
        "",
        "Run `ghost-in-the-box COMMAND --help` for that command's options.",
        "New here? Start with `ghost-in-the-box doctor`.",
    ]
    print("\n".join(lines), file=stream)


def missing_modules(feature: str) -> list[str]:
    return [name for name in FEATURES[feature]["modules"] if importlib.util.find_spec(name) is None]


# --- doctor -----------------------------------------------------------------

def _finding(check: str, severity: str, message: str, hint: str | None = None) -> dict:
    return {"check": check, "severity": severity, "message": message, "hint": hint}


def _login_command(platform_name: str, account: str) -> str:
    return f"ghost-in-the-box login start --platform {platform_name} --account {account}"


def _chmod_hint(path: Path, directory: bool = False) -> str:
    if os.name == "nt":
        return f'icacls "{path}" /inheritance:r /grant:r "%USERNAME%:(F)"'
    return f"chmod {'700' if directory else '600'} '{path}'"


def _min_python() -> tuple[int, int]:
    try:
        spec = importlib.metadata.metadata(config.PACKAGE).get("Requires-Python") or ""
    except importlib.metadata.PackageNotFoundError:
        return DEFAULT_MIN_PYTHON
    match = re.search(r">=\s*(\d+)\.(\d+)", spec)
    return (int(match[1]), int(match[2])) if match else DEFAULT_MIN_PYTHON


def _check_python() -> list[dict]:
    required = _min_python()
    current = sys.version_info[:2]
    version = platform.python_version()
    if current < required:
        return [_finding("python", "fail", f"Python {version} is older than the required "
                         f"{required[0]}.{required[1]}",
                         f"install Python {required[0]}.{required[1]} or newer and reinstall Ghost in the Box")]
    return [_finding("python", "ok", f"Python {version}")]


def _check_state_dir(root: Path) -> list[dict]:
    if not root.exists():
        return [_finding("state_dir", "warn", f"state directory {root} does not exist yet (first run); "
                         "commands create it when they first save something",
                         "ghost-in-the-box login start")]
    if not root.is_dir():
        return [_finding("state_dir", "fail", f"state path {root} is not a directory",
                         f"move {root} aside, or point {config.STATE_ENV} at a private directory")]
    if not os.access(root, os.W_OK | os.X_OK):
        return [_finding("state_dir", "fail", f"state directory {root} is not writable by this user",
                         _chmod_hint(root, directory=True))]
    if os.name != "nt" and root.stat().st_mode & 0o077:
        return [_finding("state_dir", "warn", f"state directory {root} is readable by other users",
                         _chmod_hint(root, directory=True))]
    return [_finding("state_dir", "ok", f"state directory {root}")]


def _read_registry(path: Path) -> tuple[list[dict], list[dict]]:
    """Read the registry without creating, locking or migrating anything."""
    if not path.exists():
        return [], [_finding("accounts", "warn", f"no accounts registered yet ({path})",
                             "ghost-in-the-box login start --platform PLATFORM --account HANDLE")]
    findings = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data["accounts"]
        if data.get("schema") != 1 or not isinstance(entries, dict):
            raise ValueError("schema")
        accounts = [dict(entry) for entry in entries.values() if isinstance(entry, dict)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return [], [_finding("accounts", "fail", f"account registry {path} cannot be read",
                             f"restore {path} from a backup, or move it aside and log in again")]
    if os.name != "nt" and path.stat().st_mode & 0o077:
        findings.append(_finding("accounts", "fail", f"account registry {path} is readable by other users",
                                 _chmod_hint(path)))
    if not accounts:
        findings.append(_finding("accounts", "warn", f"no accounts registered yet ({path})",
                                 "ghost-in-the-box login start --platform PLATFORM --account HANDLE"))
    return accounts, findings


def _check_accounts(accounts: list[dict]) -> list[dict]:
    findings = []
    for entry in sorted(accounts, key=lambda e: (str(e.get("platform")), str(e.get("account")))):
        name, handle = str(entry.get("platform")), str(entry.get("account"))
        label = f"{name}:{handle}"
        login = _login_command(name, handle)
        session = Path(str(entry.get("session_file", "")))
        try:
            info = session.stat()
        except OSError:
            findings.append(_finding("accounts", "fail", f"{label} session file is missing: {session}", login))
            continue
        wants_dir = entry.get("kind") == "browser_profile"
        if wants_dir != session.is_dir():
            findings.append(_finding("accounts", "fail", f"{label} session path has the wrong type: {session}",
                                     login))
            continue
        if os.name != "nt" and info.st_mode & 0o077:
            findings.append(_finding("accounts", "fail", f"{label} session is readable by other users: {session}",
                                     _chmod_hint(session, directory=wants_dir)))
            continue
        findings.append(_finding("accounts", "ok", f"{label} session {session}"))
    return findings


def _needed_features(accounts: list[dict]) -> dict[str, list[str]]:
    needed: dict[str, list[str]] = {}
    for entry in accounts:
        label = f"{entry.get('platform')}:{entry.get('account')}"
        kind, name = entry.get("kind"), entry.get("platform")
        if kind == "instagrapi_settings":
            needed.setdefault("instagram", []).append(label)
        if name == "threads" and kind == "cookie_jar":
            needed.setdefault("login", []).append(label)
        if name == "substack":
            needed.setdefault("substack", []).append(label)
    return needed


def _check_features(needed: dict[str, list[str]]) -> tuple[list[dict], dict[str, bool]]:
    findings, available = [], {}
    for feature, details in FEATURES.items():
        missing = missing_modules(feature)
        available[feature] = not missing
        purpose = details["purpose"]
        if not missing:
            findings.append(_finding(f"extra:{feature}", "ok", f"'{feature}' extra installed ({purpose})"))
            continue
        hint = config.install_command(feature)
        users = needed.get(feature)
        if users:
            findings.append(_finding(f"extra:{feature}", "fail",
                                     f"registered accounts {', '.join(sorted(users))} need the '{feature}' extra "
                                     f"(missing module {', '.join(missing)})", hint))
        elif feature == "login":
            findings.append(_finding(f"extra:{feature}", "warn",
                                     f"'login' extra is not installed, so `ghost-in-the-box login` cannot open "
                                     f"a browser ({purpose})",
                                     config.install_command("recommended") + " && " + config.chromium_install_command()))
        else:
            findings.append(_finding(f"extra:{feature}", "info",
                                     f"optional '{feature}' extra not installed ({purpose})", hint))
    return findings, available


def _playwright_browsers_root() -> Path | None:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured == "0":
        spec = importlib.util.find_spec("playwright")
        if spec is None or not spec.origin:
            return None
        return Path(spec.origin).parent / "driver" / "package" / ".local-browsers"
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def _expected_chromium_revision() -> str | None:
    spec = importlib.util.find_spec("playwright")
    if spec is None or not spec.origin:
        return None
    manifest = Path(spec.origin).parent / "driver" / "package" / "browsers.json"
    try:
        browsers = json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
        return next(str(b["revision"]) for b in browsers if b.get("name") == "chromium")
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return None


def bundled_chromium() -> Path | None:
    """The Chromium build this Playwright install launches by default, if installed."""
    root = _playwright_browsers_root()
    if root is None or not root.is_dir():
        return None
    revision = _expected_chromium_revision()
    pattern = f"chromium-{revision}" if revision else "chromium-*"
    for candidate in sorted(root.glob(pattern), reverse=True):
        if (candidate / "INSTALLATION_COMPLETE").exists():
            return candidate
    return None


def system_browsers() -> list[dict]:
    """Installed Chrome-family browsers the login wizard opens itself, in its preference order."""
    from farm.login import BROWSER_LABELS, installed_browsers
    return [{"browser": name, "label": BROWSER_LABELS[name], "path": path}
            for name, path in installed_browsers().items()]


def _check_browser(login_available: bool, needed: dict[str, list[str]]) -> list[dict]:
    if not login_available:
        return []
    from farm.login import BROWSER_LABELS, BUNDLED_LABEL, LoginError, choose_browser, login_browser_choice
    bundled = bundled_chromium()
    installed = system_browsers()
    portable = needed.get("login")
    if portable and bundled is None:
        return [_finding("browser", "fail", f"Playwright Chromium is not installed; portable Threads publishing "
                         f"for {', '.join(sorted(portable))} launches it headless",
                         config.chromium_install_command())]
    try:
        choice, source = login_browser_choice()
        name, executable = choose_browser(choice, {entry["browser"]: entry["path"] for entry in installed})
    except LoginError as exc:
        return [_finding("browser", "warn", f"login wizard browser: {exc.message}", exc.hint)]
    chosen = "" if source == "default" else f" ({source}={choice})"
    if executable:
        return [_finding("browser", "ok", f"login wizard opens {BROWSER_LABELS[name]}{chosen} at {executable} "
                         "with its own profile and attaches to it, so Google sign-in works")]
    if bundled is None:
        return [_finding("browser", "warn", "no browser for the login wizard: Chrome, Edge, Brave, Chromium and "
                         "Playwright Chromium were not found",
                         "install Google Chrome, or run: " + config.chromium_install_command())]
    return [_finding("browser", "info", f"login wizard launches {BUNDLED_LABEL} ({bundled.name}){chosen}; "
                     "Google sign-in may be blocked there",
                     "install Google Chrome to sign in with Google")]


def _check_login_profiles() -> list[dict]:
    """Which persistent login browser profiles exist; reads directory metadata only."""
    from farm.login import PROVIDERS
    families: dict[str, list[str]] = {}
    for name in PROVIDERS:
        families.setdefault(config.login_family(name), []).append(name)
    parts, loose = [], []
    for family, platforms in sorted(families.items()):
        profile = config.login_profiles_dir() / family
        label = family if platforms == [family] else f"{family} ({', '.join(sorted(platforms))})"
        exists = profile.is_dir()
        parts.append(f"{label} {'present' if exists else 'none'}")
        if exists and os.name != "nt" and profile.stat().st_mode & 0o077:
            loose.append(profile)
    findings = [_finding("login_profiles", "info", "login browser profiles: " + ", ".join(parts))]
    for profile in loose:
        findings.append(_finding("login_profiles", "warn",
                                 f"login browser profile {profile} is readable by other users; it holds a "
                                 "signed-in browser session", f"chmod 700 '{profile}'"))
    return findings


def _check_node() -> list[dict]:
    configured = os.environ.get("SOCIAL_FARM_NODE")
    if configured:
        path = Path(configured).expanduser()
        if not (path.is_file() and os.access(path, os.X_OK)):
            return [_finding("node", "fail", f"SOCIAL_FARM_NODE points to {path}, which is not an executable file",
                             "set SOCIAL_FARM_NODE to your node binary, or unset it to use node on PATH")]
        return [_finding("node", "ok", f"node {path} (SOCIAL_FARM_NODE)")]
    found = shutil.which("node")
    if not found:
        return [_finding("node", "info", "node is not on PATH; only the Threads browser bridges need it",
                         "install Node.js 18 or newer from https://nodejs.org, or run ghost-in-the-box threads-setup")]
    return [_finding("node", "ok", f"node {found}")]


def _check_timezones() -> list[dict]:
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        ZoneInfo("America/New_York")
    except (ImportError, ZoneInfoNotFoundError, ValueError, OSError):
        severity = "fail" if os.name == "nt" else "warn"
        return [_finding("timezones", severity, "no IANA time zone database is available "
                         "(scheduling and Substack connections use it)",
                         f"{config.python_command()} -m pip install tzdata")]
    return [_finding("timezones", "ok", "time zone database available")]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _outbox_path() -> Path:
    from farm import outbox
    return Path(os.environ.get("SOCIAL_FARM_OUTBOX_DB") or outbox.DEFAULT_DB).expanduser()


def _check_outbox(now: datetime) -> list[dict]:
    path = _outbox_path()
    if not path.exists():
        return [_finding("outbox", "ok", f"no outbox yet ({path}); `outbox draft` creates it")]
    findings = []
    if os.name != "nt" and path.stat().st_mode & 0o077:
        findings.append(_finding("outbox", "warn", f"outbox {path} is readable by other users", _chmod_hint(path)))
    try:
        # Read-only: doctor must never create tables, migrate or take write locks.
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT id, platform, account, status, scheduled_at, lease_until FROM jobs
                   WHERE status IN ('queued','in_flight','uncertain')
                   ORDER BY created_at""").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return findings + [_finding("outbox", "fail", f"outbox {path} cannot be read ({type(exc).__name__}: {exc})",
                                    f"stop other publishers and retry; if it persists, restore {path} from a backup")]
    problems = 0
    for row in rows:
        job, where = row["id"], f"@{row['account']} on {row['platform']}"
        resolve = (f"ghost-in-the-box outbox resolve {job} --outcome published --provider-id ID --reason TEXT "
                   f"(or --outcome failed --reason TEXT)")
        if row["status"] == "uncertain":
            problems += 1
            findings.append(_finding("outbox", "fail", f"job {job} for {where} is uncertain: it may or may not "
                                     "have posted, and it blocks that account's queue",
                                     f"check {where}, then: {resolve}"))
        elif row["status"] == "in_flight":
            lease = _parse_time(row["lease_until"])
            if lease is not None and lease <= now:
                problems += 1
                findings.append(_finding("outbox", "fail", f"job {job} for {where} is in flight with an expired lease; "
                                         "its publisher stopped before recording a receipt",
                                         f"check {where}; the next claim marks it uncertain, then: {resolve}"))
        else:
            scheduled = _parse_time(row["scheduled_at"])
            if scheduled is not None and scheduled <= now - OVERDUE_GRACE:
                problems += 1
                findings.append(_finding("outbox", "warn", f"job {job} for {where} has been due since "
                                         f"{row['scheduled_at']} and nothing published it",
                                         f"publish it: ghost-in-the-box publish {job}"))
    if not problems:
        findings.append(_finding("outbox", "ok", f"outbox {path}: {len(rows)} active job(s), none stuck"))
    return findings


def _check_credentials() -> list[dict]:
    path = config.credentials_path()
    if not path.exists():
        return [_finding("credentials", "info", f"no Instagram password file at {path} "
                         "(only `instagram-login` uses one; the login wizard does not)")]
    if path.is_symlink() or not path.is_file():
        return [_finding("credentials", "fail", f"Instagram credentials {path} must be a regular file",
                         f"replace {path} with a private JSON file holding username and password")]
    if os.name != "nt" and path.stat().st_mode & 0o077:
        return [_finding("credentials", "fail", f"Instagram credentials {path} are readable by other users",
                         _chmod_hint(path))]
    return [_finding("credentials", "ok", f"Instagram credentials {path}")]


def _check_environment() -> list[dict]:
    findings = []
    for row in config.environment_report():
        if row.get("unknown"):
            close = difflib.get_close_matches(row["name"], config.KNOWN_ENVIRONMENT, n=1)
            hint = f"did you mean {close[0]}?" if close else "unset it if it is not meant for another tool"
            findings.append(_finding("environment", "warn", f"{row['name']} is set but not used", hint))
    return findings


def doctor(now: datetime | None = None) -> dict:
    """Inspect local prerequisites without loading sessions or contacting providers."""
    now = now or datetime.now(timezone.utc)
    root = STATE_ROOT
    findings: list[dict] = []
    registry_path = Path(os.environ.get("SOCIAL_FARM_ACCOUNTS_FILE") or
                         _default_registry()).expanduser()
    checks = (
        ("python", _check_python),
        ("state_dir", lambda: _check_state_dir(root)),
    )
    for name, check in checks:
        findings += _guard(name, check)
    accounts: list[dict] = []
    try:
        accounts, registry_findings = _read_registry(registry_path)
        findings += registry_findings
    except Exception as exc:
        findings.append(_finding("accounts", "fail", f"account check crashed: {type(exc).__name__}",
                                 "report this with the output of ghost-in-the-box config"))
    findings += _guard("accounts", lambda: _check_accounts(accounts))
    needed = _needed_features(accounts)
    available: dict[str, bool] = {}
    try:
        feature_findings, available = _check_features(needed)
        findings += feature_findings
    except Exception as exc:
        findings.append(_finding("extras", "fail", f"extras check crashed: {type(exc).__name__}",
                                 "report this with the output of ghost-in-the-box config"))
    findings += _guard("browser", lambda: _check_browser(available.get("login", False), needed))
    findings += _guard("login_profiles", _check_login_profiles)
    findings += _guard("node", _check_node)
    findings += _guard("timezones", _check_timezones)
    findings += _guard("outbox", lambda: _check_outbox(now))
    findings += _guard("credentials", _check_credentials)
    findings += _guard("environment", _check_environment)

    failures = [f for f in findings if f["severity"] == "fail"]
    warnings = [f for f in findings if f["severity"] == "warn"]
    return {
        "ok": not failures,
        "verdict": "problems" if failures else "warnings" if warnings else "ok",
        "problems": failures + warnings,
        "checks": findings,
        "python": platform.python_version(),
        "platform": sys.platform,
        "version": config.package_version(),
        "state_directory": str(root),
        "state_exists": root.is_dir(),
        "optional_modules": {name: importlib.util.find_spec(name) is not None
                             for name in ("instagrapi", "PIL", "playwright", "scrapling", "substack",
                                          "substack_mcp")},
        "optional_commands": {name: shutil.which(name) is not None
                              for name in ("node", "npm", "tesseract", "xvfb-run")},
        "network_requests": 0,
    }


def _default_registry() -> Path:
    from farm.accounts import DEFAULT_REGISTRY
    return DEFAULT_REGISTRY


def _guard(name: str, check) -> list[dict]:
    """A crashing check is itself a problem; doctor never exits 0 on an exception."""
    try:
        return check()
    except Exception as exc:
        return [_finding(name, "fail", f"{name} check crashed: {type(exc).__name__}: {exc}",
                         "report this with the output of ghost-in-the-box config")]


def render_doctor(report: dict, stream=None) -> None:
    stream = stream or sys.stdout
    failures = sum(f["severity"] == "fail" for f in report["checks"])
    warnings = sum(f["severity"] == "warn" for f in report["checks"])
    if report["verdict"] == "ok":
        headline = "everything looks ready"
    else:
        parts = ([f"{failures} problem{'s' * (failures != 1)}"] if failures else []) + \
                ([f"{warnings} warning{'s' * (warnings != 1)}"] if warnings else [])
        headline = ", ".join(parts)
    print(f"ghost-in-the-box doctor {report['version']}: {headline}", file=stream)
    print(f"state directory: {report['state_directory']}", file=stream)
    labels = {"fail": "FAIL", "warn": "WARN", "info": "  --", "ok": "  ok"}
    order = {"fail": 0, "warn": 1, "info": 2, "ok": 3}
    for finding in sorted(report["checks"], key=lambda f: order[f["severity"]]):
        print(f"{labels[finding['severity']]}  {finding['message']}", file=stream)
        if finding["hint"] and finding["severity"] != "ok":
            print(f"      next: {finding['hint']}", file=stream)
    print("no network requests were made", file=stream)


def run_doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ghost-in-the-box doctor",
        description="Check Python, optional extras, browsers, the state directory, registered account "
                    "sessions and stuck outbox jobs. Reads local files only; never opens a session or "
                    "contacts a provider.",
        epilog="Exit status: 0 ready, 1 problems found (with --strict, warnings count as problems).")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    args = parser.parse_args(argv)
    report = doctor()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        render_doctor(report)
    if not report["ok"] or (args.strict and report["verdict"] != "ok"):
        return 1
    return 0


# --- config -----------------------------------------------------------------

def render_config(report: dict, stream=None) -> None:
    stream = stream or sys.stdout
    state = report["state_dir"]
    print(f"state directory: {state['path']}  ({state['source']}"
          f"{'' if state['exists'] else ', not created yet'})", file=stream)
    print("\npaths:", file=stream)
    for row in report["paths"]:
        source = row["source"]
        print(f"  {row['name']:25} {row['path']}  [{source}{'' if row['exists'] else ', missing'}]",
              file=stream)
    print("\nenvironment:", file=stream)
    for row in report["environment"]:
        if row.get("unknown"):
            print(f"  {row['name']:36} set      {row['value']}  (unknown: not used by Ghost in the Box)",
                  file=stream)
        elif row["set"]:
            print(f"  {row['name']:36} set      {row['value']}", file=stream)
        else:
            print(f"  {row['name']:36} default  {row['default']}", file=stream)


def run_config(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ghost-in-the-box config",
        description="Show where Ghost in the Box keeps its files and which SOCIAL_FARM_* settings apply.")
    sub = parser.add_subparsers(dest="action")
    show = sub.add_parser("show", help="effective state directory, every path and environment setting (default)")
    show.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    report = config.config_report()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        render_config(report)
    return 0


# --- dispatcher -------------------------------------------------------------

def _unknown_command(command: str) -> int:
    names = _all_commands()
    close = difflib.get_close_matches(command, names, n=1, cutoff=0.6)
    if not close:
        close = [name for name in names if name.startswith(command) or command in name][:1]
    print(f"ghost-in-the-box: unknown command '{command}'", file=sys.stderr)
    if close:
        print(f"Did you mean: ghost-in-the-box {close[0]}", file=sys.stderr)
    print("Run `ghost-in-the-box --help` to see every command.", file=sys.stderr)
    return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print_help()
        return 0
    if argv[0] == "--version":
        print(f"ghost-in-the-box {config.package_version()}")
        return 0
    command = argv[0]
    if command == "help":
        if len(argv) == 1 or argv[1] in {"-h", "--help"}:
            print_help()
            return 0
        command, argv = argv[1], [argv[1], "--help"]
    if command.startswith("-"):
        print(f"ghost-in-the-box: unknown option '{command}'", file=sys.stderr)
        print("Options go after the command: ghost-in-the-box COMMAND --help", file=sys.stderr)
        return 2
    if command == "doctor":
        return run_doctor(argv[1:])
    if command == "config":
        return run_config(argv[1:])
    if command not in COMMANDS:
        return _unknown_command(command)
    feature = COMMAND_FEATURES.get(command)
    wants_help = any(arg in ("-h", "--help") for arg in argv[1:])
    if feature and not wants_help and missing_modules(feature):
        print(f"ghost-in-the-box {command} needs the optional '{feature}' extra "
              f"(missing module {', '.join(missing_modules(feature))}).", file=sys.stderr)
        print(f"Install it: {config.install_command(feature)}", file=sys.stderr)
        return 4
    previous = sys.argv
    try:
        sys.argv = ["ghost-in-the-box " + command, *argv[1:]]
        runpy.run_module(COMMANDS[command][0], run_name="__main__")
    finally:
        sys.argv = previous
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
