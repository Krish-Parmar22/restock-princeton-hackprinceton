"""The Pi's per-frame pipeline: frame -> motion trigger -> grab package. Used by main.py and tests.

Two bugs this fixes (found by tests on realistic frames, Nov 14):
1. BEFORE came from the middle of long grabs. The old loop asked the 3 s FrameBuffer for the
   before frame only when the grab ENDED, so any grab over ~2 s got a "before" with the hand in it.
   Now BEFORE is snapshotted the moment a zone starts moving, and DURING is sampled live.
2. One long rummage split into two grabs. Background subtraction slowly absorbs a hand that stays
   in the bin, so motion can drop out for ~1 s mid-grab. The second package then had no clean
   before and could double-count. Now a grab is only finalized after MERGE_S seconds of quiet. If
   the same zone moves again sooner, it's the same grab (same before, more during frames).

Frames:
  BEFORE  ~1 s before motion first started (snapshot)
  DURING  sampled at ~2 fps while the zone is active; the cutter keeps 6 evenly spaced
  AFTER   the frame when the zone has been still for settle_time (+ MERGE_S of no new motion)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from frame_buffer import FrameBuffer
from grab_cutter import GrabCutter
from motion_trigger import MotionTrigger

MAX_DURING_KEPT = 30   # cap memory on very long grabs
MERGE_S = 1.0          # quiet time before a grab is final; motion sooner = same grab


@dataclass
class _Grab:
    t_start: float
    before: object
    during: list = field(default_factory=list)   # [(t, frame)]
    last_sample: float = 0.0
    t_end: float | None = None                    # set when the trigger reports the grab ended
    after: object = None
    merged: bool = False                          # continued after a first end (within MERGE_S)


class PiPipeline:
    def __init__(self, zones: list[dict], frame_shape, cutter: GrabCutter, camera_id: str = "pi",
                 buffer_seconds: float = 3.0, during_fps: float = 2.0, trigger: MotionTrigger | None = None,
                 merge_s: float = MERGE_S):
        self.trigger = trigger or MotionTrigger(zones, frame_shape)
        self.buffer = FrameBuffer(duration=buffer_seconds)
        self.cutter = cutter
        self.camera_id = camera_id
        self.points = {z["name"]: z["points"] for z in zones}
        self.interval = 1.0 / during_fps
        self.merge_s = merge_s
        self.grabs: dict[str, _Grab] = {}

    def process(self, frame, ts: float) -> list[str]:
        """Feed one frame. Returns the grab package folders finalized on this frame."""
        self.buffer.add(frame, ts)
        ended = {e["zone"]: e for e in self.trigger.update(frame, ts)}
        if getattr(self.trigger, "scene_reset", False) and self.grabs:
            self.grabs.clear()   # camera moved mid-grab: that clip is meaningless, never upload it
            print("[camera] view changed (camera moved or lights): re-learning the scene, grab discarded")
        folders = []
        for z in self.trigger.zones:
            g = self.grabs.get(z.name)
            if z.active:
                if g is None:   # a new grab: snapshot BEFORE now, while it's still in the buffer
                    before = self.buffer.get_frame_at(z.motion_start - 1.0)
                    g = self.grabs[z.name] = _Grab(t_start=z.motion_start,
                                                   before=None if before is None else before.copy())
                elif g.t_end is not None:   # moved again within MERGE_S: same grab continues
                    g.t_end, g.after, g.merged = None, None, True
                if ts - g.last_sample >= self.interval:
                    g.during.append((ts, frame.copy()))
                    g.last_sample = ts
                    if len(g.during) > MAX_DURING_KEPT:
                        g.during = g.during[::2]
            if g is None:
                continue
            if z.name in ended:            # trigger says the grab ended (settled)
                g.t_end, g.after = ts, frame.copy()
                if ended[z.name].get("capped"):   # hit max_duration: ship it now, don't merge into it
                    folder = self._finalize(z.name, g)
                    del self.grabs[z.name]
                    if folder:
                        folders.append(folder)
                    continue
            elif not z.active and g.t_end is None and g.merged:
                # a merged continuation died down without its own "ended" event (it never re-met
                # min_duration): it's still part of the grab, so end it here instead of losing it
                g.t_end, g.after = ts, frame.copy()
            elif not z.active and g.t_end is None and not z.triggered and not g.during[1:]:
                del self.grabs[z.name]     # brief flicker that never became a grab
                continue
            if g.t_end is not None and ts - g.t_end >= self.merge_s:
                folder = self._finalize(z.name, g)
                del self.grabs[z.name]
                if folder:
                    folders.append(folder)
        return folders

    def _finalize(self, zone: str, g: _Grab) -> str | None:
        class _AfterBuffer:   # the cutter reads AFTER at t_end; serve the frame we kept
            def __init__(self, inner, t_end, after):
                self.inner, self.t_end, self.after = inner, t_end, after

            def get_frame_at(self, t):
                return self.after if abs(t - self.t_end) < 1e-6 and self.after is not None else self.inner.get_frame_at(t)

            def get_frames_between(self, a, b):
                return self.inner.get_frames_between(a, b)

        return self.cutter.cut(
            zone, g.t_start, g.t_end, _AfterBuffer(self.buffer, g.t_end, g.after),
            zone_polygon=self.points.get(zone), camera_id=self.camera_id,
            before_frame=g.before, during=g.during,
        )

    def flush(self) -> list[str]:
        """Finalize any grabs still waiting out their merge window (e.g. on shutdown)."""
        folders = []
        for zone, g in list(self.grabs.items()):
            if g.t_end is not None:
                f = self._finalize(zone, g)
                if f:
                    folders.append(f)
            del self.grabs[zone]
        return folders
