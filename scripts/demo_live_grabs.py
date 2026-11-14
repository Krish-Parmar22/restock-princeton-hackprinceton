"""Push realistic grabs through the RUNNING system, so they show up on the dashboard and (via alerts) iMessage.

  real photos -> 15 fps "camera" video -> the Pi's own motion code (hardware/pipeline.py)
  -> uploader -> POST /api/grabs/upload on the running API -> watcher -> real YOLO-World + d1
  -> Postgres ledger -> dashboard (:8501) + below-par alert -> Photon bridge -> owner's phone

Plus the 4 hand-built edge cases (touched, put back, no hand, wrong zone) so "Needs your OK" has something.

Run (API on :8000 with the watcher on):  uv run python scripts/demo_live_grabs.py
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "hardware"))

import cv2  # noqa: E402
import uploader  # noqa: E402
from grab_cutter import GrabCutter  # noqa: E402
from pipeline import PiPipeline  # noqa: E402

from tests.e2e.scene import ZONES, build_all, remove_slices, scene, with_hand  # noqa: E402

FPS = 15


def camera_video():
    s, after = scene(), remove_slices(scene())
    frames, t = [], time.time() - 60

    def add(img, n):
        nonlocal t
        bgr = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
        for _ in range(n):
            frames.append((t, bgr))
            t += 1 / FPS

    add(s, 6 * FPS)                                            # idle: background model learns the bins
    for d in np.linspace(0.0, 0.9, int(1.5 * FPS)):           # hand reaches into the pepperoni
        add(with_hand(s, "pepperoni", float(d)), 1)
    add(with_hand(after, "pepperoni", 0.9), int(0.6 * FPS))   # grabs a handful
    for d in np.linspace(0.9, -0.6, int(1.0 * FPS)):          # leaves
        add(with_hand(after, "pepperoni", float(d)), 1)
    add(after, 12 * FPS)                                       # bin is emptier, settles
    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--token", default="")
    ap.add_argument("--no-edge-cases", action="store_true")
    args = ap.parse_args()
    uploader.BASE_URL, uploader.UPLOAD_TOKEN = args.api, args.token
    work = Path(tempfile.mkdtemp(prefix="demo-grabs-"))

    zones = [{"name": "pepperoni", "points": ZONES["pepperoni"]}]
    frames = camera_video()
    pipe = PiPipeline(zones, frames[0][1].shape, GrabCutter(output_dir=str(work / "pi"), source="webcam"), camera_id="demo-cam")
    folders = []
    for ts, f in frames:
        folders += pipe.process(f, ts)
    folders += pipe.flush()
    print(f"Pi motion code cut {len(folders)} grab package(s) from {len(frames)} frames")

    if not args.no_edge_cases:
        cases = build_all(work / "cases")
        folders += [str(cases[k]) for k in ("touched", "put_back", "no_hand", "wrong_zone")]

    ids = []
    for f in folders:
        status = uploader.deliver(f)
        ids.append(Path(f).name)
        print(f"  upload {Path(f).name}: {status}")

    day = datetime.now(timezone.utc).astimezone().date().isoformat()
    print("waiting for the watcher (YOLO + d1)...")
    seen = {}
    for _ in range(120):
        rows = httpx.get(f"{args.api}/api/grabs", params={"date": day}).json()
        seen = {g["grab_id"]: g for g in rows if g["grab_id"] in ids}
        if len(seen) == len(ids):
            break
        time.sleep(2)
    for gid in ids:
        g = seen.get(gid)
        print(f"  {gid}: " + (f"{g['action']} {g.get('qty_label') or ''} {g.get('item') or ''} -> {g['status']} (conf {g['confidence']})" if g else "NOT PROCESSED"))


if __name__ == "__main__":
    main()
