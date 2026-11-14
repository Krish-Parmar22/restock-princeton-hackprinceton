"""The Pi's per-frame pipeline: frame -> motion trigger -> grab package. Used by main.py and tests.

Why this exists (found by tests/e2e/test_live_system.py, Nov 14): the old loop asked the 3 s
FrameBuffer for the BEFORE frame only when the grab ENDED. Any grab longer than ~2 s got a
"before" frame from the middle of the grab (with the hand in it), which breaks d1's main signal
(before vs after). Now:
  - BEFORE is snapshotted the moment a zone starts moving (from ~1 s earlier in the buffer)
  - DURING frames are sampled live at ~2 fps while the zone is active (so long grabs keep them)
  - AFTER is the frame when the zone has been still for the settle time
"""
from __future__ import annotations

from dataclasses import dataclass, field

from frame_buffer import FrameBuffer
from grab_cutter import GrabCutter
from motion_trigger import MotionTrigger

MAX_DURING_KEPT = 30   # cap memory on very long grabs; the cutter keeps 6 evenly spaced


@dataclass
class _Live:
    before: object
    during: list = field(default_factory=list)   # [(t, frame)]
    last_sample: float = 0.0


class PiPipeline:
    def __init__(self, zones: list[dict], frame_shape, cutter: GrabCutter, camera_id: str = "pi",
                 buffer_seconds: float = 3.0, during_fps: float = 2.0, trigger: MotionTrigger | None = None):
        self.trigger = trigger or MotionTrigger(zones, frame_shape)
        self.buffer = FrameBuffer(duration=buffer_seconds)
        self.cutter = cutter
        self.camera_id = camera_id
        self.points = {z["name"]: z["points"] for z in zones}
        self.interval = 1.0 / during_fps
        self.live: dict[str, _Live] = {}

    def process(self, frame, ts: float) -> list[str]:
        """Feed one frame. Returns the grab package folders completed on this frame."""
        self.buffer.add(frame, ts)
        events = self.trigger.update(frame, ts)
        for z in self.trigger.zones:
            if z.active and z.name not in self.live:
                before = self.buffer.get_frame_at(z.motion_start - 1.0)
                self.live[z.name] = _Live(before=None if before is None else before.copy())
            if z.active and z.name in self.live:
                lv = self.live[z.name]
                if ts - lv.last_sample >= self.interval:
                    lv.during.append((ts, frame.copy()))
                    lv.last_sample = ts
                    if len(lv.during) > MAX_DURING_KEPT:
                        lv.during = lv.during[::2]
            if not z.active and z.name in self.live and not any(e["zone"] == z.name for e in events):
                del self.live[z.name]   # motion stopped before min_duration: no grab
        folders = []
        for ev in events:
            lv = self.live.pop(ev["zone"], None)
            folder = self.cutter.cut(
                ev["zone"], ev["t_start"], ev["t_end"], self.buffer,
                zone_polygon=self.points.get(ev["zone"]), camera_id=self.camera_id,
                before_frame=lv.before if lv else None,
                during=lv.during if lv else None,
            )
            if folder:
                folders.append(folder)
        return folders
