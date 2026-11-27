"""Unified entrypoint for the frozen ResearchRadar desktop + web Windows executable."""

import multiprocessing
from pathlib import Path
import sys
import traceback


def _ensure_streams() -> Path:
    """Guarantee valid stdout/stderr streams for windowed (console=False) subprocesses."""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    log_dir = base / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "research_radar.log"
    if sys.stdout is None:
        sys.stdout = open(log_path, "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(log_path, "a", encoding="utf-8", buffering=1)
    return log_path


def main() -> int:
    multiprocessing.freeze_support()
    log_path = _ensure_streams()
    args = sys.argv[1:]
    try:
        if len(args) >= 4 and args[0] == "--internal-extract-pdf":
            from research_radar.pdf_explorer import extract_pdf_pages_json
            print(extract_pdf_pages_json(args[1], int(args[2]), int(args[3])))
            return 0
        if args and args[0] == "--cli":
            from research_radar.cli import main as cli_main
            sys.argv = ["radar", *args[1:]]
            return cli_main()
        from research_radar.desktop import main as desktop_main
        if not args:
            sys.argv = [sys.argv[0], "--open-web"]
        return desktop_main()
    except Exception:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc() + "\n")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
