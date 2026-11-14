"""LIVE SYSTEM TEST: real processes, real models, the Pi's own motion detection.

  synthetic 15 fps "camera" video (real photos: a hand reaches into the pepperoni bin and takes slices)
    -> hardware MotionTrigger + FrameBuffer + GrabCutter (the Pi's code, unmodified loop)
    -> hardware UploadWorker -> HTTP -> a real uvicorn API process (own DB, watcher inside)
    -> real YOLO-World + real d1 -> Postgres -> below-par alert -> a mock Photon bridge (/send)
  plus: the laptop is DOWN when the grab happens (the Pi must hold it and deliver later), and
  concurrent owner texts from several chats hit the real agent at once.

Opt-in:  RUN_E2E=1 uv run pytest tests/e2e/test_live_system.py -q -s
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import psycopg
import pytest

if os.getenv("RUN_E2E") != "1":
    pytest.skip("live system test is opt-in: RUN_E2E=1", allow_module_level=True)

import httpx  # noqa: E402

from app.config import ROOT, settings  # noqa: E402
from app.db.conn import apply_schema  # noqa: E402
from app.ledger.seed import seed  # noqa: E402
from tests.e2e.scene import ZONES, remove_slices, scene, with_hand  # noqa: E402

sys.path.insert(0, str(ROOT / "hardware"))
import uploader  # noqa: E402
from grab_cutter import GrabCutter  # noqa: E402

DB = "pizza_live"
URL = f"postgresql://localhost:5432/{DB}"
TOKEN = "live-token"
FPS = 15


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class MockBridge(BaseHTTPRequestHandler):
    sent: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        MockBridge.sent.append(body)
        out = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    if not settings.openrouter_api_key or not settings.agent_model:
        pytest.skip("needs OPENROUTER_API_KEY + AGENT_MODEL")
    with psycopg.connect("postgresql://localhost:5432/postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DB}")
    apply_schema(URL)
    seed(URL, reset=True)
    bport, aport = _port(), _port()
    bridge = HTTPServer(("127.0.0.1", bport), MockBridge)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    grabs = tmp_path_factory.mktemp("laptop_grabs")
    env = {**os.environ, "DATABASE_URL": URL, "GRABS_DIR": str(grabs), "UPLOAD_TOKEN": TOKEN,
           "WATCHER_IN_SERVER": "true", "BRIDGE_URL": f"http://127.0.0.1:{bport}", "LANGSMITH_TRACING": "false"}
    log = open(tmp_path_factory.mktemp("logs") / "api.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api.server:app", "--host", "127.0.0.1",
                             "--port", str(aport)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{aport}"
    for _ in range(240):  # YOLO weights load at startup
        try:
            if httpx.get(f"{base}/health", timeout=1).json().get("watcher", {}).get("alive"):
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        pytest.fail(f"API never came up; see {log.name}")
    yield {"base": base, "grabs": grabs, "log": log.name}
    proc.terminate()
    proc.wait(10)
    bridge.shutdown()


# ---------------------------------------------------------------- the synthetic camera

def camera_video():
    """(timestamp, BGR frame) at 15 fps: 6 s idle (MOG2 learns the scene), a hand reaches into the
    pepperoni bin over 1.5 s, holds 0.6 s, leaves over 1.0 s, then the bin is emptier for 12 s."""
    import cv2
    s, after = scene(), remove_slices(scene())
    t0 = time.time()
    frames, t = [], t0

    def add(img, n):
        nonlocal t
        bgr = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
        for _ in range(n):
            frames.append((t, bgr))
            t += 1 / FPS

    add(s, 6 * FPS)
    for d in np.linspace(0.0, 0.9, int(1.5 * FPS)):
        add(with_hand(s, "pepperoni", float(d)), 1)
    add(with_hand(after, "pepperoni", 0.9), int(0.6 * FPS))
    for d in np.linspace(0.9, -0.6, int(1.0 * FPS)):
        add(with_hand(after, "pepperoni", float(d)), 1)
    add(after, 12 * FPS)
    return frames


def run_pi(frames, out_dir: Path) -> list[str]:
    """Exactly the Pi's production loop (hardware/pipeline.PiPipeline, used by main.py)."""
    from pipeline import PiPipeline
    zones = [{"name": "pepperoni", "points": ZONES["pepperoni"]}]
    pipe = PiPipeline(zones, frames[0][1].shape, GrabCutter(output_dir=str(out_dir), source="webcam"),
                      camera_id="live-test")
    folders = []
    for ts, f in frames:
        folders += pipe.process(f, ts)
    return folders


def _hand_in_zone(img_path: str) -> float:
    """Mean abs difference from the clean scene inside the zone (0 = identical, hand absent)."""
    import cv2
    clean = cv2.cvtColor(np.asarray(scene()), cv2.COLOR_RGB2BGR).astype(float)
    img = cv2.imread(img_path).astype(float)
    (x0, y0), (x1, y1) = ZONES["pepperoni"][0], ZONES["pepperoni"][2]
    # the top half of the zone (where the hand enters) and above it
    return float(np.abs(img[max(0, y0 - 120):y0 + 40, x0:x1] - clean[max(0, y0 - 120):y0 + 40, x0:x1]).mean())


# ---------------------------------------------------------------- tests

def test_1_pi_motion_detects_exactly_one_grab_with_clean_before(live, tmp_path):
    frames = camera_video()
    folders = run_pi(frames, tmp_path / "pi")
    print(f"\nPi produced {len(folders)} package(s): {[Path(f).name for f in folders]}")
    assert len(folders) == 1, "one reach-in should be exactly one grab"
    meta = json.loads((Path(folders[0]) / "meta.json").read_text())
    dur = meta["t_end"] - meta["t_start"]
    before_diff = _hand_in_zone(str(Path(folders[0]) / "before.jpg"))
    print(f"grab lasted {dur:.1f}s; before-frame difference vs clean scene: {before_diff:.1f}")
    assert meta["zone_polygon"] == ZONES["pepperoni"]
    assert before_diff < 4.0, "BEFORE frame must show the bin before the hand arrived"
    live["pi_folder"] = folders[0]


def test_2_laptop_down_then_up_grab_is_delivered(live):
    folder = live["pi_folder"]
    uploader.BASE_URL, uploader.UPLOAD_TOKEN = "http://127.0.0.1:9", TOKEN   # nothing listens on :9
    assert uploader.deliver(folder) == "retry" and uploader.pending(str(Path(folder).parent)) == [folder]
    uploader.BASE_URL = live["base"]
    assert uploader.deliver(folder) == "sent"


def test_3_live_watcher_runs_real_d1_and_yolo(live):
    gid = Path(live["pi_folder"]).name
    day = datetime.now(timezone.utc).astimezone().date().isoformat()
    row = None
    for _ in range(120):
        rows = [g for g in httpx.get(f"{live['base']}/api/grabs", params={"date": day}).json() if g["grab_id"] == gid]
        if rows:
            row = rows[0]
            break
        time.sleep(1)
    assert row, f"watcher never recorded {gid}; see {live['log']}"
    print(f"\nlive grab -> action={row['action']} item={row['item']} status={row['status']} conf={row['confidence']}")
    assert row["action"] == "took" and row["item"] == "pepperoni"


def test_4_concurrent_owner_texts(live):
    chats = [f"live-owner-{i}" for i in range(4)]
    q = ["how much pepperoni do we have?", "how much cheese do we have?",
         "how much dough do we have?", "how much sauce do we have?"]

    def one(i):
        t = time.time()
        r = httpx.post(f"{live['base']}/inbound", timeout=180, json={
            "chat_id": chats[i], "sender": f"+1555000{i}", "text": q[i], "ts": datetime.now(timezone.utc).isoformat()})
        return i, r.status_code, r.json().get("reply"), time.time() - t

    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(one, range(4)))
    for i, code, reply, dt in results:
        print(f"{chats[i]} ({dt:.1f}s): {reply}")
        assert code == 200 and reply and "snag" not in reply
    item_words = ["pepperoni", "cheese", "dough", "sauce"]
    assert all(item_words[i] in (r or "").lower() for i, _, r, _ in results), "each chat got its own answer"


def test_5_below_par_alert_reaches_the_bridge(live):
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("UPDATE items SET par = 1000 WHERE name = 'pepperoni'")   # force below par
    MockBridge.sent.clear()
    # a new grab triggers the watcher's after-poll alert check
    import shutil
    src = Path(live["pi_folder"])
    dup = src.parent / (src.name + "-again")
    shutil.copytree(src, dup, ignore=shutil.ignore_patterns(".sent"))
    m = json.loads((dup / "meta.json").read_text())
    m["grab_id"] = dup.name
    (dup / "meta.json").write_text(json.dumps(m))
    uploader.BASE_URL = live["base"]
    assert uploader.deliver(str(dup)) == "sent"
    for _ in range(90):
        if len({s["chat_id"] for s in MockBridge.sent if "pepperoni" in s["text"]}) == 4:
            break
        time.sleep(1)
    print(f"\nbridge received: {MockBridge.sent}")
    alerts = [s for s in MockBridge.sent if "pepperoni" in s["text"]]
    assert {a["chat_id"] for a in alerts} == {f"live-owner-{i}" for i in range(4)}, "every owner chat gets it"
    assert len(alerts) == 4, "exactly once each (cooldown is per chat)"
