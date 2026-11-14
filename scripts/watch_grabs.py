"""Watch the grabs folder: every new grab package -> d1 checks -> ledger.

Usage:
  uv run python scripts/watch_grabs.py            # poll GRABS_DIR every 2s, forever
  uv run python scripts/watch_grabs.py --once     # process what's there now, then exit
  uv run python scripts/watch_grabs.py --dir tmp/grabs --interval 1

Backend comes from D1_BACKEND ('fake' or 'liquid'); see docs/d1-notes.md.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.grabs.watcher import GrabWatcher  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="process the current folder and exit")
    ap.add_argument("--dir", type=Path, default=settings.grabs_dir)
    ap.add_argument("--interval", type=float, default=2.0)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    w = GrabWatcher(a.dir, settle_s=1.0)  # settle in --once too: never read a half-copied package
    logging.info("d1 backend: %s, autolog threshold %.2f", settings.d1_backend, settings.autolog_min_prob)
    if a.once:
        done = w.poll_once()
        logging.info("processed %d grab(s)", len(done))
        return 0
    try:
        w.run_forever(a.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
