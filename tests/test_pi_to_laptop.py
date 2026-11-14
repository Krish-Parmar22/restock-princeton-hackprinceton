"""Pi -> laptop over real HTTP: the hardware GrabCutter + UploadWorker against the real FastAPI
upload endpoint (uvicorn in a thread), then the laptop reads the package exactly as the watcher does."""
from __future__ import annotations

import socket
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hardware"))

import app.api.upload as upload_mod  # noqa: E402
from app.api.server import app  # noqa: E402
from app.config import settings  # noqa: E402
from app.contracts import GrabPackage  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def laptop(tmp_path, monkeypatch):
    dest = tmp_path / "laptop_grabs"
    monkeypatch.setattr(upload_mod, "settings", replace(settings, grabs_dir=dest, upload_token="tok"))
    monkeypatch.setattr(upload_mod, "_known_items", lambda: {"pepperoni", "cheese", "dough", "sauce"})
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", dest
    server.should_exit = True
    t.join(timeout=5)


class _Buffer:
    """Minimal frame buffer with the methods GrabCutter uses."""

    def __init__(self, frames):
        self.frames = frames  # [(t, frame)]

    def get_frame_at(self, t):
        return min(self.frames, key=lambda tf: abs(tf[0] - t))[1]

    def get_frames_between(self, a, b):
        return [(t, f) for t, f in self.frames if a <= t <= b]


def test_real_cutter_and_uploader_deliver_to_real_api(laptop, tmp_path):
    import uploader
    from grab_cutter import GrabCutter

    url, dest = laptop
    pi_out = tmp_path / "pi_grabs"
    t0 = time.time()
    frames = [(t0 + i * 0.1, np.full((720, 1280, 3), i * 5 % 255, np.uint8)) for i in range(-20, 60)]
    poly = [[400, 200], [800, 200], [800, 500], [400, 500]]
    folder = GrabCutter(output_dir=str(pi_out), source="webcam").cut(
        "pepperoni", t0, t0 + 4.0, _Buffer(frames), zone_polygon=poly, camera_id="pi-test")
    assert folder and not Path(folder).name.startswith(".tmp-")

    uploader.BASE_URL, uploader.UPLOAD_TOKEN = url, "wrong"
    assert uploader.deliver(folder) == "retry"             # wrong token: config error, keep it, don't drop
    assert uploader.pending(str(pi_out)) == [folder]
    uploader.UPLOAD_TOKEN = "tok"
    assert uploader.deliver(folder) == "sent"
    assert uploader.send_package(folder) == "duplicate"   # retry after success -> 409

    stored = dest / Path(folder).name
    pkg = GrabPackage.load(stored)                          # exactly how the laptop watcher reads it
    assert pkg.meta.zone == "pepperoni" and pkg.meta.zone_polygon == poly
    assert pkg.meta.t_start.tzinfo is not None              # epoch seconds -> UTC-aware
    assert 1 <= len(pkg.during) <= 6


def test_upload_worker_holds_grabs_while_laptop_is_down_then_delivers(laptop, tmp_path, monkeypatch):
    """The real UploadWorker thread (what main.py runs): laptop unreachable -> grab stays pending;
    laptop back -> delivered without anyone touching it."""
    import uploader
    from grab_cutter import GrabCutter

    url, dest = laptop
    pi_out = tmp_path / "pi_grabs"
    t0 = time.time()
    frames = [(t0 + i * 0.1, np.full((720, 1280, 3), i * 7 % 255, np.uint8)) for i in range(-20, 50)]
    folder = GrabCutter(output_dir=str(pi_out), source="webcam").cut(
        "cheese", t0, t0 + 3.0, _Buffer(frames), zone_polygon=[[0, 0], [99, 0], [99, 99]], camera_id="w")
    monkeypatch.setattr(uploader, "MAX_BACKOFF", 0.2)
    uploader.BASE_URL, uploader.UPLOAD_TOKEN = "http://127.0.0.1:9", "tok"     # laptop "down"
    w = uploader.UploadWorker(str(pi_out), poll=0.05)
    w.start()
    time.sleep(0.6)
    assert uploader.pending(str(pi_out)) == [folder]                         # held, not dropped
    uploader.BASE_URL = url                                                   # laptop back
    for _ in range(100):
        if (dest / Path(folder).name).exists():
            break
        time.sleep(0.05)
    w.stop_flag = True
    w.nudge()
    assert (dest / Path(folder).name / "meta.json").exists()
    assert uploader.pending(str(pi_out)) == []
