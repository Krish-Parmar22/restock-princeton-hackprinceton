"""Cut a grab event into the grab package folder format."""

import cv2
import json
import os
import uuid
from datetime import datetime


class GrabCutter:
    def __init__(self, output_dir: str = "grabs", source: str = "iphone"):
        self.output_dir = output_dir
        self.source = source
        os.makedirs(output_dir, exist_ok=True)

    def cut(self, zone_name: str, t_start: float, t_end: float,
            frame_buffer, during_fps: float = 2.0,
            zone_polygon: list | None = None, camera_id: str = "mac_cam") -> str | None:
        """Produce a grab package from the frame buffer.

        Returns the grab folder path, or None if not enough frames.
        """
        # before: ~1 second before motion started
        before_frame = frame_buffer.get_frame_at(t_start - 1.0)
        if before_frame is None:
            before_frame = frame_buffer.get_frame_at(t_start)
        if before_frame is None:
            return None

        # after: the latest frame (post-settle)
        after_frame = frame_buffer.get_frame_at(t_end)
        if after_frame is None:
            return None

        # during: sample at ~during_fps between t_start and t_end
        all_during = frame_buffer.get_frames_between(t_start, t_end)
        if not all_during:
            return None

        # subsample to target fps
        interval = 1.0 / during_fps
        during_frames = []
        next_t = t_start
        for t, f in all_during:
            if t >= next_t:
                during_frames.append((t, f))
                next_t = t + interval

        # d1 takes at most 8 images (before + 6 during + after): keep 6 evenly spaced
        if len(during_frames) > 6:
            step = (len(during_frames) - 1) / 5
            during_frames = [during_frames[round(i * step)] for i in range(6)]

        # unique id, written to a hidden temp folder and renamed when complete, so the
        # uploader (and anything else watching grabs/) never sees half a package
        ts_str = datetime.fromtimestamp(t_start).strftime("%Y-%m-%dT%H-%M-%S")
        grab_id, n = f"{ts_str}_{zone_name}", 2
        while os.path.exists(os.path.join(self.output_dir, grab_id)):
            grab_id, n = f"{ts_str}_{zone_name}-{n}", n + 1
        final = os.path.join(self.output_dir, grab_id)
        folder = os.path.join(self.output_dir, f".tmp-{grab_id}")
        os.makedirs(folder, exist_ok=True)

        cv2.imwrite(os.path.join(folder, "before.jpg"), before_frame)
        cv2.imwrite(os.path.join(folder, "after.jpg"), after_frame)

        frame_times = []
        for i, (t, f) in enumerate(during_frames):
            fname = f"during_{i + 1:02d}.jpg"
            cv2.imwrite(os.path.join(folder, fname), f)
            frame_times.append(t)

        meta = {
            "grab_id": grab_id,
            "camera_id": camera_id,
            "zone": zone_name,
            "t_start": t_start,
            "t_end": t_end,
            "frame_times": frame_times,
            "source": self.source,
        }
        if zone_polygon:
            meta["zone_polygon"] = [[int(x), int(y)] for x, y in zone_polygon]  # full-frame pixels
        with open(os.path.join(folder, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2)
        os.rename(folder, final)
        folder = final

        print(f"[grab] {grab_id}: before + {len(during_frames)} during + after")
        return folder
