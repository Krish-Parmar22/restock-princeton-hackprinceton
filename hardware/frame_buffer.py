"""Rolling frame buffer — keeps the last ~N seconds of frames in memory."""

from collections import deque
import time


class FrameBuffer:
    def __init__(self, duration: float = 2.0):
        self.duration = duration
        self._buffer = deque()  # each entry: (timestamp, frame)

    def add(self, frame, timestamp: float = None):
        if timestamp is None:
            timestamp = time.time()
        self._buffer.append((timestamp, frame.copy()))
        # evict old frames
        cutoff = timestamp - self.duration
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()

    def get_frame_at(self, target_time: float):
        """Return the frame closest to target_time, or None."""
        if not self._buffer:
            return None
        best = min(self._buffer, key=lambda x: abs(x[0] - target_time))
        return best[1]

    def get_frames_between(self, t_start: float, t_end: float):
        """Return all frames between t_start and t_end."""
        return [(t, f) for t, f in self._buffer if t_start <= t <= t_end]

    def __len__(self):
        return len(self._buffer)
