"""Upload grab packages to the backend API.

Each grab folder (before.jpg, during_*.jpg, after.jpg, meta.json) is
POSTed as a multipart form to POST {BASE_URL}/upload.

Uploads run in a background thread so they never block the camera loop.
Set BASE_URL before calling upload_grab(), or pass --api-url to main.py.
"""

import glob
import json
import logging
import os
import threading
from datetime import datetime

import cv2
import requests

log = logging.getLogger(__name__)

BASE_URL = ""  # set by main.py via --api-url

# map zone names → numeric IDs for the backend
ZONE_MAP: dict[str, int] = {}
_next_zone_id = [1]


def _zone_id(name: str) -> int:
    """Auto-assign a stable numeric ID per zone name."""
    if name not in ZONE_MAP:
        ZONE_MAP[name] = _next_zone_id[0]
        _next_zone_id[0] += 1
    return ZONE_MAP[name]


def upload_grab(grab_dir: str, *, background: bool = True) -> None:
    """Upload a grab package folder.

    Args:
        grab_dir: path to the grab folder
        background: if True (default), upload in a daemon thread.
                    Set False for testing to run synchronously.
    """
    if not BASE_URL:
        return
    if background:
        t = threading.Thread(target=_do_upload, args=(grab_dir,), daemon=True)
        t.start()
    else:
        _do_upload(grab_dir)


def _do_upload(grab_dir: str) -> None:
    """POST each image in the grab package to {BASE_URL}/upload.

    Reads meta.json for zone/timestamps, resizes each image to 640x480 PNG,
    and sends as multipart POST. Errors are logged, never raised.
    """
    meta_path = os.path.join(grab_dir, "meta.json")
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except Exception as e:
        log.error("[upload] failed to read %s: %s", meta_path, e)
        return

    zone_name = meta.get("zone", "")
    zone_num = _zone_id(zone_name)
    t_start = meta.get("t_start", 0)
    timestamp = datetime.fromtimestamp(t_start).isoformat()
    grab_id = meta.get("grab_id", os.path.basename(grab_dir))

    # collect image files in order: before, during_*, after
    images = []
    before = os.path.join(grab_dir, "before.jpg")
    if os.path.exists(before):
        images.append(before)
    images.extend(sorted(glob.glob(os.path.join(grab_dir, "during_*.jpg"))))
    after = os.path.join(grab_dir, "after.jpg")
    if os.path.exists(after):
        images.append(after)

    url = f"{BASE_URL.rstrip('/')}/upload"
    uploaded = 0

    for img_path in images:
        try:
            frame = cv2.imread(img_path)
            if frame is None:
                log.warning("[upload] could not read %s", img_path)
                continue
            resized = cv2.resize(frame, (640, 480))
            _, png_bytes = cv2.imencode(".png", resized)

            resp = requests.post(
                url,
                files={"image": (os.path.basename(img_path), png_bytes.tobytes(), "image/png")},
                data={
                    "zone": zone_num,
                    "zone_name": zone_name,
                    "time": timestamp,
                    "grab_id": grab_id,
                },
                timeout=10,
            )
            if resp.ok:
                uploaded += 1
            else:
                log.warning("[upload] %s → %s %s", os.path.basename(img_path), resp.status_code, resp.reason)
        except requests.ConnectionError:
            log.error("[upload] connection refused for %s — is the backend running at %s?", grab_id, BASE_URL)
            return  # don't retry the rest if server is down
        except Exception as e:
            log.error("[upload] failed %s: %s", os.path.basename(img_path), e)

    if uploaded:
        print(f"[upload] {grab_id}: {uploaded}/{len(images)} frames sent")
