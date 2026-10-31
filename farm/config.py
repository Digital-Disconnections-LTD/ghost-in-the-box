"""Shared local state location; existing installations keep their paths."""

import os
from pathlib import Path

STATE_ROOT = Path(os.environ.get("SOCIAL_FARM_STATE_DIR") or
                  Path.home() / ".batbox/social-farm-state").expanduser()
