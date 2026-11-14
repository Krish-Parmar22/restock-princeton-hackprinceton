"""Poll the grabs folder and push every new complete grab package through d1 into the ledger.

Processed grab_ids live in <grabs_dir>/.processed.json so restarts don't redo work
(record_grab is idempotent on grab_id as well).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

from app.config import settings
from app.contracts import GrabPackage
from app.d1.base import D1Client
from app.grabs.checks import check_grab

log = logging.getLogger("grabs.watcher")

PROCESSED_FILE = ".processed.json"
MAX_FAILURES = 3  # per grab per process; after this we stop retrying until restart


def items_from_db() -> list[str]:
    """Item names from the ledger's items table."""
    import psycopg

    with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
        return [r[0] for r in conn.execute("SELECT name FROM items ORDER BY name").fetchall()]


def _record(row: dict, d1_answers: list[dict]) -> None:
    from app.ledger import api  # looked up at call time so tests can patch api.record_grab

    api.record_grab(row, d1_answers)


class GrabWatcher:
    def __init__(
        self,
        grabs_dir: Path | None = None,
        client: D1Client | None = None,
        items_fn: Callable[[], list[str]] = items_from_db,
        record_fn: Callable[[dict, list[dict]], None] = _record,
        settle_s: float = 1.0,
    ):
        self.dir = Path(grabs_dir or settings.grabs_dir)
        if client is None:
            from app.d1.base import get_client

            client = get_client()
        self.client = client
        self.items_fn, self.record_fn, self.settle_s = items_fn, record_fn, settle_s
        self.state_path = self.dir / PROCESSED_FILE
        self.processed: set[str] = self._load_state()
        self.failures: dict[str, int] = {}

    # ---------- state ----------
    def _load_state(self) -> set[str]:
        try:
            return set(json.loads(self.state_path.read_text()))
        except FileNotFoundError:
            return set()
        except (json.JSONDecodeError, TypeError):
            log.warning("ignoring unreadable %s", self.state_path)
            return set()

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self.processed), indent=1))
        os.replace(tmp, self.state_path)

    # ---------- discovery ----------
    def _settled(self, folder: Path) -> bool:
        """True when no file in the folder changed in the last settle_s (still copying otherwise)."""
        if self.settle_s <= 0:
            return True
        newest = max((p.stat().st_mtime for p in folder.iterdir()), default=0.0)
        return time.time() - newest >= self.settle_s

    def pending(self) -> list[GrabPackage]:
        """Complete, settled, not-yet-processed packages, oldest grab_id first."""
        if not self.dir.is_dir():
            return []
        out = []
        for folder in sorted(p for p in self.dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            if folder.name in self.processed or self.failures.get(folder.name, 0) >= MAX_FAILURES:
                continue
            try:
                if not self._settled(folder):
                    continue
                pkg = GrabPackage.load(folder)
            except (ValueError, OSError):
                continue  # incomplete or half-written; try again next poll
            if pkg.meta.grab_id in self.processed:
                continue
            out.append(pkg)
        return out

    def _items(self, pkgs: list[GrabPackage]) -> list[str]:
        try:
            items = self.items_fn()
            if items:
                return items
            log.warning("items table is empty; falling back to zone names")
        except Exception as e:  # DB down or not migrated
            log.warning("items unavailable (%s); falling back to zone names", type(e).__name__)
        zones = {p.meta.zone for p in pkgs}
        for folder in self.dir.iterdir():
            try:
                zones.add(json.loads((folder / "meta.json").read_text())["zone"])
            except Exception:
                pass
        return sorted(zones)

    # ---------- processing ----------
    def poll_once(self) -> list[str]:
        """Process every pending package once. Returns the grab_ids recorded this poll."""
        pkgs = self.pending()
        if not pkgs:
            return []
        items = self._items(pkgs)
        done = []
        for pkg in pkgs:
            gid = pkg.meta.grab_id
            try:
                row, answers = check_grab(pkg, items, self.client)
                self.record_fn(row, answers)
            except Exception as e:
                self.failures[gid] = self.failures.get(gid, 0) + 1
                log.error("%s failed (%d/%d): %s", gid, self.failures[gid], MAX_FAILURES, e)
                continue
            self.processed.add(gid)
            self.processed.add(pkg.before.parent.name)  # folder name, in case it differs from grab_id
            self._save_state()
            done.append(gid)
            conf = f"{row['confidence']:.2f}" if row["confidence"] is not None else "-"
            log.info("%s %s %s %s %s %s", gid, row["zone"], row["action"], row["item"] or "-", row["status"], conf)
        return done

    def run_forever(self, interval: float = 2.0) -> None:
        log.info("watching %s every %.1fs", self.dir, interval)
        while True:
            self.poll_once()
            time.sleep(interval)
