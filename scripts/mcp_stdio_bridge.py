#!/usr/bin/env python3
"""Bridge a framed MCP client to a newline-JSON stdio server."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading

MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def canonical(payload: bytes) -> bytes:
    return json.dumps(json.loads(payload), ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def read_message(stream):
    line = stream.readline(MAX_MESSAGE_BYTES + 1)
    while line and not line.strip():
        line = stream.readline(MAX_MESSAGE_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_MESSAGE_BYTES:
        raise ValueError("message too large")
    if not line.lower().startswith(b"content-length:"):
        return canonical(line), "jsonl"
    size = int(line.split(b":", 1)[1].strip())
    if not 0 < size <= MAX_MESSAGE_BYTES:
        raise ValueError("invalid message length")
    header_bytes = len(line)
    while True:
        line = stream.readline(16385)
        header_bytes += len(line)
        if not line or header_bytes > 16384:
            raise ValueError("invalid message headers")
        if line in (b"\n", b"\r\n"):
            break
        if b":" not in line or line.lower().startswith(b"content-length:"):
            raise ValueError("invalid message headers")
    payload = stream.read(size)
    if len(payload) != size:
        raise ValueError("incomplete message")
    return canonical(payload), "framed"


def write_message(stream, payload: bytes, mode: str):
    if mode == "framed":
        stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
        stream.write(payload)
    else:
        stream.write(payload + b"\n")
    stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="server executable and arguments, after --")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provide an MCP server command after --")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=sys.stderr, start_new_session=os.name == "posix")
    mode = ["framed"]
    ready = threading.Event()
    failures = []

    def stop():
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass

    def forward_input():
        # Dedicated descriptors avoid holding interpreter stdio locks at shutdown.
        try:
            with os.fdopen(os.dup(sys.stdin.fileno()), "rb") as incoming:
                while (message := read_message(incoming)) is not None:
                    payload, selected_mode = message
                    if ready.is_set() and selected_mode != mode[0]:
                        raise ValueError("message framing changed")
                    mode[0] = selected_mode
                    ready.set()
                    process.stdin.write(payload + b"\n")
                    process.stdin.flush()
        except (OSError, ValueError) as error:
            failures.append(type(error).__name__)
        finally:
            ready.set()
            try:
                process.stdin.close()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                stop()

    def forward_output():
        try:
            ready.wait()
            with os.fdopen(os.dup(sys.stdout.fileno()), "wb") as outgoing:
                while line := process.stdout.readline(MAX_MESSAGE_BYTES + 1):
                    if len(line) > MAX_MESSAGE_BYTES:
                        raise ValueError("server message too large")
                    if line.strip():
                        write_message(outgoing, canonical(line), mode[0])
        except (OSError, ValueError) as error:
            failures.append(type(error).__name__)
            stop()

    incoming = threading.Thread(target=forward_input, daemon=True)
    outgoing = threading.Thread(target=forward_output, daemon=True)
    incoming.start()
    outgoing.start()
    try:
        result = process.wait()
        ready.set()
        outgoing.join(timeout=3)
        if outgoing.is_alive():
            failures.append("output did not finish")
        if failures:
            print("MCP bridge stopped: invalid or interrupted transport", file=sys.stderr)
            return 2
        return result
    except KeyboardInterrupt:
        stop()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
