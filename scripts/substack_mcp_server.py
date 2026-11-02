#!/usr/bin/env python3
"""Run upstream python-substack MCP with durable local sessions.

Without --account it uses the connection file named by SOCIAL_FARM_SUBSTACK_CONFIG
(see ghost-in-the-box config). Exit codes: 0 ok, 1 runtime failure, 2 usage,
4 the account is not registered or the substack extra is missing.
"""
import argparse
import importlib.util
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.cli_output import DOCTOR, EXIT_AUTH, EXIT_OK, EXIT_RUNTIME, emit, failure
from farm.config import install_command


def _startup_failure(code, message, hint, exit_code, **extra):
    # stdout belongs to the MCP protocol, so startup problems go to stderr.
    print(f"substack-mcp: {message}\nhint: {hint}", file=sys.stderr)
    emit(failure(code, message, hint, **extra), stream=sys.stderr)
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", metavar="HANDLE", help="Substack handle registered by the login wizard")
    args = parser.parse_args(argv)
    if importlib.util.find_spec("substack_mcp") is None:
        return _startup_failure("dependency_unavailable", "substack-mcp needs the 'substack' extra.",
                                install_command("substack"), EXIT_AUTH)
    from farm.substack_connection import SubstackConnection
    from substack_mcp import mcp_server

    config_path = None
    if args.account:
        from farm.accounts import AccountRegistry
        try:
            entry = AccountRegistry().get("substack", args.account)
        except (OSError, ValueError) as error:
            return _startup_failure("registry_unreadable", f"the account registry could not be read "
                                    f"({type(error).__name__})", DOCTOR, EXIT_RUNTIME,
                                    error_type=type(error).__name__)
        if not entry or entry["kind"] != "oauth_file":
            return _startup_failure(
                "account_not_registered", f"Substack account {args.account} is not registered",
                f"ghost-in-the-box login start --platform substack --account {args.account}", EXIT_AUTH)
        config_path = entry["session_file"]
    connection = SubstackConnection(config_path)
    mcp_server.get_api = connection.get_api

    @mcp_server.mcp.tool()
    def connection_status() -> dict:
        """Read local connection setup and the latest publication receipt."""
        return connection.status()

    mcp_server.main()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
