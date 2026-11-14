"""Tests for the continuous recorder."""

import os
import time
import cv2
import numpy as np
from recorder import Recorder, CHUNK_SECONDS


class TestRecorder:
    def test_creates_output_dir(self, tmp_path):
        out = str(tmp_path / "new_dir")
        rec = Recorder(output_dir=out)
        assert os.path.isdir(out)
        rec.release()

    def test_writes_mp4(self, blank_frame, tmp_recordings):
        rec = Recorder(output_dir=tmp_recordings, fps=15.0, resolution=(1280, 720))
        for _ in range(30):
            rec.write_frame(blank_frame)
        rec.release()

        files = os.listdir(tmp_recordings)
        mp4s = [f for f in files if f.endswith(".mp4")]
        assert len(mp4s) == 1
        assert mp4s[0].startswith("recording_")

        # verify the file is readable
        path = os.path.join(tmp_recordings, mp4s[0])
        cap = cv2.VideoCapture(path)
        assert cap.isOpened()
        assert cap.get(cv2.CAP_PROP_FRAME_COUNT) >= 20  # some frames may be dropped
        cap.release()

    def test_chunk_rotation(self, blank_frame, tmp_recordings):
        """Force a chunk rotation by using a very short chunk time."""
        import recorder
        original = recorder.CHUNK_SECONDS
        recorder.CHUNK_SECONDS = 0.5  # half a second

        rec = Recorder(output_dir=tmp_recordings, fps=15.0, resolution=(1280, 720))
        for _ in range(15):
            rec.write_frame(blank_frame)
            time.sleep(0.05)  # ~20fps, so 15 frames takes ~0.75s, should trigger rotation

        rec.release()
        recorder.CHUNK_SECONDS = original

        mp4s = [f for f in os.listdir(tmp_recordings) if f.endswith(".mp4")]
        assert len(mp4s) >= 2

    def test_release_idempotent(self, tmp_recordings):
        rec = Recorder(output_dir=tmp_recordings)
        rec.release()
        rec.release()  # should not crash

    def test_frame_resize(self, tmp_recordings):
        """Frames of different sizes should be resized to the target resolution."""
        rec = Recorder(output_dir=tmp_recordings, fps=15.0, resolution=(640, 480))
        big_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        for _ in range(10):
            rec.write_frame(big_frame)
        rec.release()

        mp4s = [f for f in os.listdir(tmp_recordings) if f.endswith(".mp4")]
        assert len(mp4s) == 1
        cap = cv2.VideoCapture(os.path.join(tmp_recordings, mp4s[0]))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        assert (w, h) == (640, 480)
