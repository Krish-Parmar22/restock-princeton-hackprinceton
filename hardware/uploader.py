"""Upload grab packages to the laptop: one multipart POST per package (docs/handoff.md).

    POST {BASE_URL}/api/grabs/upload
      meta     = meta.json text (zone NAME, epoch-second timestamps, zone_polygon)
      files    = before.jpg, during_01..06.jpg, after.jpg  (original JPEGs, not resized)
      header   X-Upload-Token: <UPLOAD_TOKEN>   (when set)

Why one package per request (vs one POST per image): the laptop gets the whole grab or
nothing, can dedupe retries (409), and never has to guess when a grab is complete.

Delivery is guaranteed by a background worker: every package folder without a `.sent`
marker is retried with backoff until the laptop answers 200/409. 400 = the package itself
is bad, so it's marked `.rejected` and never retried. Nothing is deleted.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import threading
import time

import requests

log = logging.getLogger(__name__)

BASE_URL = ""       # set by main.py via --api-url
UPLOAD_TOKEN = ""   # set by main.py via --upload-token (or UPLOAD_TOKEN env var)
SENT, REJECTED = ".sent", ".rejected"
MAX_BACKOFF = 60.0


def _files(grab_dir: str) -> list[str]:
    files = [os.path.join(grab_dir, "before.jpg")]
    files += sorted(glob.glob(os.path.join(grab_dir, "during_*.jpg")))[:6]
    files.append(os.path.join(grab_dir, "after.jpg"))
    return [f for f in files if os.path.exists(f)]


def send_package(grab_dir: str) -> str:
    """POST one package. Returns 'sent' | 'duplicate' | 'rejected' | 'retry'."""
    if not BASE_URL:
        return "retry"
    try:
        with open(os.path.join(grab_dir, "meta.json")) as f:
            meta_text = f.read()
        json.loads(meta_text)
    except Exception as e:
        log.error("[upload] unreadable meta in %s: %s", grab_dir, e)
        return "rejected"
    handles = [(os.path.basename(p), open(p, "rb")) for p in _files(grab_dir)]
    try:
        resp = requests.post(
            f"{BASE_URL.rstrip('/')}/api/grabs/upload",
            data={"meta": meta_text},
            files=[(name, (name, fh, "image/jpeg")) for name, fh in handles],
            headers={"X-Upload-Token": UPLOAD_TOKEN} if UPLOAD_TOKEN else {},
            timeout=30,
        )
    except requests.RequestException as e:
        log.warning("[upload] %s: laptop unreachable (%s), will retry", os.path.basename(grab_dir), e)
        return "retry"
    finally:
        for _, fh in handles:
            fh.close()
    if resp.status_code == 200:
        return "sent"
    if resp.status_code == 409:
        return "duplicate"   # an earlier attempt landed: success
    if resp.status_code == 400:
        log.error("[upload] %s rejected: %s", os.path.basename(grab_dir), resp.text[:300])
        return "rejected"
    log.warning("[upload] %s: HTTP %s, will retry", os.path.basename(grab_dir), resp.status_code)
    return "retry"


def _mark(grab_dir: str, marker: str, note: str = "") -> None:
    with open(os.path.join(grab_dir, marker), "w") as f:
        f.write(note or time.strftime("%Y-%m-%dT%H:%M:%S"))


def deliver(grab_dir: str) -> str:
    """Send once and record the outcome with a marker file."""
    status = send_package(grab_dir)
    if status in ("sent", "duplicate"):
        _mark(grab_dir, SENT, status)
        print(f"[upload] {os.path.basename(grab_dir)}: {status}")
    elif status == "rejected":
        _mark(grab_dir, REJECTED)
    return status


def pending(grabs_dir: str) -> list[str]:
    """Complete package folders that haven't been sent or rejected, oldest first."""
    out = []
    for d in sorted(glob.glob(os.path.join(grabs_dir, "*"))):
        name = os.path.basename(d)
        if not os.path.isdir(d) or name.startswith("."):
            continue
        if os.path.exists(os.path.join(d, SENT)) or os.path.exists(os.path.join(d, REJECTED)):
            continue
        if not os.path.exists(os.path.join(d, "meta.json")):
            continue
        out.append(d)
    return out


class UploadWorker(threading.Thread):
    """Background thread: keeps delivering every unsent package, with backoff when offline."""

    def __init__(self, grabs_dir: str, poll: float = 1.0):
        super().__init__(daemon=True, name="uploader")
        self.grabs_dir, self.poll = grabs_dir, poll
        self.wake = threading.Event()
        self.stop_flag = False
        self.backoff = 0.0

    def nudge(self) -> None:
        """Call after writing a new package to send it right away."""
        self.wake.set()

    def run(self) -> None:
        while not self.stop_flag:
            for d in pending(self.grabs_dir):
                if deliver(d) == "retry":
                    self.backoff = min(MAX_BACKOFF, (self.backoff * 2) or 2.0)
                    break   # laptop unreachable: wait before trying the rest
                self.backoff = 0.0
            self.wake.wait(self.backoff or self.poll)
            self.wake.clear()


def upload_grab(grab_dir: str, *, background: bool = True) -> None:
    """Back-compat entry point: deliver one package now (or in a thread)."""
    if not BASE_URL:
        return
    if background:
        threading.Thread(target=deliver, args=(grab_dir,), daemon=True).start()
    else:
        deliver(grab_dir)
