"""Tests for the uploader module."""

import json
import os
from unittest.mock import patch, MagicMock

import cv2
import numpy as np

import uploader


def _make_grab_dir(tmp_path, zone="cheese", t_start=1000.0, t_end=1002.0, num_during=2):
    """Create a minimal grab directory with images and meta.json."""
    grab_dir = tmp_path / f"grab_{zone}"
    grab_dir.mkdir(exist_ok=True)

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.imwrite(str(grab_dir / "before.jpg"), frame)
    cv2.imwrite(str(grab_dir / "after.jpg"), frame)
    for i in range(num_during):
        cv2.imwrite(str(grab_dir / f"during_{i + 1:02d}.jpg"), frame)

    meta = {
        "grab_id": f"test_{zone}",
        "zone": zone,
        "t_start": t_start,
        "t_end": t_end,
        "frame_times": [t_start + 0.5 * i for i in range(num_during)],
        "source": "iphone",
        "camera_id": "mac_cam",
    }
    with open(str(grab_dir / "meta.json"), "w") as f:
        json.dump(meta, f)

    return str(grab_dir)


def _reset_zones():
    """Reset auto-assigned zone IDs between tests."""
    uploader.ZONE_MAP.clear()
    uploader._next_zone_id[0] = 1


class TestUploadGrab:
    def test_skips_when_no_base_url(self, tmp_path):
        """Should silently return when BASE_URL is empty."""
        grab_dir = _make_grab_dir(tmp_path)
        old = uploader.BASE_URL
        try:
            uploader.BASE_URL = ""
            with patch("uploader.requests.post") as mock_post:
                uploader.upload_grab(grab_dir, background=False)
                mock_post.assert_not_called()
        finally:
            uploader.BASE_URL = old

    def test_posts_all_images(self, tmp_path):
        """Should POST once per image: before + during_01 + during_02 + after = 4."""
        _reset_zones()
        grab_dir = _make_grab_dir(tmp_path, num_during=2)
        old = uploader.BASE_URL
        try:
            uploader.BASE_URL = "http://localhost:9999"
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.status_code = 200

            with patch("uploader.requests.post", return_value=mock_resp) as mock_post:
                uploader.upload_grab(grab_dir, background=False)
                assert mock_post.call_count == 4  # before + 2 during + after
        finally:
            uploader.BASE_URL = old

    def test_post_url_and_fields(self, tmp_path):
        """Verify the URL, zone fields, and timestamp in each POST."""
        _reset_zones()
        grab_dir = _make_grab_dir(tmp_path, zone="pepperoni", t_start=1700000000.0, num_during=0)
        old = uploader.BASE_URL
        try:
            uploader.BASE_URL = "http://example.com"
            mock_resp = MagicMock()
            mock_resp.ok = True

            with patch("uploader.requests.post", return_value=mock_resp) as mock_post:
                uploader.upload_grab(grab_dir, background=False)
                # 2 calls: before + after
                assert mock_post.call_count == 2

                for call in mock_post.call_args_list:
                    data = call[1]["data"]
                    assert data["zone"] == 1  # first zone auto-assigned to 1
                    assert data["zone_name"] == "pepperoni"
                    assert data["grab_id"] == "test_pepperoni"
                    assert "2023-11-14" in data["time"]  # from t_start epoch
                    assert call[0][0] == "http://example.com/upload"
        finally:
            uploader.BASE_URL = old

    def test_images_are_resized_to_640x480_png(self, tmp_path):
        """Each uploaded image should be a 640x480 PNG."""
        _reset_zones()
        grab_dir = _make_grab_dir(tmp_path, num_during=0)
        old = uploader.BASE_URL
        try:
            uploader.BASE_URL = "http://localhost:9999"
            mock_resp = MagicMock()
            mock_resp.ok = True

            with patch("uploader.requests.post", return_value=mock_resp) as mock_post:
                uploader.upload_grab(grab_dir, background=False)

                for call in mock_post.call_args_list:
                    file_tuple = call[1]["files"]["image"]
                    png_bytes = file_tuple[1]
                    arr = np.frombuffer(png_bytes, dtype=np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    assert img.shape == (480, 640, 3)
                    assert png_bytes[:4] == b'\x89PNG'
        finally:
            uploader.BASE_URL = old

    def test_connection_error_does_not_raise(self, tmp_path):
        """Upload failures should be logged, not raised."""
        _reset_zones()
        grab_dir = _make_grab_dir(tmp_path, num_during=0)
        old = uploader.BASE_URL
        try:
            uploader.BASE_URL = "http://localhost:9999"
            with patch("uploader.requests.post", side_effect=ConnectionError("refused")):
                uploader.upload_grab(grab_dir, background=False)
        finally:
            uploader.BASE_URL = old

    def test_missing_meta_json_does_not_raise(self, tmp_path):
        """If meta.json is missing, should log and return."""
        _reset_zones()
        grab_dir = str(tmp_path / "empty_grab")
        os.makedirs(grab_dir, exist_ok=True)
        old = uploader.BASE_URL
        try:
            uploader.BASE_URL = "http://localhost:9999"
            with patch("uploader.requests.post") as mock_post:
                uploader.upload_grab(grab_dir, background=False)
                mock_post.assert_not_called()
        finally:
            uploader.BASE_URL = old

    def test_zone_id_auto_assignment(self):
        """Zone names should get stable auto-assigned numeric IDs."""
        _reset_zones()
        assert uploader._zone_id("cheese") == 1
        assert uploader._zone_id("pepperoni") == 2
        assert uploader._zone_id("sauce") == 3
        assert uploader._zone_id("cheese") == 1  # stable
        assert uploader._zone_id("pepperoni") == 2  # stable

    def test_background_thread_upload(self, tmp_path):
        """Background=True should still work (fire-and-forget)."""
        _reset_zones()
        grab_dir = _make_grab_dir(tmp_path, zone="dough", num_during=0)
        old = uploader.BASE_URL
        try:
            uploader.BASE_URL = "http://localhost:9999"
            mock_resp = MagicMock()
            mock_resp.ok = True

            with patch("uploader.requests.post", return_value=mock_resp) as mock_post:
                uploader.upload_grab(grab_dir, background=True)
                # give the thread a moment
                import time
                time.sleep(0.5)
                assert mock_post.call_count == 2  # before + after
        finally:
            uploader.BASE_URL = old
