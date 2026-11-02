"""Shared local state locations and settings; existing installations keep their paths.

STATE_ROOT is fixed at import time, like the module constants that already bind
it. The helpers below build every other location from the current STATE_ROOT and
read their own environment overrides when called, matching how the outbox and
account registry resolve SOCIAL_FARM_OUTBOX_DB and SOCIAL_FARM_ACCOUNTS_FILE.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
from urllib.parse import urlsplit, urlunsplit

PACKAGE = "ghost-in-the-box"
STATE_ENV = "SOCIAL_FARM_STATE_DIR"
LEGACY_HOME = Path.home() / ".batbox"
DEFAULT_STATE_ROOT = LEGACY_HOME / "social-farm-state"

STATE_ROOT = Path(os.environ.get(STATE_ENV) or DEFAULT_STATE_ROOT).expanduser()

# Locations that historically lived beside the default state directory.
LEGACY_CREDENTIALS = LEGACY_HOME / "social-farm-creds" / "ig.json"
LEGACY_MEMES = LEGACY_HOME / "media-reference" / "tech-memes"


def _root(root: str | Path | None) -> Path:
    return Path(root).expanduser() if root is not None else STATE_ROOT


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _custom_root(root: Path) -> bool:
    return root.expanduser().resolve() != DEFAULT_STATE_ROOT.expanduser().resolve()


def _state_or_legacy(env: str, relative: str, legacy: Path, root: str | Path | None) -> tuple[Path, str]:
    """Env override, then the state directory, keeping a legacy file that already exists."""
    override = _env_path(env)
    if override is not None:
        return override, env
    base = _root(root)
    if not _custom_root(base):
        return legacy, "default"
    inside = base / relative
    if not inside.exists() and legacy.exists():
        # Someone who moved their state directory keeps their existing private files.
        return legacy, "legacy"
    return inside, STATE_ENV if os.environ.get(STATE_ENV) else "state directory"


def state_root() -> Path:
    return STATE_ROOT


def state_path(*parts: str, root: str | Path | None = None) -> Path:
    """A location inside the state directory."""
    return _root(root).joinpath(*parts)


def accounts_file(root: str | Path | None = None) -> Path:
    return _env_path("SOCIAL_FARM_ACCOUNTS_FILE") or state_path("accounts.json", root=root)


def outbox_db(root: str | Path | None = None) -> Path:
    return _env_path("SOCIAL_FARM_OUTBOX_DB") or state_path("outbox.sqlite3", root=root)


def inbox_db(root: str | Path | None = None) -> Path:
    return state_path("inbox.sqlite3", root=root)


def instagram_sessions_dir(root: str | Path | None = None) -> Path:
    return state_path("ig", root=root)


def instagram_references_dir(root: str | Path | None = None) -> Path:
    return state_path("instagram-references", root=root)


def carousels_dir(root: str | Path | None = None) -> Path:
    return state_path("carousels", root=root)


def substack_config(root: str | Path | None = None) -> Path:
    return _env_path("SOCIAL_FARM_SUBSTACK_CONFIG") or state_path("substack", "connection.json", root=root)


def threads_auth_dir(root: str | Path | None = None) -> Path:
    return state_path("threads-auth", root=root)


def threads_cadence_dir(root: str | Path | None = None) -> Path:
    return state_path("threads-cadence", root=root)


def threads_scrapling_dir(root: str | Path | None = None) -> Path:
    return state_path("threads-scrapling", root=root)


def threads_browser_dir(root: str | Path | None = None) -> Path:
    return state_path("threads-browser-fallback", root=root)


def threads_portable_dir(root: str | Path | None = None) -> Path:
    return state_path("threads-portable", root=root)


def threads_mcp_source(root: str | Path | None = None) -> Path:
    return _env_path("SOCIAL_FARM_THREADS_MCP_SOURCE") or threads_browser_dir(root) / "source"


def threads_work_dir(root: str | Path | None = None) -> Path:
    """Working files for the Threads agent, formerly loose in $HOME."""
    return _env_path("SOCIAL_FARM_THREADS_WORK_DIR") or state_path("threads-work", root=root)


def threads_post_marker(root: str | Path | None = None) -> Path:
    """File whose mtime records the latest Threads post (was $HOME/threadspost)."""
    return threads_work_dir(root) / "threadspost"


# Instagram and Threads are one Meta login, so they share one wizard browser profile.
LOGIN_FAMILIES = {"instagram": "meta", "threads": "meta"}


def login_family(platform: str) -> str:
    """The login profile name a platform signs in through."""
    if not isinstance(platform, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", platform):
        raise ValueError("platform must be a lowercase provider name such as linkedin")
    return LOGIN_FAMILIES.get(platform, platform)


def login_profiles_dir(root: str | Path | None = None) -> Path:
    """Parent of the login wizard's persistent browser profiles."""
    return state_path("login-profiles", root=root)


def login_profile_dir(platform: str, root: str | Path | None = None) -> Path:
    """The persistent browser profile the login wizard uses for a platform."""
    return login_profiles_dir(root) / login_family(platform)


def credentials_path(root: str | Path | None = None) -> Path:
    """Private Instagram username/password JSON used by `instagram-login`."""
    return _state_or_legacy("SOCIAL_FARM_IG_CREDENTIALS", "credentials/ig.json", LEGACY_CREDENTIALS, root)[0]


def memes_root(root: str | Path | None = None) -> Path:
    """Local visual reference library used by `memes`."""
    return _state_or_legacy("SOCIAL_FARM_MEMES_DIR", "media-reference/tech-memes", LEGACY_MEMES, root)[0]


def node_executable() -> str | None:
    configured = os.environ.get("SOCIAL_FARM_NODE")
    if configured:
        return configured
    return shutil.which("node")


# name, description, default (text), used by
ENVIRONMENT = (
    ("SOCIAL_FARM_STATE_DIR", "State directory for sessions, registry, outbox and inbox",
     str(DEFAULT_STATE_ROOT), "every command"),
    ("SOCIAL_FARM_ACCOUNTS_FILE", "Account registry file", "<state>/accounts.json",
     "accounts, login, publish"),
    ("SOCIAL_FARM_OUTBOX_DB", "Outbox SQLite database", "<state>/outbox.sqlite3", "outbox, publish"),
    ("SOCIAL_FARM_SUBSTACK_CONFIG", "Substack connection file", "<state>/substack/connection.json",
     "substack-mcp"),
    ("SOCIAL_FARM_IG_CREDENTIALS", "Private Instagram username/password JSON",
     f"{LEGACY_CREDENTIALS} (or <state>/credentials/ig.json with a custom state dir)",
     "instagram-login, threads"),
    ("SOCIAL_FARM_MEMES_DIR", "Visual reference library",
     f"{LEGACY_MEMES} (or <state>/media-reference/tech-memes with a custom state dir)", "memes"),
    ("SOCIAL_FARM_THREADS_WORK_DIR", "Threads agent working files", "<state>/threads-work", "threads"),
    ("SOCIAL_FARM_THREADS_MCP_SOURCE", "Threads browser bridge sources",
     "<state>/threads-browser-fallback/source", "threads, threads-setup"),
    ("SOCIAL_FARM_NODE", "Node.js executable for the Threads browser bridges", "node on PATH",
     "threads, threads-setup"),
    ("SOCIAL_FARM_SCRAPLING_PYTHON", "Python interpreter for the Scrapling Threads transport",
     "the running Python", "threads"),
    ("SOCIAL_FARM_THREADS_SCRAPLING_STATE", "Scrapling bridge state (set internally for subprocesses)",
     "<state>/threads-scrapling", "threads"),
    ("SOCIAL_FARM_LOGIN_BROWSER", "Browser the login wizard opens: auto, chrome, msedge, brave or chromium",
     "auto (Chrome, Edge, Brave, Chromium, then Playwright Chromium)", "login"),
    ("SOCIAL_FARM_IG_ACCOUNT", "Default account for the Instagram MCP server", "none", "instagram-mcp"),
    ("SOCIAL_FARM_IG_PYTHON", "Python interpreter with instagrapi installed", "the running Python",
     "instagram-mcp, publish"),
    ("SOCIAL_FARM_VISION_URL", "OpenAI-compatible vision endpoint",
     "http://127.0.0.1:8001/v1/chat/completions", "vision"),
    ("SOCIAL_FARM_VISION_MODEL", "Vision model name", "your-vision-model", "vision"),
    ("SOCIAL_FARM_CHAT_PATH", "Optional notification delivery path", "none", "threads-notifications"),
    ("SOCIAL_FARM_THREADS_NOTIFY_AGENT", "Durable worker recipient for Threads notifications", "none",
     "threads-notifications"),
    ("SOCIAL_FARM_ACCOUNT", "Account used by the Threads publish systemd unit", "none", "systemd units"),
)
KNOWN_ENVIRONMENT = frozenset(name for name, *_ in ENVIRONMENT)


def _display_value(value: str) -> str:
    """Never echo credentials embedded in a URL."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.scheme and parts.netloc and (parts.username or parts.password):
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, "***@" + host, parts.path, parts.query, parts.fragment))
    return value


def environment_report() -> list[dict]:
    """Every SOCIAL_FARM_* setting with its current value, default and whether it is set."""
    rows = []
    for name, description, default, used_by in ENVIRONMENT:
        value = os.environ.get(name)
        rows.append({"name": name, "set": bool(value), "value": _display_value(value) if value else None,
                     "default": default, "description": description, "used_by": used_by})
    for name in sorted(os.environ):
        if name.startswith("SOCIAL_FARM_") and name not in KNOWN_ENVIRONMENT:
            rows.append({"name": name, "set": True, "value": _display_value(os.environ[name]),
                         "default": None, "description": "not used by Ghost in the Box (check the spelling)",
                         "used_by": None, "unknown": True})
    return rows


def _path_row(label: str, path: Path, env: str | None, source: str | None = None) -> dict:
    if source is None:
        source = env if env and os.environ.get(env) else (
            STATE_ENV if os.environ.get(STATE_ENV) else "default")
    return {"name": label, "path": str(path), "env": env, "source": source, "exists": path.exists()}


def paths_report() -> list[dict]:
    credentials, credentials_source = _state_or_legacy(
        "SOCIAL_FARM_IG_CREDENTIALS", "credentials/ig.json", LEGACY_CREDENTIALS, None)
    memes, memes_source = _state_or_legacy(
        "SOCIAL_FARM_MEMES_DIR", "media-reference/tech-memes", LEGACY_MEMES, None)
    return [
        _path_row("accounts registry", accounts_file(), "SOCIAL_FARM_ACCOUNTS_FILE"),
        _path_row("outbox database", outbox_db(), "SOCIAL_FARM_OUTBOX_DB"),
        _path_row("inbox database", inbox_db(), None),
        _path_row("instagram sessions", instagram_sessions_dir(), None),
        _path_row("instagram credentials", credentials, "SOCIAL_FARM_IG_CREDENTIALS", credentials_source),
        _path_row("instagram references", instagram_references_dir(), None),
        _path_row("carousels", carousels_dir(), None),
        _path_row("memes library", memes, "SOCIAL_FARM_MEMES_DIR", memes_source),
        _path_row("substack connection", substack_config(), "SOCIAL_FARM_SUBSTACK_CONFIG"),
        _path_row("threads work dir", threads_work_dir(), "SOCIAL_FARM_THREADS_WORK_DIR"),
        _path_row("threads auth", threads_auth_dir(), None),
        _path_row("threads cadence", threads_cadence_dir(), None),
        _path_row("threads scrapling state", threads_scrapling_dir(), None),
        _path_row("threads browser state", threads_browser_dir(), None),
        _path_row("threads browser sources", threads_mcp_source(), "SOCIAL_FARM_THREADS_MCP_SOURCE"),
        _path_row("threads portable state", threads_portable_dir(), None),
        _path_row("login browser profiles", login_profiles_dir(), None),
    ]


def config_report() -> dict:
    root = STATE_ROOT
    return {
        "state_dir": {"path": str(root), "source": STATE_ENV if os.environ.get(STATE_ENV) else "default",
                      "exists": root.is_dir()},
        "paths": paths_report(),
        "environment": environment_report(),
    }


def package_version() -> str:
    """Installed distribution version, falling back to the checkout's pyproject.toml.

    The last resort is a valid PEP 440 local version, so MCP clients and bug
    reports always get something parseable.
    """
    try:
        return importlib.metadata.version(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        pass
    import tomllib
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        with pyproject.open("rb") as handle:
            version = tomllib.load(handle)["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return "0+unknown"
    return version if isinstance(version, str) and version else "0+unknown"


def _editable_checkout() -> Path | None:
    # A checkout that was ever pip-installed keeps a stale *.egg-info at its root,
    # which shadows the real install whenever the checkout is on sys.path, so I
    # look at every matching distribution, not just the first.
    distributions = list(importlib.metadata.distributions(name=PACKAGE))
    if not distributions:
        # Running from a source tree without an install.
        return Path(__file__).resolve().parents[1]
    for distribution in distributions:
        raw = distribution.read_text("direct_url.json")
        if raw:
            return _checkout_from_direct_url(raw)
    for distribution in distributions:
        root = Path(distribution.locate_file(""))
        if (root / "pyproject.toml").is_file() and (root / "farm" / "cli.py").is_file():
            return root.resolve()
    return None


def _checkout_from_direct_url(raw: str) -> Path | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not (data.get("dir_info") or {}).get("editable"):
        return None
    url = data.get("url", "")
    if not url.startswith("file://"):
        return None
    from urllib.request import url2pathname
    return Path(url2pathname(urlsplit(url).path))


def _quote(value: str) -> str:
    return f'"{value}"' if os.name == "nt" else shlex.quote(value)


def python_command() -> str:
    """The running interpreter, quoted for the user's shell."""
    return _quote(sys.executable)


def install_command(extras: str) -> str:
    """The exact pip command that adds optional extras to this installation."""
    checkout = _editable_checkout()
    if checkout is not None:
        return f"{python_command()} -m pip install -e {_quote(f'{checkout}[{extras}]')}"
    return f"{python_command()} -m pip install {_quote(f'{PACKAGE}[{extras}]')}"


def chromium_install_command() -> str:
    return f"{python_command()} -m playwright install chromium"
