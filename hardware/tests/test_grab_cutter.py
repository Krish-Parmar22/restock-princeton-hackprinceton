"""Tests for grab package creation."""

import json
import os
import numpy as np
from frame_buffer import FrameBuffer
from grab_cutter import GrabCutter


class TestGrabCutter:
    def _fill_buffer(self, buf, blank_frame, t_start=0.0, t_end=3.0, interval=0.1):
        """Fill a buffer with frames at regular intervals."""
        t = t_start
        while t <= t_end:
            # vary pixel slightly so frames aren't identical
            frame = blank_frame.copy()
            frame[:, :, 0] = int(t * 10) % 256
            buf.add(frame, timestamp=t)
            t += interval

    def test_basic_grab_package(self, blank_frame, tmp_grabs):
        buf = FrameBuffer(duration=5.0)
        self._fill_buffer(buf, blank_frame, t_start=0.0, t_end=4.0)

        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        folder = cutter.cut(
            zone_name="cheese",
            t_start=1.5,
            t_end=2.5,
            frame_buffer=buf,
        )

        assert folder is not None
        assert os.path.isdir(folder)

        # check required files
        assert os.path.isfile(os.path.join(folder, "before.jpg"))
        assert os.path.isfile(os.path.join(folder, "after.jpg"))
        assert os.path.isfile(os.path.join(folder, "meta.json"))

        # at least one during frame
        during_files = [f for f in os.listdir(folder) if f.startswith("during_")]
        assert len(during_files) >= 1

    def test_meta_json_schema(self, blank_frame, tmp_grabs):
        buf = FrameBuffer(duration=5.0)
        self._fill_buffer(buf, blank_frame, t_start=0.0, t_end=4.0)

        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        folder = cutter.cut(
            zone_name="pepperoni",
            t_start=1.0,
            t_end=2.0,
            frame_buffer=buf,
        )

        with open(os.path.join(folder, "meta.json")) as f:
            meta = json.load(f)

        assert "grab_id" in meta
        assert meta["zone"] == "pepperoni"
        assert meta["source"] == "iphone"
        assert "t_start" in meta
        assert "t_end" in meta
        assert meta["t_start"] < meta["t_end"]
        assert isinstance(meta["frame_times"], list)
        assert len(meta["frame_times"]) >= 1
        assert "camera_id" in meta

    def test_before_frame_is_before_motion(self, blank_frame, tmp_grabs):
        """Before frame should come from ~1s before t_start."""
        buf = FrameBuffer(duration=5.0)

        # frame at t=0.5 has a distinctive pixel value
        marker_frame = blank_frame.copy()
        marker_frame[0, 0] = [42, 42, 42]
        buf.add(marker_frame, timestamp=0.5)

        # fill the rest normally
        self._fill_buffer(buf, blank_frame, t_start=0.6, t_end=4.0)

        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        folder = cutter.cut(
            zone_name="cheese",
            t_start=1.5,  # before should look for t=0.5
            t_end=2.5,
            frame_buffer=buf,
        )

        assert folder is not None
        # The before.jpg should exist and be a valid image
        import cv2
        before = cv2.imread(os.path.join(folder, "before.jpg"))
        assert before is not None
        assert before.shape == blank_frame.shape

    def test_during_fps_subsampling(self, blank_frame, tmp_grabs):
        """During frames should be subsampled at ~2fps."""
        buf = FrameBuffer(duration=10.0)
        # fill at 15fps over 2 seconds of "motion"
        self._fill_buffer(buf, blank_frame, t_start=0.0, t_end=5.0, interval=1.0 / 15)

        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        folder = cutter.cut(
            zone_name="cheese",
            t_start=1.0,
            t_end=3.0,  # 2 seconds of motion
            frame_buffer=buf,
            during_fps=2.0,
        )

        during_files = sorted(f for f in os.listdir(folder) if f.startswith("during_"))
        # 2 seconds at 2fps = ~4 frames
        assert 3 <= len(during_files) <= 6

    def test_empty_buffer_returns_none(self, tmp_grabs):
        buf = FrameBuffer(duration=5.0)
        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        folder = cutter.cut(
            zone_name="cheese",
            t_start=1.0,
            t_end=2.0,
            frame_buffer=buf,
        )
        assert folder is None

    def test_folder_naming(self, blank_frame, tmp_grabs):
        buf = FrameBuffer(duration=5.0)
        self._fill_buffer(buf, blank_frame, t_start=0.0, t_end=4.0)

        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        folder = cutter.cut(
            zone_name="sauce",
            t_start=1.0,
            t_end=2.0,
            frame_buffer=buf,
        )

        dirname = os.path.basename(folder)
        assert "sauce" in dirname
        # should have a timestamp prefix
        assert "T" in dirname

    def test_multiple_grabs_separate_folders(self, blank_frame, tmp_grabs):
        buf = FrameBuffer(duration=10.0)
        self._fill_buffer(buf, blank_frame, t_start=0.0, t_end=8.0)

        cutter = GrabCutter(output_dir=tmp_grabs, source="iphone")
        f1 = cutter.cut("cheese", 1.0, 2.0, buf)
        f2 = cutter.cut("pepperoni", 3.0, 4.0, buf)

        assert f1 != f2
        assert os.path.isdir(f1)
        assert os.path.isdir(f2)
