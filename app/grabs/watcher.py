"""Poll the grabs folder and push every new complete grab package through d1 into the ledger.

Processed packages (keyed by FOLDER name) live in <grabs_dir>/.processed.json so restarts
don't redo work (record_grab is also idempotent on grab_id). Failures retry with backoff
forever, never a permanent skip, and are listed in <grabs_dir>/.failed.json for visibility.
A package is only read once its files have stopped changing for `settle_s` AND every image
decodes, so a half-copied package is never sent to d1.
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
FAILED_FILE = ".failed.json"
MAX_BACKOFF_S = 300.0


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
        backoff_base_s: float = 2.0,
        hand_check: Callable | None = None,
    ):
        self.dir = Path(grabs_dir or settings.grabs_dir)
        if client is None:
            from app.d1.base import get_client

            client = get_client()
        self.client = client
        self.items_fn, self.record_fn, self.settle_s = items_fn, record_fn, settle_s
        self.backoff_base_s = backoff_base_s
        self.hand_check = hand_check
        self.after_poll: Callable[[], object] | None = None
        self.state_path = self.dir / PROCESSED_FILE
        self.processed: set[str] = self._load_state()
        self.failures: dict[str, dict] = {}  # folder name -> {"count", "next_try", "error"}

    # ---------- state ----------
    def _load_state(self) -> set[str]:
        """Must be a JSON list of strings. Anything else is quarantined (never guessed at):
        reprocessing is safe because record_grab is idempotent."""
        try:
            data = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return set()
        except (json.JSONDecodeError, OSError):
            data = None
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            return set(data)
        bad = self.state_path.with_name(f".processed.corrupt-{int(time.time())}.json")
        os.replace(self.state_path, bad)
        log.error("unreadable %s moved to %s; reprocessing (record_grab is idempotent)", self.state_path, bad.name)
        return set()

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self.processed), indent=1))
        os.replace(tmp, self.state_path)

    # ---------- discovery ----------
    def _save_failures(self) -> None:
        tmp = self.dir / (FAILED_FILE + ".tmp")
        tmp.write_text(json.dumps(self.failures, indent=1, default=str))
        os.replace(tmp, self.dir / FAILED_FILE)

    def _fail(self, key: str, err: Exception) -> None:
        f = self.failures.get(key, {"count": 0})
        f["count"] += 1
        f["error"] = f"{type(err).__name__}: {err}"[:300]
        f["next_try"] = time.time() + min(self.backoff_base_s * 2 ** (f["count"] - 1), MAX_BACKOFF_S)
        self.failures[key] = f
        self._save_failures()
        log.error("%s failed (attempt %d, retrying): %s", key, f["count"], f["error"])

    @staticmethod
    def _images_ok(pkg: GrabPackage) -> bool:
        from PIL import Image

        try:
            for p in (pkg.before, *pkg.during, pkg.after):
                with Image.open(p) as im:
                    im.verify()
            return True
        except Exception:
            return False

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
            if folder.name in self.processed:
                continue
            f = self.failures.get(folder.name)
            if f and time.time() < f["next_try"]:
                continue
            try:
                if not self._settled(folder):
                    continue
                pkg = GrabPackage.load(folder)
            except (ValueError, OSError):
                continue  # incomplete or half-written; try again next poll
            if not self._images_ok(pkg):
                continue  # all files present but an image is still half-written
            out.append(pkg)
        return out

    def _items(self) -> list[str] | None:
        """The shop's real item list, or None. Never guessed from zone names (fail closed)."""
        try:
            items = self.items_fn()
        except Exception as e:  # DB down or not migrated
            log.error("item list unavailable (%s); will retry next poll", type(e).__name__)
            return None
        if not items:
            log.error("items table is empty; seed it first. Will retry next poll")
            return None
        return items

    # ---------- processing ----------
    def poll_once(self) -> list[str]:
        """Process every pending package once. Returns the grab_ids recorded this poll."""
        pkgs = self.pending()
        if not pkgs:
            return []
        items = self._items()
        if items is None:
            return []
        done = []
        for pkg in pkgs:
            gid, key = pkg.meta.grab_id, pkg.before.parent.name  # key = folder name, everywhere
            try:
                row, answers = check_grab(pkg, items, self.client, self.hand_check)
                self.record_fn(row, answers)
            except Exception as e:
                self._fail(key, e)
                continue
            self.processed.add(key)
            if self.failures.pop(key, None) is not None:
                self._save_failures()
            self._save_state()
            done.append(gid)
            conf = f"{row['confidence']:.2f}" if row["confidence"] is not None else "-"
            log.info("%s %s %s %s %s %s", gid, row["zone"], row["action"], row["item"] or "-", row["status"], conf)
        return done

    def run_forever(self, interval: float = 2.0) -> None:
        log.info("watching %s every %.1fs", self.dir, interval)
        while True:
            try:
                if self.poll_once() and self.after_poll:
                    self.after_poll()   # e.g. proactive below-par alerts
            except Exception:  # never let one bad poll kill the watcher thread
                log.exception("poll failed; continuing")
            time.sleep(interval)


def build_watcher(allow_fake: bool = False, grabs_dir: Path | None = None) -> "GrabWatcher":
    """The production watcher: real d1 (refuses the fake one unless allow_fake), YOLO-World hand
    check preloaded at startup (not on the first grab) when YOLO_ENABLED."""
    from app.d1.base import FakeD1, get_client

    client = get_client()
    if isinstance(client, FakeD1) and not allow_fake:
        raise RuntimeError("D1_BACKEND=fake would log made-up grabs into the real ledger. "
                           "Set D1_BACKEND=openrouter (or pass allow_fake for dev).")
    hand_check = None
    if settings.yolo_enabled:
        from app.grabs import yolo

        if yolo.available():
            yolo._model()  # load weights now: the first real grab shouldn't wait ~25 s
            hand_check = yolo.check_hand
            log.info("YOLO-World hand double-check: on (weights loaded)")
        else:
            log.warning("YOLO_ENABLED but ultralytics isn't installed: d1 only")
    w = GrabWatcher(grabs_dir, client=client, settle_s=1.0, hand_check=hand_check)
    from app.agent.alerts import check_and_alert
    w.after_poll = check_and_alert
    return w
