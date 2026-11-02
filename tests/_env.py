"""Keep tests away from the real state directory and account registry."""
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from farm import config


def isolate_state(test: unittest.TestCase) -> Path:
    """Point the state settings at a fresh temp dir until the test's cleanups run.

    The registry and outbox read their overrides when called. Modules that bound
    STATE_ROOT at import time (such as the Instagram media snapshot) get the temp
    dir rebound for the test as well.
    """
    temp = TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    state = Path(temp.name).resolve() / "state"
    state.mkdir(mode=0o700)
    env = patch.dict(os.environ, {
        "SOCIAL_FARM_STATE_DIR": str(state),
        "SOCIAL_FARM_ACCOUNTS_FILE": str(state / "accounts.json"),
        "SOCIAL_FARM_OUTBOX_DB": str(state / "outbox.sqlite3"),
    })
    env.start()
    test.addCleanup(env.stop)
    original = config.STATE_ROOT
    for name, module in list(sys.modules.items()):
        if (name == "farm" or name.startswith(("farm.", "scripts."))) and \
                getattr(module, "STATE_ROOT", None) is original:
            rebound = patch.object(module, "STATE_ROOT", state)
            rebound.start()
            test.addCleanup(rebound.stop)
    return state


class IsolatedStateTestCase(unittest.TestCase):
    """TestCase whose setUp isolates state; subclasses that define setUp call super().setUp() first."""

    def setUp(self):
        super().setUp()
        self.isolated_state = isolate_state(self)
