"""M5-04 fixtures: the M3-04 laboratory world; the harness is built over it per test."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for helper in ("tests/laboratory",):
    sys.path.insert(0, str(ROOT / helper))

from lab_support import new_env, run  # noqa: E402

__all__ = ["ROOT", "new_env", "run"]
