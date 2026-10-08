"""Training pipeline backend package, running parallel to data package with independent evolution."""

from __future__ import annotations

import sys
from pathlib import Path

_TRAIN_SRC = Path(__file__).resolve().parent / "src"
if _TRAIN_SRC.is_dir() and str(_TRAIN_SRC) not in sys.path:
    sys.path.insert(0, str(_TRAIN_SRC))
