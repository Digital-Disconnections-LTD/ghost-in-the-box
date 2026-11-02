"""Choose the Threads transport for one account and say why.

Five transports exist. The registration kind is the main switch:

* ``portable``: ``cookie_jar`` from ``ghost-in-the-box login start --platform threads``,
  driven by Playwright Chromium.
* ``scrapling``: a verified Scrapling browser profile made by ``threads connect``
  from a linked ``instagrapi_settings`` Instagram session.
* ``mobile``: a saved Threads mobile bearer (``threads mobile-login``) plus the
  linked Instagram session.
* ``browser_node``: the pinned threads-mcp Node browser from ``threads-setup``.
* the reader (``farm.threads_reader``) is not a separate login: it tries the
  mobile reads, then whichever browser transport above is usable.

``resolve`` is local only: it reads the registry and local files and checks
installed modules, never a provider.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib.util
import os
from pathlib import Path
import platform as _platform
import shlex
import shutil
import subprocess
import sys

from farm.adapters import DependencyUnavailable
from farm.adapters.threads import ThreadsReadFailure
from farm.config import (chromium_install_command, install_command, python_command,
                         threads_browser_dir)

LOGIN_INSTALL = f"{install_command('login')} && {chromium_install_command()}"
CHROMIUM_INSTALL = chromium_install_command()
LOGIN_COMMAND = "ghost-in-the-box login start --platform threads --account {account}"
CONNECT_COMMAND = "ghost-in-the-box threads --account {account} connect"
SETUP_COMMAND = "ghost-in-the-box threads-setup --account {account}"
NODE_INSTALL = ("install Node.js 24 with npm, set SOCIAL_FARM_NODE to its node binary, then "
                + SETUP_COMMAND)
XVFB_INSTALL = "install xvfb-run (Debian/Ubuntu: sudo apt-get install xvfb) or run with a DISPLAY"
PURPOSES = ("agent", "publish_job", "read")
NODE_FALLBACK = "node-v24.22.1-linux-x64/bin/node"


class ThreadsDependencyUnavailable(DependencyUnavailable, ThreadsReadFailure):
    """A local package, browser or runtime is missing; nothing was sent.

    The shared DependencyUnavailable makes exact-job publishers release the job
    with this install hint; ThreadsReadFailure keeps read callers' handling.
    """

    def __init__(self, dependency: str, install: str, message: str = "dependency_unavailable"):
        self.dependency = dependency
        self.install = install
        super().__init__(message, install_hint=install)


@dataclass(frozen=True)
class Transport:
    name: str
    ready: bool
    reason: str
    registration_kind: str | None
    live_identity_check: bool = True
    dependency: str | None = None
    install: str | None = None
    next_step: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def scrapling_install(python) -> str:
    """pip command for the browser extra in the interpreter that runs the bridge."""
    command = install_command("browser")
    if not _same_interpreter(python):
        command = command.replace(python_command(), shlex.quote(str(python)), 1)
    return f"{command} && scrapling install"


def _same_interpreter(python) -> bool:
    # A venv python is often a symlink to the system binary; compare the paths
    # as given so the system interpreter is not mistaken for the venv.
    return Path(python).absolute() == Path(sys.executable).absolute()


def module_available(name: str, python=None) -> bool:
    """True when `name` is importable by `python` (default: this interpreter)."""
    if python is None or _same_interpreter(python):
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False
    executable = Path(python)
    if not executable.is_file():
        return False
    probe = ("import importlib.util, sys; "
             "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)")
    try:
        response = subprocess.run([str(executable), "-c", probe, name],
                                  capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return response.returncode == 0


def mobile_live_identity_check() -> bool:
    """Whether the mobile publisher asks the server who the bearer belongs to."""
    from farm.adapters.threads import ThreadsAdapter
    return getattr(ThreadsAdapter, "LIVE_IDENTITY_CHECK", False) is True


def node_paths(browser_state_dir: Path, *, which=None) -> tuple[Path, Path]:
    which = which or shutil.which
    node = Path(os.environ.get("SOCIAL_FARM_NODE") or which("node") or
                Path(browser_state_dir) / NODE_FALLBACK)
    source = Path(os.environ.get("SOCIAL_FARM_THREADS_MCP_SOURCE") or
                  Path(browser_state_dir) / "source")
    return node.expanduser().absolute(), source.expanduser().absolute()


def display_available(*, which=None) -> bool:
    which = which or shutil.which
    return bool(which("xvfb-run") or sys.platform == "darwin" or os.name == "nt" or
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _linux_x64() -> bool:
    return sys.platform == "linux" and _platform.machine().lower() in {"x86_64", "amd64"}


def _portable(account, threads, available) -> Transport:
    kind = "cookie_jar"
    if not threads["session_exists"] or not threads["session_private"]:
        return Transport("portable", False,
                         "the saved Threads login file is missing or readable by other users",
                         kind, next_step=LOGIN_COMMAND.format(account=account))
    if not available("playwright"):
        return Transport("portable", False,
                         "the login-wizard registration needs Playwright, which is not installed",
                         kind, dependency="playwright", install=LOGIN_INSTALL)
    return Transport("portable", True,
                     "cookie_jar registration from the login wizard drives a local "
                     "Playwright Chromium with the saved cookies", kind)


def _scrapling(account, state_dir, python, available) -> Transport:
    from farm.threads_scrapling import verified_profile
    kind = "instagrapi_settings"
    if not verified_profile(account, state_dir):
        return Transport("scrapling", False,
                         "no verified Scrapling browser profile exists for this linked account",
                         kind, next_step=CONNECT_COMMAND.format(account=account))
    if not Path(python).is_file():
        return Transport("scrapling", False,
                         "the Python set by SOCIAL_FARM_SCRAPLING_PYTHON does not exist",
                         kind, dependency="python",
                         install="set SOCIAL_FARM_SCRAPLING_PYTHON to a Python with the browser extra")
    if not available("scrapling", python):
        return Transport("scrapling", False,
                         "the verified browser profile needs Scrapling, which is not installed",
                         kind, dependency="scrapling", install=scrapling_install(python))
    return Transport("scrapling", True,
                     "a verified Scrapling browser profile exists for the linked Instagram session",
                     kind)


def _mobile(account, threads, auth_dir, *, purpose) -> Transport:
    from farm.threads_auth import ThreadsAuthError, load_token
    kind = "instagrapi_settings"
    live = mobile_live_identity_check()
    if not threads["session_exists"] or not threads["session_private"]:
        return Transport("mobile", False,
                         "the linked Instagram session file is missing or readable by other users",
                         kind, live, next_step=CONNECT_COMMAND.format(account=account))
    if purpose == "read":
        return Transport("mobile", True,
                         "mobile reads use the linked private Instagram session", kind, live)
    try:
        load_token(account, auth_dir)
    except ThreadsAuthError:
        return Transport("mobile", False, "no saved Threads mobile bearer for this account",
                         kind, live,
                         next_step=f"ghost-in-the-box threads --account {account} mobile-login")
    reason = "a saved Threads mobile bearer matches the linked Instagram session"
    if not live:
        reason += "; the server is not asked who the bearer belongs to before publishing"
    return Transport("mobile", True, reason, kind, live)


def _browser_node(account, browser_state_dir, which) -> Transport:
    kind = "instagrapi_settings"
    node, source = node_paths(browser_state_dir, which=which)
    if not node.is_file():
        return Transport("browser_node", False, "the Node browser transport needs Node.js",
                         kind, dependency="node",
                         install=(SETUP_COMMAND if _linux_x64() else NODE_INSTALL).format(account=account))
    if not (source / "build/browser/session.js").is_file():
        return Transport("browser_node", False,
                         "the pinned threads-mcp browser source is not set up",
                         kind, dependency="threads-mcp",
                         install=SETUP_COMMAND.format(account=account))
    if not display_available(which=which):
        return Transport("browser_node", False,
                         "the Node browser needs a display or xvfb-run on this machine",
                         kind, dependency="xvfb-run", install=XVFB_INSTALL)
    return Transport("browser_node", True,
                     "the pinned threads-mcp Node browser is set up for the linked session", kind)


def resolve(account: str, *, registry, purpose: str = "agent",
            scrapling_state_dir: Path | None = None,
            browser_state_dir: Path | None = None,
            auth_dir: Path | None = None, python=None,
            available=module_available, which=None) -> Transport:
    """Return the transport `purpose` will use for `account`, or why none can run.

    purpose "agent": the `ghost-in-the-box threads` commands (portable or scrapling,
    the transports with exact receipt reconciliation).
    purpose "publish_job": `ghost-in-the-box publish JOB` (mobile first, then the
    browser transports, as scripts/social_publish_job.py tries them).
    purpose "read": `threads-notifications` (mobile reads, then a browser).
    """
    if purpose not in PURPOSES:
        raise ValueError("unknown transport purpose")
    from farm.threads_auth import DEFAULT_AUTH_DIR
    from farm.threads_scrapling import BASE as SCRAPLING_BASE, PYTHON as SCRAPLING_PYTHON
    scrapling_state_dir = Path(scrapling_state_dir or SCRAPLING_BASE)
    browser_state_dir = Path(browser_state_dir or threads_browser_dir())
    auth_dir = Path(auth_dir or DEFAULT_AUTH_DIR)
    python = Path(python or SCRAPLING_PYTHON)

    threads = registry.status("threads", account)
    if not threads:
        return Transport("none", False, f"no Threads registration exists for @{account}", None,
                         next_step=LOGIN_COMMAND.format(account=account))
    kind = threads["kind"]
    if kind == "cookie_jar":
        return _portable(account, threads, available)
    if kind != "instagrapi_settings":
        return Transport("none", False,
                         f"Threads registration kind '{kind}' is not supported; use the login "
                         "wizard (cookie_jar) or a linked Instagram session (instagrapi_settings)",
                         kind, next_step=LOGIN_COMMAND.format(account=account))
    instagram = registry.status("instagram", account)
    if (not instagram or instagram["kind"] != "instagrapi_settings" or
            instagram["account_key"] != threads["account_key"] or
            instagram["session_file"] != threads["session_file"]):
        return Transport("none", False,
                         "the instagrapi_settings Threads registration is not linked to a "
                         "matching Instagram registration (same session file and account key)",
                         kind, next_step=CONNECT_COMMAND.format(account=account))

    if purpose == "agent":
        return _scrapling(account, scrapling_state_dir, python, available)
    candidates = [
        _mobile(account, threads, auth_dir, purpose=purpose),
        _scrapling(account, scrapling_state_dir, python, available),
        _browser_node(account, browser_state_dir, which),
    ]
    for candidate in candidates:
        if candidate.ready:
            return candidate
    # Prefer the verified browser profile's problem: it is the usual route.
    scrapling = candidates[1]
    blocked = scrapling if scrapling.dependency else next(
        (item for item in candidates if item.dependency), scrapling)
    summary = "; ".join(f"{item.name}: {item.reason}" for item in candidates)
    return Transport("none", False, f"no usable transport ({summary})", kind,
                     dependency=blocked.dependency, install=blocked.install,
                     next_step=blocked.next_step or CONNECT_COMMAND.format(account=account))
