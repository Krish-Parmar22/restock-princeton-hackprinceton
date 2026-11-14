"""Tests for the uploader: one package per POST, markers, retry worker."""

import json
import os
import time
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import requests

import uploader


def _make_grab_dir(root, name="2026-11-14T12-14-03_pepperoni", zone="pepperoni", num_during=2):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    for f in ["before.jpg", "after.jpg"] + [f"during_{i + 1:02d}.jpg" for i in range(num_during)]:
        cv2.imwrite(str(d / f), frame)
    (d / "meta.json").write_text(json.dumps({
        "grab_id": name, "zone": zone, "t_start": 1700000000.0, "t_end": 1700000002.0,
        "frame_times": [1700000000.5], "source": "webcam", "camera_id": "pi-1",
        "zone_polygon": [[0, 0], [100, 0], [100, 100]],
    }))
    return d


def _resp(code, text=""):
    r = MagicMock()
    r.status_code, r.text = code, text
    return r


def _setup(token=""):
    uploader.BASE_URL, uploader.UPLOAD_TOKEN = "http://laptop:8000", token


def test_one_post_per_package_with_meta_files_and_token(tmp_path):
    _setup(token="s3cret")
    d = _make_grab_dir(tmp_path, num_during=2)
    with patch("uploader.requests.post", return_value=_resp(200)) as post:
        assert uploader.deliver(str(d)) == "sent"
    assert post.call_count == 1
    args, kw = post.call_args
    assert args[0] == "http://laptop:8000/api/grabs/upload"
    assert json.loads(kw["data"]["meta"])["zone"] == "pepperoni"
    assert [f[0] for f in kw["files"]] == ["before.jpg", "during_01.jpg", "during_02.jpg", "after.jpg"]
    assert all(f[1][2] == "image/jpeg" for f in kw["files"])
    assert kw["headers"] == {"X-Upload-Token": "s3cret"}
    assert (d / uploader.SENT).exists()


def test_409_counts_as_sent_400_is_rejected_and_never_retried(tmp_path):
    _setup()
    a = _make_grab_dir(tmp_path, "a_pepperoni")
    b = _make_grab_dir(tmp_path, "b_pepperoni")
    with patch("uploader.requests.post", return_value=_resp(409)):
        assert uploader.deliver(str(a)) == "duplicate"
    with patch("uploader.requests.post", return_value=_resp(400, "bad zone")):
        assert uploader.deliver(str(b)) == "rejected"
    assert (a / uploader.SENT).exists() and (b / uploader.REJECTED).exists()
    assert uploader.pending(str(tmp_path)) == []


def test_unreachable_or_unauthorized_keeps_package_pending(tmp_path):
    _setup()
    d = _make_grab_dir(tmp_path)
    for side in (requests.ConnectionError("down"), None):
        kw = {"side_effect": side} if side else {"return_value": _resp(503)}
        with patch("uploader.requests.post", **kw):
            assert uploader.deliver(str(d)) == "retry"
    with patch("uploader.requests.post", return_value=_resp(401)):
        assert uploader.deliver(str(d)) == "retry"      # wrong token: fix config, the grab still goes
    assert uploader.pending(str(tmp_path)) == [str(d)]
    assert os.path.exists(d / "before.jpg")             # never deleted


def test_pending_skips_hidden_temp_and_incomplete(tmp_path):
    _make_grab_dir(tmp_path, ".tmp-x_pepperoni")
    (tmp_path / "half_written").mkdir()                 # no meta.json yet
    ok = _make_grab_dir(tmp_path, "ok_pepperoni")
    assert uploader.pending(str(tmp_path)) == [str(ok)]


def test_worker_retries_until_laptop_comes_back(tmp_path, monkeypatch):
    _setup()
    d = _make_grab_dir(tmp_path)
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("down")
        return _resp(200)

    monkeypatch.setattr(uploader, "MAX_BACKOFF", 0.05)
    with patch("uploader.requests.post", side_effect=flaky):
        w = uploader.UploadWorker(str(tmp_path), poll=0.01)
        w.start()
        for _ in range(200):
            if (d / uploader.SENT).exists():
                break
            time.sleep(0.02)
        w.stop_flag = True
        w.nudge()
    assert (d / uploader.SENT).exists() and calls["n"] >= 3


def test_no_base_url_does_nothing(tmp_path):
    uploader.BASE_URL = ""
    d = _make_grab_dir(tmp_path)
    with patch("uploader.requests.post") as post:
        uploader.upload_grab(str(d), background=False)
        post.assert_not_called()
