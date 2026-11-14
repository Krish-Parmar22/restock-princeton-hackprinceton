"""Continuous recording in ~5-minute MP4 chunks with timestamped filenames."""

import cv2
import os
import time
from datetime import datetime

CHUNK_SECONDS = 300  # 5 minutes


class Recorder:
    def __init__(self, output_dir: str, fps: float = 15.0, resolution: tuple = (1280, 720)):
        self.output_dir = output_dir
        self.fps = fps
        self.resolution = resolution
        self.writer = None
        self.chunk_start = 0.0
        self.frame_count = 0
        os.makedirs(output_dir, exist_ok=True)

    def _new_chunk(self):
        if self.writer is not None:
            self.writer.release()
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S-%f")
        path = os.path.join(self.output_dir, f"recording_{ts}.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.writer = cv2.VideoWriter(path, fourcc, self.fps, self.resolution)
        self.chunk_start = time.time()
        self.frame_count = 0
        print(f"[recorder] new chunk: {path}")

    def write_frame(self, frame):
        if self.writer is None or (time.time() - self.chunk_start) >= CHUNK_SECONDS:
            self._new_chunk()
        resized = cv2.resize(frame, self.resolution)
        self.writer.write(resized)
        self.frame_count += 1

    def release(self):
        if self.writer is not None:
            self.writer.release()
            self.writer = None
