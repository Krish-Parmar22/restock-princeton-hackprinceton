"""Browser live view + box editor (hardware/stream.py), offline."""
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stream import StreamState, serve  # noqa: E402


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_validate_rejects_bad_boxes():
    s = StreamState([], 640, 480)
    assert s.validate([{"name": "pepperoni", "points": [[1, 1], [100, 1], [100, 100]]}]) is None
    assert "one of" in s.validate([{"name": "glass", "points": [[1, 1], [2, 2], [3, 3]]}])
    assert "outside" in s.validate([{"name": "cheese", "points": [[1, 1], [900, 1], [5, 5]]}])
    assert "3 corners" in s.validate([{"name": "cheese", "points": [[1, 1], [2, 2]]}])
    assert "two boxes" in s.validate([{"name": "cheese", "points": [[1, 1], [9, 1], [5, 5]]}] * 2)


def test_page_frame_and_zone_save_round_trip():
    zones = [{"name": "cheese", "points": [[10, 10], [200, 10], [200, 200], [10, 200]]}]
    s = StreamState(zones, 640, 480, key="k")
    port = _port()
    srv = serve(s, port)
    try:
        base = f"http://127.0.0.1:{port}"
        s.publish(np.full((480, 640, 3), 90, np.uint8), {"cheese": True}, "grabs: 0")
        page = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert "Bin camera" in page and "<option>pepperoni</option>" in page and "640" in page
        assert urllib.request.urlopen(base + "/frame.jpg", timeout=5).read()[:2] == b"\xff\xd8"
        assert json.loads(urllib.request.urlopen(base + "/zones", timeout=5).read()) == zones

        new = [{"name": "pepperoni", "points": [[5, 5], [300, 5], [300, 300]]}]
        code, body = _post(base + "/zones", new)
        assert code == 403, "saving needs the key when an upload token is set"
        code, body = _post(base + "/zones?key=k", new)
        assert code == 200 and s.take_pending() == new and s.take_pending() is None

        r = urllib.request.urlopen(base + "/stream", timeout=5)
        assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
        assert b"--frame" in r.read(200)
        r.close()
    finally:
        srv.shutdown()
        time.sleep(0.1)
