"""Generate fake grab packages so software can be built before the camera works.

Each package: before.jpg (bin with N items), during_*.jpg (a 'hand' over the bin),
after.jpg (bin with N-k items for 'took', N+k for 'put_back', N for 'touched'), meta.json.

Usage: uv run python scripts/make_fake_grabs.py [--out grabs] [--day 2026-11-14]
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw

ZONES = {  # zone -> dot color
    "pepperoni": (176, 40, 40),
    "cheese": (240, 214, 120),
    "dough": (230, 220, 200),
    "sauce": (200, 60, 30),
}
SCENARIOS = [  # (zone, action, k)
    ("pepperoni", "took", 4),
    ("cheese", "took", 5),
    ("pepperoni", "took", 3),
    ("dough", "took", 1),
    ("sauce", "touched", 0),
    ("cheese", "put_back", 2),
    ("pepperoni", "took", 6),
]


def draw_bin(zone: str, n: int, hand: float | None = None, seed: int = 0) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", (640, 480), (70, 72, 78))
    d = ImageDraw.Draw(img)
    d.rectangle([170, 120, 470, 380], fill=(200, 200, 205), outline=(40, 40, 40), width=4)  # steel bin
    d.text((180, 100), zone, fill=(255, 255, 255))
    for _ in range(n):
        x, y = rnd.randint(190, 445), rnd.randint(140, 355)
        d.ellipse([x, y, x + 22, y + 22], fill=ZONES[zone], outline=(30, 30, 30))
    if hand is not None:  # skin-toned blob entering from the top, depth 0..1
        top = int(-60 + 300 * hand)
        d.ellipse([260, top, 380, top + 170], fill=(224, 172, 140), outline=(120, 80, 60), width=3)
    return img


def make(out: Path, day: str) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    base = datetime.fromisoformat(f"{day}T11:30:00").replace(tzinfo=timezone.utc)
    made = []
    for i, (zone, action, k) in enumerate(SCENARIOS):
        t0 = base + timedelta(minutes=17 * i)
        n_before = 14
        n_after = {"took": n_before - k, "put_back": n_before + k, "touched": n_before}[action]
        grab_id = f"{t0.strftime('%Y-%m-%dT%H-%M-%S')}_{zone}"
        folder = out / grab_id
        folder.mkdir(exist_ok=True)
        draw_bin(zone, n_before, seed=i).save(folder / "before.jpg", quality=90)
        frame_times = [t0 - timedelta(seconds=1)]
        for j, depth in enumerate([0.3, 0.7, 0.9, 0.5], start=1):
            draw_bin(zone, n_before, hand=depth, seed=i).save(folder / f"during_{j:02d}.jpg", quality=90)
            frame_times.append(t0 + timedelta(seconds=0.5 * j))
        draw_bin(zone, n_after, seed=i + 100).save(folder / "after.jpg", quality=90)
        t_end = t0 + timedelta(seconds=3)
        frame_times.append(t_end + timedelta(seconds=1))
        meta = {
            "grab_id": grab_id, "camera_id": "fake0", "zone": zone,
            "t_start": t0.isoformat(), "t_end": t_end.isoformat(),
            "frame_times": [t.isoformat() for t in frame_times], "source": "fake",
            "zone_polygon": [[170, 120], [470, 120], [470, 380], [170, 380]],  # the drawn bin
            "_truth": {"action": action, "k": k},  # ground truth, for tests only
        }
        (folder / "meta.json").write_text(json.dumps(meta, indent=2))
        made.append(folder)
    return made


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="grabs")
    ap.add_argument("--day", default="2026-11-14")
    a = ap.parse_args()
    for f in make(Path(a.out), a.day):
        print(f)
