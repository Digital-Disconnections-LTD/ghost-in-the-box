"""Terminal contract for the Threads commands; it is the shared one in farm.cli_output."""
from __future__ import annotations

from farm.cli_output import (
    EXIT_AUTH, EXIT_OK, EXIT_RUNTIME, EXIT_UNCERTAIN, EXIT_USAGE, MAX_BODY_BYTES, THREADS_TEXT_LIMIT,
    BodyInputError, attach_error, read_body,
)

__all__ = [
    "EXIT_AUTH", "EXIT_OK", "EXIT_RUNTIME", "EXIT_UNCERTAIN", "EXIT_USAGE", "MAX_BODY_BYTES",
    "THREADS_TEXT_LIMIT", "BodyInputError", "attach_error", "read_body",
]
