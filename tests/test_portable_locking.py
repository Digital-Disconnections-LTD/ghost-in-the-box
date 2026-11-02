import errno
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class WindowsLockTests(unittest.TestCase):
    def backend(self, msvcrt):
        source = (Path(__file__).resolve().parents[1] / "farm/locking.py").read_text()
        env = {}
        with patch("os.name", "nt"), patch.dict(sys.modules, msvcrt=msvcrt):
            exec(compile(source, "locking.py", "exec"), env)
        return SimpleNamespace(**env)

    def test_nonblocking_contention_and_unlock_use_one_byte(self):
        msvcrt = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=Mock())
        backend = self.backend(msvcrt)
        with patch("os.lseek") as seek:
            backend.flock(42, backend.LOCK_EX)
            backend.flock(42, backend.LOCK_UN)
            self.assertEqual(msvcrt.locking.call_args_list[0].args, (42, 2, 1))
            self.assertEqual(msvcrt.locking.call_args_list[1].args, (42, 0, 1))
            self.assertEqual(seek.call_count, 2)
            msvcrt.locking.side_effect = PermissionError(errno.EACCES, "locked")
            with self.assertRaises(BlockingIOError):
                backend.flock(42, backend.LOCK_EX | backend.LOCK_NB)

    def test_blocking_lock_waits_on_contention_only(self):
        msvcrt = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0,
                                locking=Mock(side_effect=[PermissionError(errno.EACCES, "locked"), None]))
        backend = self.backend(msvcrt)
        with patch("os.lseek"), patch("time.sleep") as sleep:
            backend.flock(42, backend.LOCK_EX)
            sleep.assert_called_once_with(0.05)
        msvcrt.locking.side_effect = OSError(errno.EBADF, "bad fd")
        with patch("os.lseek"), self.assertRaises(OSError):
            backend.flock(42, backend.LOCK_EX)

    def test_blocking_lock_times_out_instead_of_spinning_forever(self):
        msvcrt = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0,
                                locking=Mock(side_effect=PermissionError(errno.EACCES, "locked")))
        backend = self.backend(msvcrt)
        clock = iter(range(0, 10_000, 1))
        with patch("os.lseek"), patch("time.sleep"), patch("time.monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(TimeoutError):
                backend.flock(42, backend.LOCK_EX, 5)
        self.assertLess(msvcrt.locking.call_count, 10)
        clock = iter(range(0, 10_000_000, 1))
        with patch("os.lseek"), patch("time.sleep"), patch("time.monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(TimeoutError):
                backend.flock(42, backend.LOCK_EX)
        self.assertEqual(backend.WINDOWS_BLOCKING_TIMEOUT, 600.0)

    def test_exclusive_lock_releases_only_what_it_acquired(self):
        msvcrt = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0,
                                locking=Mock(side_effect=OSError(errno.EBADF, "bad fd")))
        backend = self.backend(msvcrt)
        with TemporaryDirectory() as temp, patch("os.lseek"):
            with self.assertRaises(OSError):
                with backend.exclusive_lock(Path(temp) / "x.lock"):
                    self.fail("lock body ran without the lock")
        self.assertEqual([c.args[1] for c in msvcrt.locking.call_args_list], [2])


@unittest.skipIf(os.name == "nt", "native flock contention")
class PosixLockTests(unittest.TestCase):
    def test_timeout_raises_while_another_holder_keeps_the_lock(self):
        from farm import locking
        with TemporaryDirectory() as temp:
            path = Path(temp) / "x.lock"
            with locking.exclusive_lock(path):
                with self.assertRaises(TimeoutError):
                    with locking.exclusive_lock(path, timeout=0.2):
                        self.fail("second holder acquired a held lock")
                with self.assertRaises(BlockingIOError):
                    with locking.exclusive_lock(path, blocking=False):
                        self.fail("second holder acquired a held lock")
            with locking.exclusive_lock(path, timeout=0.2):
                pass


if __name__ == "__main__":
    unittest.main()
