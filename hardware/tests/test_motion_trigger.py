"""Tests for motion detection inside polygon zones (MOG2-based)."""

import numpy as np
import cv2
from motion_trigger import MotionTrigger
from conftest import make_frame_with_motion

# MOG2 needs a warmup period to learn the background.
# We feed 60+ identical frames before injecting motion.
WARMUP_FRAMES = 60


def _warmup(trigger, frame, n=WARMUP_FRAMES):
    """Feed still frames to let MOG2 learn the background."""
    for i in range(n):
        trigger.update(frame, timestamp=float(i) * 0.066)
    return n * 0.066  # return the timestamp we're at


class TestMotionTrigger:
    def test_no_motion_no_events(self, blank_frame, sample_zones):
        trigger = MotionTrigger(sample_zones, blank_frame.shape)
        t = _warmup(trigger, blank_frame)
        # more still frames after warmup
        for i in range(30):
            events = trigger.update(blank_frame, timestamp=t + i * 0.1)
            assert events == []

    def test_brief_flicker_ignored(self, blank_frame, sample_zones):
        """Motion shorter than min_duration should not trigger a grab."""
        trigger = MotionTrigger(
            sample_zones, blank_frame.shape,
            min_duration=0.3, settle_time=0.5,
            min_contour_area=100,
        )
        cheese_pts = sample_zones[0]["points"]
        t = _warmup(trigger, blank_frame)

        # single flash of motion
        motion = make_frame_with_motion(blank_frame, cheese_pts)
        trigger.update(motion, timestamp=t + 0.1)

        # immediately back to still
        trigger.update(blank_frame, timestamp=t + 0.2)

        # wait for settle — should produce nothing
        events = []
        for i in range(20):
            events.extend(trigger.update(blank_frame, timestamp=t + 0.3 + i * 0.1))
        assert len(events) == 0

    def test_sustained_motion_triggers_grab(self, blank_frame, sample_zones):
        """Motion lasting longer than min_duration should produce a grab event after settling."""
        trigger = MotionTrigger(
            sample_zones, blank_frame.shape,
            min_duration=0.3, settle_time=0.5,
            min_contour_area=100,
        )
        cheese_pts = sample_zones[0]["points"]
        t = _warmup(trigger, blank_frame)

        # sustained motion for 0.5s — alternate intensity so MOG2 keeps seeing foreground
        for i in range(10):
            intensity = 220 if i % 2 == 0 else 140
            motion = make_frame_with_motion(blank_frame, cheese_pts, intensity=intensity)
            trigger.update(motion, timestamp=t + 0.05 * (i + 1))

        # stop motion, wait for settle
        events = []
        for i in range(20):
            events.extend(trigger.update(blank_frame, timestamp=t + 0.6 + i * 0.1))

        assert len(events) == 1
        assert events[0]["zone"] == "cheese"
        assert events[0]["t_start"] < events[0]["t_end"]

    def test_motion_in_correct_zone(self, blank_frame, sample_zones):
        """Motion in the pepperoni zone should not trigger the cheese zone."""
        trigger = MotionTrigger(
            sample_zones, blank_frame.shape,
            min_duration=0.2, settle_time=0.3,
            min_contour_area=100,
        )
        pepperoni_pts = sample_zones[1]["points"]
        t = _warmup(trigger, blank_frame)

        for i in range(10):
            intensity = 220 if i % 2 == 0 else 140
            motion = make_frame_with_motion(blank_frame, pepperoni_pts, intensity=intensity)
            trigger.update(motion, timestamp=t + 0.05 * (i + 1))

        events = []
        for i in range(15):
            events.extend(trigger.update(blank_frame, timestamp=t + 0.6 + i * 0.1))

        assert len(events) == 1
        assert events[0]["zone"] == "pepperoni"

    def test_two_zones_independent(self, blank_frame, sample_zones):
        """Simultaneous motion in both zones should produce two grabs."""
        trigger = MotionTrigger(
            sample_zones, blank_frame.shape,
            min_duration=0.2, settle_time=0.3,
            min_contour_area=100,
        )
        t = _warmup(trigger, blank_frame)

        for i in range(10):
            frame = blank_frame.copy()
            intensity = 220 if i % 2 == 0 else 140
            for z in sample_zones:
                frame = make_frame_with_motion(frame, z["points"], intensity=intensity)
            trigger.update(frame, timestamp=t + 0.05 * (i + 1))

        events = []
        for i in range(15):
            events.extend(trigger.update(blank_frame, timestamp=t + 0.6 + i * 0.1))

        zone_names = {e["zone"] for e in events}
        assert zone_names == {"cheese", "pepperoni"}

    def test_no_zones_no_crash(self, blank_frame):
        """Empty zone list should work without errors."""
        trigger = MotionTrigger([], blank_frame.shape)
        events = trigger.update(blank_frame, timestamp=0.0)
        assert events == []

    def test_zone_mask_covers_polygon(self, blank_frame, sample_zones):
        """The precomputed mask should have nonzero pixels inside the polygon."""
        trigger = MotionTrigger(sample_zones, blank_frame.shape)
        for z in trigger.zones:
            area = cv2.countNonZero(z.mask)
            assert area > 0

    def test_shadows_ignored(self, sample_zones):
        """Low-intensity changes (shadows) should not trigger a grab.

        MOG2 shadow detection works by comparing brightness ratios, so we
        use a mid-gray baseline and darken the zone slightly to simulate
        a shadow passing over.
        """
        # mid-gray baseline (like a lit countertop)
        gray_frame = np.full((720, 1280, 3), 128, dtype=np.uint8)

        trigger = MotionTrigger(
            sample_zones, gray_frame.shape,
            min_duration=0.2, settle_time=0.3,
            detect_shadows=True,
            min_contour_area=100,
            var_threshold=50,
        )
        cheese_pts = sample_zones[0]["points"]
        t = _warmup(trigger, gray_frame)

        # shadow: darken the zone slightly (128 → 100, a ~22% drop)
        for i in range(10):
            shadow = make_frame_with_motion(gray_frame, cheese_pts, intensity=100)
            trigger.update(shadow, timestamp=t + 0.05 * (i + 1))

        events = []
        for i in range(15):
            events.extend(trigger.update(gray_frame, timestamp=t + 0.6 + i * 0.1))

        assert len(events) == 0
