"""Windowless launcher for the ResearchRadar PySide6 desktop companion."""

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_radar.desktop import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
