"""YOLO-World double-check: was a hand actually inside this bin's box during the grab?

No counting. d1 decides what happened. YOLO only confirms a hand (or gloved hand) was in the
bin's zone in at least one DURING frame. If d1 says took/put_back but YOLO saw no hand in the
zone in any frame, the grab is flagged for the owner.

Runs on the laptop (not the Pi), ~100 ms/frame on CPU. Weights download once to models/.
The zone is `meta.zone_polygon` (pixel corners on the full frame); without it, the whole frame
is the zone (weaker check).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import ROOT
from app.contracts import GrabPackage

log = logging.getLogger("grabs.yolo")

WEIGHTS = ROOT / "models" / "yolov8s-worldv2.pt"
CLASSES = ["hand", "glove"]      # kitchen staff often wear gloves
MIN_CONF = 0.15                  # YOLO-World scores hands low; 0.15-0.25 is typical for real hands
MIN_OVERLAP = 0.3                # fraction of the hand box that must lie inside the zone


@dataclass
class HandCheck:
    frames_checked: int
    frames_with_hand_in_zone: int
    best_conf: float

    @property
    def hand_seen(self) -> bool:
        return self.frames_with_hand_in_zone > 0


@lru_cache(maxsize=1)
def _model():
    from ultralytics import YOLOWorld

    m = YOLOWorld(str(WEIGHTS))
    m.set_classes(CLASSES)
    return m


def _overlap_in_zone(box: tuple[float, float, float, float], polygon: list[list[float]] | None, shape) -> float:
    """Fraction of the box's area inside the zone polygon (rasterized on a coarse grid)."""
    if not polygon:
        return 1.0
    from matplotlib.path import Path as MplPath  # ships with ultralytics

    x1, y1, x2, y2 = box
    if x2 <= x1 or y2 <= y1:
        return 0.0
    n = 12
    pts = [(x1 + (x2 - x1) * (i + 0.5) / n, y1 + (y2 - y1) * (j + 0.5) / n) for i in range(n) for j in range(n)]
    inside = MplPath(polygon).contains_points(pts)
    return float(inside.mean())


def check_hand(pkg: GrabPackage, model=None, device: str = "cpu") -> HandCheck:
    model = model or _model()
    best, hits = 0.0, 0
    for frame in pkg.during:
        r = model.predict(str(frame), conf=MIN_CONF, verbose=False, device=device)[0]
        found = False
        for b in r.boxes:
            conf = float(b.conf)
            box = tuple(float(v) for v in b.xyxy[0].tolist())
            if _overlap_in_zone(box, pkg.meta.zone_polygon, r.orig_shape) >= MIN_OVERLAP:
                found = True
                best = max(best, conf)
        hits += found
    return HandCheck(frames_checked=len(pkg.during), frames_with_hand_in_zone=hits, best_conf=round(best, 4))


def available() -> bool:
    """True if ultralytics is installed and the weights are present (or downloadable)."""
    try:
        import ultralytics  # noqa: F401
        return True
    except ImportError:
        return False
