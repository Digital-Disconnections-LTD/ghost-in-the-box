"""The flock subset used by the account tools, with a native Windows backend."""
from contextlib import contextmanager
import os
import time

# A blocking Windows lock gives up after this long instead of spinning forever
# behind a process that hung while holding it. POSIX keeps native blocking.
WINDOWS_BLOCKING_TIMEOUT = 600.0
_POLL_SECONDS = 0.05

if os.name != "nt":
    from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock as _native_flock

    def flock(fd, operation, timeout=None):
        if timeout is None or operation & (LOCK_NB | LOCK_UN):
            _native_flock(fd, operation)
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                _native_flock(fd, operation | LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock was not released within {timeout:g} seconds") from None
                time.sleep(_POLL_SECONDS)
else:
    import msvcrt

    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def flock(fd, operation, timeout=None):
        os.lseek(fd, 0, os.SEEK_SET)
        if operation == LOCK_UN:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        limit = WINDOWS_BLOCKING_TIMEOUT if timeout is None else timeout
        deadline = time.monotonic() + limit
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in (13, 36):
                    raise
                if operation & LOCK_NB:
                    raise BlockingIOError("account lock is busy") from None
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock was not released within {limit:g} seconds") from None
                time.sleep(_POLL_SECONDS)
                os.lseek(fd, 0, os.SEEK_SET)


@contextmanager
def exclusive_lock(path, *, blocking=True, timeout=None):
    """Hold an exclusive lock on a private lock file; unlock only what was acquired."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flock(fd, LOCK_EX if blocking else LOCK_EX | LOCK_NB, timeout)
        try:
            yield
        finally:
            flock(fd, LOCK_UN)
    finally:
        os.close(fd)
