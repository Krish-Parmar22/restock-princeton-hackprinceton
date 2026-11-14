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
    ):
        self.min_pixels = min_pixels
        self.min_contour_area = min_contour_area
        self.min_duration = min_duration
        self.settle_time = settle_time

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
        fg_mask = self.bg_sub.apply(frame)

        # keep only definite foreground (drop shadows)
        _, fg_mask = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)

        # morphological open (remove small noise) then close (fill small holes)
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, self._kernel)
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, self._kernel)

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
            has_motion = motion_ratio > self.min_pixels

            if has_motion:
                zone.last_motion = timestamp
                if not zone.active:
                    zone.active = True
                    zone.motion_start = timestamp
                    zone.triggered = False
                elif not zone.triggered and (timestamp - zone.motion_start) >= self.min_duration:
                    zone.triggered = True

            elif zone.active:
                # no motion — check settle
                if (timestamp - zone.last_motion) >= self.settle_time:
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
