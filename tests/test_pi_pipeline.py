"""The Pi's motion pipeline (hardware/pipeline.PiPipeline, exactly what main.py runs) on realistic
frames built from real photos. Offline: no network after the two fixture photos are cached."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hardware"))

try:
    from tests.e2e.scene import ZONES, remove_slices, scene, with_hand
    _SCENE = scene()
except Exception as e:  # fixtures not cached and no network
    pytest.skip(f"scene fixtures unavailable: {e}", allow_module_level=True)

import cv2  # noqa: E402
from grab_cutter import GrabCutter  # noqa: E402
from pipeline import PiPipeline  # noqa: E402

FPS = 15
CHEESE_POLY = ZONES["cheese"]


def bgr(img):
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


CLEAN = bgr(_SCENE)
AFTER = bgr(remove_slices(_SCENE))


def run(frames, tmp_path, zones=("pepperoni",)):
    zs = [{"name": z, "points": ZONES[z]} for z in zones]
    pipe = PiPipeline(zs, frames[0][1].shape, GrabCutter(output_dir=str(tmp_path / "out"), source="webcam"), camera_id="t")
    out = []
    for ts, f in frames:
        out += pipe.process(f, ts)
    return out + pipe.flush()   # like main.py on shutdown


class Video:
    def __init__(self):
        self.t, self.frames = time.time(), []

    def hold(self, img, seconds):
        for _ in range(int(seconds * FPS)):
            self.frames.append((self.t, img))
            self.t += 1 / FPS
        return self

    def reach(self, zone, base, seconds, depths=(0.0, 0.9), after=None):
        n = int(seconds * FPS)
        for i, d in enumerate(np.linspace(*depths, n)):
            src = after if (after is not None and i > n // 2) else base
            self.frames.append((self.t, bgr(with_hand(src, zone, float(d)))))
            self.t += 1 / FPS
        return self


def frame_is_clean(path, clean=CLEAN, zone="pepperoni") -> bool:
    img = cv2.imread(str(path)).astype(float)
    (x0, y0), (x1, y1) = ZONES[zone][0], ZONES[zone][2]
    return float(np.abs(img[max(0, y0 - 120):y1, x0:x1] - clean.astype(float)[max(0, y0 - 120):y1, x0:x1]).mean()) < 4


def before_is_clean(folder, clean=CLEAN, zone="pepperoni") -> bool:
    img = cv2.imread(str(Path(folder) / "before.jpg")).astype(float)
    (x0, y0), (x1, y1) = ZONES[zone][0], ZONES[zone][2]
    return float(np.abs(img[max(0, y0 - 120):y1, x0:x1] - clean.astype(float)[max(0, y0 - 120):y1, x0:x1]).mean()) < 4


def test_one_take_is_one_grab_with_clean_before_and_no_retrigger(tmp_path):
    v = Video().hold(CLEAN, 6).reach("pepperoni", _SCENE, 2.0, after=remove_slices(_SCENE)) \
        .reach("pepperoni", remove_slices(_SCENE), 1.0, depths=(0.9, -0.6)).hold(AFTER, 15)
    out = run(v.frames, tmp_path)
    assert len(out) == 1, f"permanent bin change must not re-trigger: {out}"
    assert before_is_clean(out[0])
    assert frame_is_clean(Path(out[0]) / "after.jpg", clean=AFTER), "AFTER must show the bin, not the hand"
    assert 1 <= len(list(Path(out[0]).glob("during_*.jpg"))) <= 6


def test_long_rummage_keeps_clean_before_and_six_during(tmp_path):
    """A realistic long grab: in fast, rummage ~8 s (hand keeps moving), out."""
    rng = np.random.default_rng(0)
    v = Video().hold(CLEAN, 6).reach("pepperoni", _SCENE, 0.6, depths=(0.0, 0.8))
    for d in rng.uniform(0.55, 0.95, int(8 * FPS)):
        v.frames.append((v.t, bgr(with_hand(_SCENE, "pepperoni", float(d)))))
        v.t += 1 / FPS
    v.reach("pepperoni", _SCENE, 0.8, depths=(0.8, -0.6)).hold(CLEAN, 6)
    out = run(v.frames, tmp_path)
    assert len(out) == 1, out
    meta = json.loads((Path(out[0]) / "meta.json").read_text())
    assert meta["t_end"] - meta["t_start"] > 8, meta["t_end"] - meta["t_start"]
    assert before_is_clean(out[0])
    assert frame_is_clean(Path(out[0]) / "after.jpg"), "AFTER must be taken once the hand has left"
    assert len(list(Path(out[0]).glob("during_*.jpg"))) == 6


def test_hand_pausing_in_bin_does_not_end_grab_early(tmp_path):
    """Hand goes in, pauses perfectly still for 3 s (grabbing a handful), comes out. The pause is
    still foreground, so the grab must NOT end until the hand has left: AFTER is hand-free."""
    v = Video().hold(CLEAN, 6).reach("pepperoni", _SCENE, 0.6, depths=(0.0, 0.9))
    v.hold(bgr(with_hand(_SCENE, "pepperoni", 0.9)), 3).reach("pepperoni", _SCENE, 0.8, depths=(0.9, -0.6)).hold(CLEAN, 8)
    out = run(v.frames, tmp_path)
    assert len(out) == 1, out
    assert before_is_clean(out[0])
    assert frame_is_clean(Path(out[0]) / "after.jpg"), "a paused hand must not end the grab early"


def test_hand_frozen_over_static_timeout_is_bounded(tmp_path):
    """Hand frozen > static_timeout (6 s): the grab ends anyway so packages are never stuck. Rare in a
    kitchen; the result is bounded: at most 2 packages, the first with a clean BEFORE."""
    v = Video().hold(CLEAN, 6).reach("pepperoni", _SCENE, 0.6, depths=(0.0, 0.9))
    v.hold(bgr(with_hand(_SCENE, "pepperoni", 0.9)), 10).reach("pepperoni", _SCENE, 0.8, depths=(0.9, -0.6)).hold(CLEAN, 12)
    out = run(v.frames, tmp_path)
    assert 1 <= len(out) <= 2 and before_is_clean(out[0])


def test_quick_flicker_is_not_a_grab(tmp_path):
    v = Video().hold(CLEAN, 6).reach("pepperoni", _SCENE, 0.15).hold(CLEAN, 5)   # < min_duration 0.3 s
    assert run(v.frames, tmp_path) == []


def test_two_bins_in_a_row(tmp_path):
    """Pepperoni then cheese. The arm reaching down to the pepperoni crosses the cheese bowl, so the
    Pi may ALSO cut a cheese package for that moment. That's expected: d1 sees the cheese bin
    unchanged -> 'touched' (no inventory change). Both real grabs must be there, in time order."""
    v = Video().hold(CLEAN, 6).reach("pepperoni", _SCENE, 1.5).reach("pepperoni", _SCENE, 0.8, depths=(0.9, -0.6)) \
        .hold(CLEAN, 4).reach("cheese", _SCENE, 1.5).reach("cheese", _SCENE, 0.8, depths=(0.9, -0.6)).hold(CLEAN, 4)
    out = run(v.frames, tmp_path, zones=("pepperoni", "cheese"))
    metas = [json.loads((Path(f) / "meta.json").read_text()) for f in out]
    zones = [m["zone"] for m in metas]
    assert "pepperoni" in zones and zones[-1] == "cheese", zones
    assert len(out) <= 3, zones
    assert all(m["zone_polygon"] == ZONES[m["zone"]] for m in metas)
    real = [m for m in metas if m["zone"] == "pepperoni"] + [metas[-1]]
    assert real[0]["t_start"] < real[-1]["t_start"]          # pepperoni grab before the cheese grab


def test_lights_brightening_documented_behavior(tmp_path):
    """A sudden global lighting change: MOG2 may see 'motion' in every zone. If it does, the package
    goes to d1, which answers 'same' -> 'touched' (no inventory change). This pins the behavior so a
    change is noticed; it is not allowed to produce more than one grab per zone."""
    brighter = np.clip(CLEAN.astype(int) + 35, 0, 255).astype(np.uint8)
    v = Video().hold(CLEAN, 6).hold(brighter, 8)
    out = run(v.frames, tmp_path, zones=("pepperoni", "cheese"))
    per_zone = {}
    for f in out:
        z = json.loads((Path(f) / "meta.json").read_text())["zone"]
        per_zone[z] = per_zone.get(z, 0) + 1
    assert all(n <= 1 for n in per_zone.values()), per_zone


def test_constant_motion_is_capped_and_still_ships(tmp_path):
    """Someone keeps moving in the box for 150 s (busy line): grabs must still be cut, never stuck."""
    rng = np.random.default_rng(1)
    v = Video().hold(CLEAN, 6)
    for d in rng.uniform(0.3, 0.95, int(150 * FPS)):
        v.frames.append((v.t, bgr(with_hand(_SCENE, "pepperoni", float(d)))))
        v.t += 1 / FPS
    zs = [{"name": "pepperoni", "points": ZONES["pepperoni"]}]
    pipe = PiPipeline(zs, CLEAN.shape, GrabCutter(output_dir=str(tmp_path / "out"), source="webcam"), camera_id="t")
    out = []
    for ts, f in v.frames:
        out += pipe.process(f, ts)
    assert len(out) >= 2, f"constant motion must still produce packages before shutdown: {out}"
    for f in out:
        m = json.loads((Path(f) / "meta.json").read_text())
        assert m["t_end"] - m["t_start"] <= 61


def test_bumped_camera_is_not_a_grab_and_recovers_fast(tmp_path):
    """Someone knocks the webcam mid-demo: the whole view shifts. That must not upload junk, and the
    next real grab must still work a few seconds later (found live, Nov 14)."""
    shifted = np.roll(CLEAN, 300, axis=1)            # the camera turned: a totally different view
    v = Video().hold(CLEAN, 6).reach("pepperoni", _SCENE, 0.6, depths=(0.0, 0.6))   # a reach starts...
    v.hold(shifted, 6)                                # ...and the camera gets bumped
    assert run(v.frames, tmp_path / "a") == [], "a bumped camera is not a grab"
    # knocked and set back: after re-learning, the next real grab is still caught
    zs = [{"name": "pepperoni", "points": ZONES["pepperoni"]}]
    pipe = PiPipeline(zs, CLEAN.shape, GrabCutter(output_dir=str(tmp_path / "b"), source="webcam"), camera_id="t")
    w = Video().hold(CLEAN, 6).hold(shifted, 3).hold(CLEAN, 6).reach("pepperoni", _SCENE, 1.5) \
        .reach("pepperoni", _SCENE, 0.8, depths=(0.9, -0.6)).hold(CLEAN, 8)
    out = []
    for ts, f in w.frames:
        out += pipe.process(f, ts)
    assert len(out) >= 1
