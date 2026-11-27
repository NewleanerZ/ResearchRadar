"""Path resolution helpers supporting both source checkouts and frozen PyInstaller bundles."""

from pathlib import Path
import sys


def is_frozen() -> bool:
    """Return True when running inside a PyInstaller / frozen binary."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Read-only directory containing bundled assets (web/dist, migrations, icons, base models)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """Writable directory next to the executable (frozen) or project root (source)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
