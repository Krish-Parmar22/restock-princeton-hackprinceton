"""Tests for the rolling frame buffer."""

import numpy as np
from frame_buffer import FrameBuffer


class TestFrameBuffer:
    def test_add_and_length(self, blank_frame):
        buf = FrameBuffer(duration=2.0)
        assert len(buf) == 0

        buf.add(blank_frame, timestamp=1.0)
        buf.add(blank_frame, timestamp=1.5)
        buf.add(blank_frame, timestamp=2.0)
        assert len(buf) == 3

    def test_eviction(self, blank_frame):
        buf = FrameBuffer(duration=2.0)
        buf.add(blank_frame, timestamp=1.0)
        buf.add(blank_frame, timestamp=2.0)
        buf.add(blank_frame, timestamp=3.0)
        # t=1.0 should be evicted (3.0 - 2.0 = 1.0 cutoff, 1.0 < 1.0 is false)
        assert len(buf) == 3

        buf.add(blank_frame, timestamp=3.5)
        # now cutoff = 1.5, so t=1.0 is evicted
        assert len(buf) == 3

    def test_get_frame_at_exact(self, blank_frame, white_frame):
        buf = FrameBuffer(duration=5.0)
        buf.add(blank_frame, timestamp=1.0)
        buf.add(white_frame, timestamp=2.0)

        result = buf.get_frame_at(2.0)
        assert result is not None
        assert np.array_equal(result, white_frame)

    def test_get_frame_at_closest(self, blank_frame, white_frame):
        buf = FrameBuffer(duration=5.0)
        buf.add(blank_frame, timestamp=1.0)
        buf.add(white_frame, timestamp=3.0)

        # 2.4 is closer to 3.0 than to 1.0
        result = buf.get_frame_at(2.4)
        assert np.array_equal(result, white_frame)

        # 1.3 is closer to 1.0
        result = buf.get_frame_at(1.3)
        assert np.array_equal(result, blank_frame)

    def test_get_frame_at_empty(self):
        buf = FrameBuffer(duration=2.0)
        assert buf.get_frame_at(1.0) is None

    def test_get_frames_between(self, blank_frame):
        buf = FrameBuffer(duration=10.0)
        for t in [1.0, 2.0, 3.0, 4.0, 5.0]:
            buf.add(blank_frame, timestamp=t)

        frames = buf.get_frames_between(2.0, 4.0)
        assert len(frames) == 3
        timestamps = [t for t, f in frames]
        assert timestamps == [2.0, 3.0, 4.0]

    def test_get_frames_between_empty_range(self, blank_frame):
        buf = FrameBuffer(duration=10.0)
        buf.add(blank_frame, timestamp=1.0)
        buf.add(blank_frame, timestamp=5.0)

        frames = buf.get_frames_between(2.0, 4.0)
        assert len(frames) == 0

    def test_frames_are_copies(self, blank_frame):
        """Modifying the original frame should not affect buffered frames."""
        buf = FrameBuffer(duration=5.0)
        frame = blank_frame.copy()
        buf.add(frame, timestamp=1.0)

        frame[:] = 128  # modify original
        result = buf.get_frame_at(1.0)
        assert result[0, 0, 0] == 0  # buffered copy is unchanged
