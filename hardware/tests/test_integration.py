"""Integration test: simulate a full grab cycle without a real camera.

Feeds synthetic frames through the pipeline and verifies a grab package
is produced with the correct format. MOG2 needs ~60 warmup frames to
learn the background before it can detect foreground.
"""

import json
import os
import numpy as np
from frame_buffer import FrameBuffer
from motion_trigger import MotionTrigger
from grab_cutter import GrabCutter
from conftest import make_frame_with_motion

WARMUP = 60


class TestFullGrabCycle:
    """Simulate: still → hand enters zone → motion → hand leaves → grab package."""

    def _run(self, trigger, buf, frame, t, dt, n, grabs, cutter):
        """Feed n copies of frame, collecting grabs."""
        for _ in range(n):
            buf.add(frame, t)
            events = trigger.update(frame, t)
            for ev in events:
                folder = cutter.cut(ev["zone"], ev["t_start"], ev["t_end"], buf)
                if folder:
                    grabs.append(folder)
            t += dt
        return t

    def test_single_grab_end_to_end(self, blank_frame, sample_zones, tmp_grabs):
        buf = FrameBuffer(duration=10.0)
        trigger = MotionTrigger(
            sample_zones, blank_frame.shape,
            min_duration=0.2, settle_time=0.3,
            min_contour_area=100,
        )
        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        cheese_pts = sample_zones[0]["points"]
        grabs = []
        t = 0.0
        dt = 1.0 / 15

        # warmup MOG2
        t = self._run(trigger, buf, blank_frame, t, dt, WARMUP, grabs, cutter)

        # 1s of stillness (before-frame material)
        t = self._run(trigger, buf, blank_frame, t, dt, 15, grabs, cutter)

        # motion in cheese zone (alternating intensity)
        for i in range(10):
            intensity = 220 if i % 2 == 0 else 140
            frame = make_frame_with_motion(blank_frame, cheese_pts, intensity=intensity)
            buf.add(frame, t)
            events = trigger.update(frame, t)
            for ev in events:
                folder = cutter.cut(ev["zone"], ev["t_start"], ev["t_end"], buf)
                if folder:
                    grabs.append(folder)
            t += dt

        # settle
        t = self._run(trigger, buf, blank_frame, t, dt, 20, grabs, cutter)

        assert len(grabs) == 1
        folder = grabs[0]

        assert os.path.isfile(os.path.join(folder, "before.jpg"))
        assert os.path.isfile(os.path.join(folder, "after.jpg"))
        assert os.path.isfile(os.path.join(folder, "meta.json"))

        during_files = sorted(f for f in os.listdir(folder) if f.startswith("during_"))
        assert len(during_files) >= 1

        with open(os.path.join(folder, "meta.json")) as f:
            meta = json.load(f)
        assert meta["zone"] == "cheese"
        assert meta["source"] == "iphone"
        assert meta["t_start"] < meta["t_end"]
        assert len(meta["frame_times"]) == len(during_files)

    def test_no_grab_on_stillness(self, blank_frame, sample_zones, tmp_grabs):
        """Warmup + 30 seconds of nothing should produce zero grabs."""
        buf = FrameBuffer(duration=5.0)
        trigger = MotionTrigger(sample_zones, blank_frame.shape, min_contour_area=100)
        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        grabs = []
        t = 0.0
        dt = 1.0 / 15

        t = self._run(trigger, buf, blank_frame, t, dt, WARMUP, grabs, cutter)
        t = self._run(trigger, buf, blank_frame, t, dt, 450, grabs, cutter)

        assert len(grabs) == 0

    def test_motion_in_wrong_zone_no_grab_for_other(self, blank_frame, sample_zones, tmp_grabs):
        """Motion in pepperoni zone should not produce a cheese grab."""
        buf = FrameBuffer(duration=10.0)
        trigger = MotionTrigger(
            sample_zones, blank_frame.shape,
            min_duration=0.2, settle_time=0.3,
            min_contour_area=100,
        )
        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        pepperoni_pts = sample_zones[1]["points"]
        grabs = []
        t = 0.0
        dt = 1.0 / 15

        t = self._run(trigger, buf, blank_frame, t, dt, WARMUP, grabs, cutter)
        t = self._run(trigger, buf, blank_frame, t, dt, 15, grabs, cutter)

        for i in range(10):
            intensity = 220 if i % 2 == 0 else 140
            frame = make_frame_with_motion(blank_frame, pepperoni_pts, intensity=intensity)
            buf.add(frame, t)
            events = trigger.update(frame, t)
            for ev in events:
                folder = cutter.cut(ev["zone"], ev["t_start"], ev["t_end"], buf)
                if folder:
                    grabs.append(folder)
            t += dt

        t = self._run(trigger, buf, blank_frame, t, dt, 20, grabs, cutter)

        assert len(grabs) == 1
        with open(os.path.join(grabs[0], "meta.json")) as f:
            meta = json.load(f)
        assert meta["zone"] == "pepperoni"

    def test_two_sequential_grabs(self, blank_frame, sample_zones, tmp_grabs):
        """Two separate motions should produce two separate grab packages."""
        buf = FrameBuffer(duration=10.0)
        trigger = MotionTrigger(
            sample_zones, blank_frame.shape,
            min_duration=0.2, settle_time=0.3,
            min_contour_area=100,
        )
        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        cheese_pts = sample_zones[0]["points"]
        pepperoni_pts = sample_zones[1]["points"]
        grabs = []
        t = 0.0
        dt = 1.0 / 15

        t = self._run(trigger, buf, blank_frame, t, dt, WARMUP, grabs, cutter)

        # Grab 1: cheese
        t = self._run(trigger, buf, blank_frame, t, dt, 15, grabs, cutter)
        for i in range(10):
            intensity = 220 if i % 2 == 0 else 140
            frame = make_frame_with_motion(blank_frame, cheese_pts, intensity=intensity)
            buf.add(frame, t)
            events = trigger.update(frame, t)
            for ev in events:
                folder = cutter.cut(ev["zone"], ev["t_start"], ev["t_end"], buf)
                if folder:
                    grabs.append(folder)
            t += dt
        t = self._run(trigger, buf, blank_frame, t, dt, 30, grabs, cutter)

        # Grab 2: pepperoni
        t = self._run(trigger, buf, blank_frame, t, dt, 15, grabs, cutter)
        for i in range(10):
            intensity = 220 if i % 2 == 0 else 140
            frame = make_frame_with_motion(blank_frame, pepperoni_pts, intensity=intensity)
            buf.add(frame, t)
            events = trigger.update(frame, t)
            for ev in events:
                folder = cutter.cut(ev["zone"], ev["t_start"], ev["t_end"], buf)
                if folder:
                    grabs.append(folder)
            t += dt
        t = self._run(trigger, buf, blank_frame, t, dt, 30, grabs, cutter)

        assert len(grabs) == 2
        zones = {json.load(open(os.path.join(g, "meta.json")))["zone"] for g in grabs}
        assert zones == {"cheese", "pepperoni"}

    def test_after_frame_shows_bin_not_arm(self, blank_frame, sample_zones, tmp_grabs):
        """The after frame should be from after motion stopped (settle time)."""
        buf = FrameBuffer(duration=10.0)
        trigger = MotionTrigger(
            sample_zones, blank_frame.shape,
            min_duration=0.2, settle_time=0.5,
            min_contour_area=100,
        )
        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        cheese_pts = sample_zones[0]["points"]
        grabs = []
        t = 0.0
        dt = 1.0 / 15

        t = self._run(trigger, buf, blank_frame, t, dt, WARMUP, grabs, cutter)
        t = self._run(trigger, buf, blank_frame, t, dt, 15, grabs, cutter)

        for i in range(10):
            intensity = 220 if i % 2 == 0 else 140
            frame = make_frame_with_motion(blank_frame, cheese_pts, intensity=intensity)
            buf.add(frame, t)
            trigger.update(frame, t)
            t += dt

        # settle
        for _ in range(25):
            buf.add(blank_frame, t)
            events = trigger.update(blank_frame, t)
            for ev in events:
                folder = cutter.cut(ev["zone"], ev["t_start"], ev["t_end"], buf)
                if folder:
                    grabs.append(folder)
            t += dt

        assert len(grabs) == 1

        import cv2
        after = cv2.imread(os.path.join(grabs[0], "after.jpg"))
        # still frame = near-black
        assert after.mean() < 50
