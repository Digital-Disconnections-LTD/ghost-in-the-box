#!/usr/bin/env python3
"""Install the optional pinned Threads browser fallback in private local state."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from farm.config import STATE_ROOT
from farm.accounts import AccountRegistry
from farm.cli_output import EXIT_AUTH, EXIT_RUNTIME, EXIT_USAGE, attach_error, guarded
from farm.threads_transport import NODE_INSTALL


BASE = STATE_ROOT / 'threads-browser-fallback'
NODE_VERSION = "v24.22.1"
NODE_TARBALL = f"node-{NODE_VERSION}-linux-x64.tar.xz"
NODE_SHA256 = "6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6"
SOURCE_URL = "https://github.com/bintangtimurlangit/threads-mcp.git"
SOURCE_REVISION = "2d22286ef94a31e699c0a7477f450f4fe48009c7"


def _run(argv, *, env=None, cwd=None, timeout=180):
    result = subprocess.run(argv, env=env, cwd=cwd, capture_output=True,
                            text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("setup_step_failed")


def _node() -> Path:
    installed = os.environ.get("SOCIAL_FARM_NODE") or shutil.which("node")
    if installed:
        executable = Path(installed).expanduser()
        if not executable.is_file() or not (executable.parent / "npm").is_file():
            raise RuntimeError("node_and_npm_required")
        return executable
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("install_node_and_npm_for_this_platform")
    directory = BASE / f"node-{NODE_VERSION}-linux-x64"
    executable = directory / "bin/node"
    if executable.is_file():
        return executable
    url = f"https://nodejs.org/dist/{NODE_VERSION}/{NODE_TARBALL}"
    with tempfile.NamedTemporaryFile(prefix=".node-", suffix=".tar.xz",
                                     dir=BASE, delete=False) as handle:
        archive = Path(handle.name)
        digest = hashlib.sha256()
        try:
            with urlopen(url, timeout=30) as response:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    handle.write(chunk)
        except Exception:
            archive.unlink(missing_ok=True)
            raise
    try:
        if digest.hexdigest() != NODE_SHA256:
            raise RuntimeError("node_checksum_mismatch")
        with tarfile.open(archive, mode="r:xz") as tar:
            tar.extractall(BASE, filter="data")
    finally:
        archive.unlink(missing_ok=True)
    if not executable.is_file():
        raise RuntimeError("node_install_incomplete")
    return executable


def _source(node: Path) -> Path:
    source = BASE / "source"
    if not source.exists():
        _run(["git", "clone", SOURCE_URL, str(source)], timeout=180)
        _run(["git", "checkout", SOURCE_REVISION], cwd=source)
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source,
                            capture_output=True, text=True, check=False)
    if result.returncode or result.stdout.strip() != SOURCE_REVISION:
        raise RuntimeError("source_revision_mismatch")
    # Keep the user agent consistent with the pinned browser runtime.
    session_file = source / "src/browser/session.ts"
    code = session_file.read_text(encoding="utf-8")
    updated = code.replace("Chrome/124.0.0.0", "Chrome/146.0.0.0")
    if "Chrome/146.0.0.0" not in updated:
        raise RuntimeError("source_patch_failed")
    needs_build = updated != code
    if needs_build:
        session_file.write_text(updated, encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    env["HUSKY"] = "0"
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    env["npm_config_cache"] = str(BASE / "npm-cache")
    if not (source / "node_modules/cloakbrowser").is_dir():
        _run([str(node.parent / "npm"), "ci", "--ignore-scripts", "--no-audit",
              "--no-fund"], cwd=source, env=env, timeout=300)
    if needs_build or not (source / "build/browser/session.js").is_file():
        _run([str(node.parent / "npm"), "run", "build"], cwd=source,
             env=env, timeout=180)
    return source


def _binary(node: Path, source: Path) -> None:
    env = os.environ.copy()
    env["CLOAKBROWSER_CACHE_DIR"] = str(BASE / "binary")
    _run([str(node), "--input-type=module", "-e",
          "import {ensureBinary} from 'cloakbrowser'; await ensureBinary();"],
         cwd=source, env=env, timeout=300)


def _seed(account: str, node: Path, source: Path) -> bool:
    registry = AccountRegistry()
    threads = registry.status("threads", account)
    instagram = registry.status("instagram", account)
    if (not threads or not instagram or
            threads["session_file"] != instagram["session_file"] or
            threads["account_key"] != instagram["account_key"] or
            not threads["session_exists"] or not threads["session_private"]):
        return False
    profile = BASE / "profile" / account
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(profile, 0o700)
    if (profile / "Default/Cookies").is_file():
        return True
    data = json.loads(Path(threads["session_file"]).read_text(encoding="utf-8"))
    auth = data.get("authorization_data") or {}
    cookies = data.get("cookies") or {}
    if (cookies.get("ds_user_id") and auth.get("ds_user_id") and
            str(cookies["ds_user_id"]) != str(auth["ds_user_id"])):
        return False
    sessionid = auth.get("sessionid") or cookies.get("sessionid")
    user_id = auth.get("ds_user_id") or cookies.get("ds_user_id")
    if not sessionid or not str(user_id).isdigit():
        return False
    env = os.environ.copy()
    env.update({"THREADS_PROFILE_DIR": str(profile),
                "THREADS_SESSIONID": str(sessionid),
                "THREADS_DS_USER_ID": str(user_id),
                "CLOAKBROWSER_CACHE_DIR": str(BASE / "binary")})
    _run([str(node), str(source / "build/import-session.js")], cwd=source,
         env=env, timeout=120)
    return True


ERRORS = {
    "invalid_account": (EXIT_USAGE, "Use a Threads handle with letters, digits, '.' or '_'.",
                        "ghost-in-the-box threads-setup --account YOUR_HANDLE"),
    "node_and_npm_required": (EXIT_AUTH, "SOCIAL_FARM_NODE or the node on PATH has no npm next "
                              "to it.", "Install Node.js 24 with npm, or point SOCIAL_FARM_NODE "
                              "at a node binary that has npm beside it."),
    "install_node_and_npm_for_this_platform": (EXIT_AUTH, "Automatic Node.js setup only covers "
                                               "Linux x86-64.", NODE_INSTALL),
    "git_required": (EXIT_AUTH, "git is needed to fetch the pinned threads-mcp source.",
                     "Install git, then re-run this command."),
    "node_checksum_mismatch": (EXIT_RUNTIME, "The downloaded Node.js archive failed its "
                               "checksum, so it was discarded.", "Retry; if it repeats, check "
                               "your network or proxy."),
    "source_revision_mismatch": (EXIT_RUNTIME, "The local threads-mcp checkout is not at the "
                                 "pinned revision.", "Remove {source} and re-run this command."),
    "setup_failed": (EXIT_RUNTIME, "A setup step failed.", "Re-run this command; the steps "
                     "resume where they stopped."),
}
SETUP_CODES = {"node_and_npm_required", "install_node_and_npm_for_this_platform",
               "node_checksum_mismatch", "source_revision_mismatch"}


@guarded("threads-setup")
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, metavar="HANDLE", help="your Threads handle")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._]+", args.account):
        result = {"ok": False, "status": "invalid_account"}
        code = attach_error(result, ERRORS, account=args.account)
        print(json.dumps(result))
        return code
    os.umask(0o077)
    try:
        BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(BASE, 0o700)
        node = _node()
        source = _source(node)
        _binary(node, source)
        seeded = _seed(args.account, node, source)
        print(json.dumps({"ok": True, "status": "ready", "profile_seeded": seeded}))
        return 0
    except RuntimeError as exc:
        result = {"ok": False, "status": str(exc) if str(exc) in SETUP_CODES else "setup_failed"}
    except FileNotFoundError as exc:
        # git and npm are run by name; a missing executable is a dependency, not a failure.
        result = {"ok": False, "status": "git_required" if "git" in str(exc.filename or "")
                  else "setup_failed"}
    except Exception:
        result = {"ok": False, "status": "setup_failed"}
    code = attach_error(result, ERRORS, account=args.account, source=str(BASE / "source"))
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
