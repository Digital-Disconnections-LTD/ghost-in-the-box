#!/usr/bin/env python3
"""Register and inspect local social account session references.

  ghost-in-the-box accounts list
  ghost-in-the-box accounts show --platform instagram --account HANDLE
  ghost-in-the-box accounts register --platform instagram --account HANDLE
      --kind instagrapi_settings --session-file /private/path/HANDLE.json

Most people never need register: `ghost-in-the-box login start` does it.
Exit status: 0 ok, 1 runtime failure, 2 usage error, 4 account not registered.
"""

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farm.accounts import (
    AccountRegistry, KNOWN_PLATFORMS, RegistryError, SESSION_KINDS, check_handle, handle_rule,
)
from farm.cli_output import (
    DOCTOR, EXIT_AUTH, EXIT_RUNTIME, EXIT_USAGE, choice_error, command_help, fail, guarded, succeed,
)

COMMAND = "accounts"


def _failure(code: str, message: str, hint: str | None, status: int) -> int:
    # `error_type` stays for callers that matched on it before the envelope.
    return fail(code, message, hint, status, error_type=code)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", metavar="PATH", help="private registry path (default: see ghost-in-the-box config)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    register = commands.add_parser("register", help="record where a saved session file lives",
                                   description="Record where a saved session file lives. The login wizard "
                                               "does this for you.")
    register.add_argument("--platform", required=True, metavar="PLATFORM", help="one of: " + ", ".join(KNOWN_PLATFORMS))
    register.add_argument("--account", required=True, metavar="HANDLE", help="public handle without @")
    register.add_argument("--kind", choices=SESSION_KINDS, required=True)
    register.add_argument("--session-file", required=True, metavar="PATH",
                          help="private session file or browser profile")
    register.add_argument("--account-key", metavar="KEY",
                          help="shared serialization key; Instagram and Threads default to meta:HANDLE")
    show = commands.add_parser("show", help="show one registration and whether its session is usable",
                               description="Show one registration and whether its session file is usable.")
    show.add_argument("platform_arg", nargs="?", metavar="PLATFORM", help=argparse.SUPPRESS)
    show.add_argument("account_arg", nargs="?", metavar="ACCOUNT", help=argparse.SUPPRESS)
    show.add_argument("--platform", dest="platform_flag", metavar="PLATFORM",
                      help="one of: " + ", ".join(KNOWN_PLATFORMS))
    show.add_argument("--account", dest="account_flag", metavar="HANDLE", help="public handle without @")
    show.usage = "%(prog)s --platform PLATFORM --account HANDLE"
    commands.add_parser("list", help="list every registration", description="List every registration.")
    return parser


def _pick(parser, flag, positional, name):
    if flag and positional and flag != positional:
        parser.error(f"{name} given twice with different values: {positional} and {flag}")
    value = flag or positional
    if not value:
        parser.error(f"show needs --{name}, e.g. accounts show --platform instagram --account YOUR_HANDLE")
    return value


def _unknown_platform(platform: str) -> int | None:
    if platform in KNOWN_PLATFORMS:
        return None
    message, hint = choice_error("--platform", platform, KNOWN_PLATFORMS)
    return _failure("unknown_platform", message, hint, EXIT_USAGE)


@guarded(COMMAND)
def main(argv=None) -> int:
    os.umask(0o077)
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            refused = _unknown_platform(args.platform)
            if refused is not None:
                return refused
            account = args.account.removeprefix("@")
            try:
                check_handle(args.platform, account)
            except ValueError:
                return _failure("invalid_account", f"--account {args.account!r} is not {handle_rule(args.platform)}",
                                "pass your public handle as shown on your profile, without @", EXIT_USAGE)
            registry = AccountRegistry(args.registry)
            result = registry.register(
                platform=args.platform, account=account, kind=args.kind,
                session_file=args.session_file, account_key=args.account_key,
            )
        elif args.command == "show":
            platform = _pick(parser, args.platform_flag, args.platform_arg, "platform")
            account = _pick(parser, args.account_flag, args.account_arg, "account")
            refused = _unknown_platform(platform)
            if refused is not None:
                return refused
            registry = AccountRegistry(args.registry)
            result = registry.status(platform, account)
            if result is None:
                return _failure(
                    "account_not_registered", f"{platform}:{account} is not registered in {registry.path}",
                    f"ghost-in-the-box login start --platform {platform} --account {account}", EXIT_AUTH)
        else:
            result = AccountRegistry(args.registry).list()
        return succeed(result)
    except RegistryError as exc:
        status = EXIT_RUNTIME if exc.code == "registry_unreadable" else EXIT_USAGE
        return _failure(exc.code, exc.message, exc.hint or command_help(COMMAND), status)
    except ValueError as exc:
        return _failure("invalid_input", str(exc), command_help(COMMAND), EXIT_USAGE)
    except OSError as exc:
        target = exc.filename or (args.registry or "the account registry")
        return _failure("local_state_unavailable", f"{exc.strerror or type(exc).__name__}: {target}",
                        f"check the path and its permissions; {DOCTOR} shows what is wrong", EXIT_RUNTIME)


if __name__ == "__main__":
    raise SystemExit(main())
