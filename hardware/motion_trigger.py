"""Motion detection inside polygon zones.

Uses MOG2 background subtraction (adapts to lighting changes) with
morphological cleanup and minimum-contour-area filtering to reduce
false positives from shadows, flicker, and camera shake.
"""

import cv2
import numpy as np
import time
from dataclasses import dataclass, field


@dataclass
class ZoneState:
    name: str
    polygon: np.ndarray       # Nx2 int32
    mask: np.ndarray = None   # precomputed mask for the frame size
    active: bool = False      # currently seeing motion
    motion_start: float = 0.0
    last_motion: float = 0.0
    triggered: bool = False   # a grab has started (past min_duration)


class MotionTrigger:
    def __init__(
        self,
        zones: list[dict],
        frame_shape: tuple,
        min_pixels: float = 0.03,    # fraction of zone area that must be foreground
        min_contour_area: int = 500, # ignore blobs smaller than this (pixels)
        min_duration: float = 0.3,   # seconds of motion before a grab starts
        settle_time: float = 0.8,    # seconds of stillness before grab ends
        detect_shadows: bool = True, # MOG2 shadow detection (marks shadows as gray, not white)
        history: int = 200,          # MOG2 background history (frames)
        var_threshold: float = 40,   # MOG2 variance threshold (higher = less sensitive)
        diff_threshold: int = 25,    # brightness change (0-255) that counts as movement
        min_diff_pixels: float = 0.004, # fraction of the zone that must be moving
        diff_lag: int = 4,           # compare to the frame this many frames back (~0.27 s at 15 fps)
        active_learning_rate: float = 0.0005,  # MOG2 learning while a grab is active (auto when idle)
        static_timeout: float = 3.5, # end a grab after this long with no movement, even if the
                                     # zone still differs from the background (e.g. a resting hand)
        max_duration: float = 60.0,  # never let one grab run longer than this (constant motion)
        scene_change_ratio: float = 0.45,  # this much of the WHOLE frame moving = camera moved, not a grab
        relearn_s: float = 2.0,            # then re-learn the view for this long before watching again
    ):
        self.min_pixels = min_pixels
        self.min_contour_area = min_contour_area
        self.min_duration = min_duration
        self.settle_time = settle_time
        self.diff_threshold = diff_threshold
        self.min_diff_pixels = min_diff_pixels
        self._grays = __import__("collections").deque(maxlen=diff_lag + 1)
        self.static_timeout = static_timeout
        self.max_duration = max_duration
        self.scene_change_ratio = scene_change_ratio
        self.relearn_s = relearn_s
        self.scene_reset = False
        self.active_learning_rate = active_learning_rate

        # background subtractor — adapts to gradual lighting changes
        self.bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=history,
            varThreshold=var_threshold,
            detectShadows=detect_shadows,
        )

        # morphology kernel for cleaning noise
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))

        h, w = frame_shape[:2]
        self.zones: list[ZoneState] = []
        for z in zones:
            poly = np.array(z["points"], dtype=np.int32)
            mask = np.zeros((h, w), dtype=np.uint8)
            cv2.fillPoly(mask, [poly], 255)
            self.zones.append(ZoneState(
                name=z["name"],
                polygon=poly,
                mask=mask,
            ))

    def update(self, frame, timestamp: float = None) -> list[dict]:
        """Process a frame. Returns list of grab events that just completed.

        Each event: {"zone": str, "t_start": float, "t_end": float}
        """
        if timestamp is None:
            timestamp = time.time()

        # MOG2 foreground mask: 255=foreground, 127=shadow, 0=background
        # While a grab is in progress, learn the background very slowly: auto-rate MOG2 absorbed a
        # hand pausing in the bin within ~1.3 s (test on realistic frames, Nov 14), ending the grab
        # with the hand still there. Idle: normal adaptation (lighting drift, a restocked bin).
        any_active = any(z.active for z in getattr(self, "zones", []))
        fg_mask = self.bg_sub.apply(frame, learningRate=self.active_learning_rate if any_active else -1)

        # keep only definite foreground (drop shadows)
        _, fg_mask = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)

        # morphological open (remove small noise) then close (fill small holes)
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, self._kernel)
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, self._kernel)

        # frame-to-frame movement. MOG2 alone fails two ways (found on realistic frames, Nov 14):
        # it LEARNS a hand that stays in the bin (~4 s), ending the grab while the hand is still
        # there; and a bin that permanently changed after a take stays "foreground" until learned.
        # So: a grab STARTS on MOG2 foreground + movement, CONTINUES while there's movement,
        # and ENDS when the zone has stopped moving for settle_time.
        # (compared to a frame ~0.27 s back: a smoothly moving hand shifts only a few pixels per frame)
        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        if not self._grays or self._grays[0].shape != gray.shape:
            self._grays.clear()
            move_mask = np.zeros_like(gray)
        else:
            _, move_mask = cv2.threshold(cv2.absdiff(gray, self._grays[0]), self.diff_threshold, 255, cv2.THRESH_BINARY)
        self._grays.append(gray)

        # The CAMERA moved (bumped, picked up, re-aimed) or the lights changed: most of the whole
        # picture differs from ~0.27 s ago. That's not a grab. Cancel any grab in progress (no junk
        # package), re-learn the scene fast, and ignore motion until it has (found live, Nov 14).
        self.scene_reset = False
        moved = cv2.countNonZero(move_mask) / move_mask.size
        if moved > self.scene_change_ratio:
            self._relearn_until = timestamp + self.relearn_s
            self.scene_reset = True
        if timestamp < getattr(self, "_relearn_until", 0):
            self.bg_sub.apply(frame, learningRate=0.5)          # absorb the new view quickly
            for zone in self.zones:
                zone.active, zone.triggered = False, False
            return []

        events = []

        for zone in self.zones:
            # mask foreground to this zone only
            zone_fg = cv2.bitwise_and(fg_mask, zone.mask)

            # find contours — ignore tiny blobs (dust, pixel noise)
            contours, _ = cv2.findContours(zone_fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            significant_area = sum(
                cv2.contourArea(c) for c in contours if cv2.contourArea(c) >= self.min_contour_area
            )

            zone_area = cv2.countNonZero(zone.mask)
            motion_ratio = significant_area / max(zone_area, 1)
            moving = cv2.countNonZero(cv2.bitwise_and(move_mask, zone.mask)) / max(zone_area, 1) > self.min_diff_pixels
            if zone.active:
                has_motion = moving                                        # continue while it moves
            else:
                has_motion = motion_ratio > self.min_pixels and moving     # start: new + moving

            if zone.active and timestamp - zone.motion_start >= self.max_duration:
                # Motion that never stops (a busy line, a person standing in the box): cut the grab so
                # packages still flow. If it keeps moving, the next grab starts fresh.
                if zone.triggered:
                    events.append({"zone": zone.name, "t_start": zone.motion_start, "t_end": timestamp,
                                   "capped": True})
                zone.active = False
                zone.triggered = False
                continue

            if has_motion:
                zone.last_motion = timestamp
                if not zone.active:
                    zone.active = True
                    zone.motion_start = timestamp
                    zone.triggered = False
                elif (not zone.triggered and (timestamp - zone.motion_start) >= self.min_duration
                      and motion_ratio > self.min_pixels):  # still something NEW there (flash echoes are not)
                    zone.triggered = True

            elif zone.active:
                # no movement: end once settled AND the zone looks like background again (a paused
                # hand is still foreground, so it doesn't end the grab early). If the zone stays
                # different (e.g. bin permanently changed by the take, until MOG2 learns it), end
                # anyway after static_timeout.
                still = timestamp - zone.last_motion
                if still >= self.settle_time and (motion_ratio <= self.min_pixels or still >= self.static_timeout):
                    if zone.triggered:
                        events.append({
                            "zone": zone.name,
                            "t_start": zone.motion_start,
                            "t_end": timestamp,
                        })
                    zone.active = False
                    zone.triggered = False

        return events

    @property
    def debug_mask(self):
        """Get the latest foreground mask for visualization. Call after update()."""
        # return a copy of what MOG2 sees — useful for --show mode
        return None  # populated lazily if needed
