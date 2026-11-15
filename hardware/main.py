#!/usr/bin/env python3
"""Part 1 Hardware: camera → grab packages.

Usage:
  python main.py                      # default: camera 0, iPhone source
  python main.py --camera 1           # use camera index 1
  python main.py --source webcam      # label as webcam in meta.json
  python main.py --no-record          # skip continuous recording
  python main.py --show               # show live preview window

First run `python zone_tool.py --camera <INDEX>` to define zones.json.
"""

import argparse
import json
import os
import shutil
import signal
import sys
import time

import cv2

from camera import open_camera, read_frame, zones_fit
from grab_cutter import GrabCutter
from motion_trigger import MotionTrigger
from pipeline import PiPipeline
from recorder import Recorder
import uploader

HERE = os.path.dirname(os.path.abspath(__file__))
ZONES_PATH = os.path.join(HERE, "zones.json")
GRABS_DIR = os.path.join(HERE, "grabs")
RECORDINGS_DIR = os.path.join(HERE, "recordings")

running = True


def signal_handler(sig, frame):
    global running
    print("\n[main] shutting down...")
    running = False


def check_disk_space(path, min_gb=1.0):
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024 ** 3)
    if free_gb < min_gb:
        print(f"[WARN] Only {free_gb:.1f} GB free on disk!")
        return False
    return True


def load_zones():
    if not os.path.exists(ZONES_PATH):
        print(f"[WARN] No zones.json found at {ZONES_PATH}")
        print("       Run `python zone_tool.py` first to define bin zones.")
        print("       Continuing without zones (no grabs will be produced).\n")
        return []
    with open(ZONES_PATH) as f:
        zones = json.load(f)
    print(f"[main] loaded {len(zones)} zone(s): {[z['name'] for z in zones]}")
    return zones


def main():
    global running

    parser = argparse.ArgumentParser(description="Part 1: camera → grab packages")
    parser.add_argument("--camera", type=int, default=0, help="Camera index (0=iPhone via USB-C, 1=built-in)")
    parser.add_argument("--source", default="iphone", choices=["iphone", "webcam"], help="Source label for meta.json")
    parser.add_argument("--no-record", action="store_true", help="Skip continuous recording")
    parser.add_argument("--show", action="store_true", help="Show live preview window")
    parser.add_argument("--fps", type=float, default=15.0, help="Target capture FPS")
    parser.add_argument("--api-url", default=os.getenv("UPLOAD_URL", ""), help="Laptop API base URL, e.g. http://192.168.1.20:8000 (empty=no uploads)")
    parser.add_argument("--upload-token", default=os.getenv("UPLOAD_TOKEN", ""), help="Shared secret sent as X-Upload-Token")
    parser.add_argument("--camera-id", default="pi-makeline-1", help="Fixed name for this camera in meta.json")
    parser.add_argument("--stream-port", type=int, default=0, help="Serve a live view + box editor on this port (e.g. 8090)")
    parser.add_argument("--stream-key", default="", help="Password for the live view (or CAMERA_KEY env). Set it before sharing a public link.")
    parser.add_argument("--snapshot", metavar="PATH", help="Save one frame to PATH and exit (draw zones from it on a laptop)")
    args = parser.parse_args()

    worker = None
    if args.api_url:
        uploader.BASE_URL = args.api_url
        uploader.UPLOAD_TOKEN = args.upload_token
        worker = uploader.UploadWorker(GRABS_DIR)  # also resends anything unsent from before a restart
        worker.start()
        print(f"[main] uploads enabled → {args.api_url}/api/grabs/upload")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # open camera (AVFoundation on macOS, V4L2 on the Pi / UNO Q)
    cap = open_camera(args.camera)
    if not cap.isOpened():
        print(f"[ERROR] Cannot open camera {args.camera}")
        print("macOS: grant camera access in System Settings > Privacy & Security > Camera. Linux: ls /dev/video*")
        sys.exit(1)

    # grab one frame to get dimensions
    test_frame = read_frame(cap)
    if test_frame is None:
        print("[ERROR] Camera opened but cannot read frames")
        sys.exit(1)
    h, w = test_frame.shape[:2]
    print(f"[main] camera {args.camera}: {w}x{h}, source={args.source}")

    if args.snapshot:   # one frame for drawing zones elsewhere (headless boards): zone_tool.py --image
        cv2.imwrite(args.snapshot, test_frame)
        print(f"[main] saved snapshot {args.snapshot} ({w}x{h})")
        return

    # load zones
    zones = load_zones()
    bad = zones_fit(zones, w, h)
    if bad:
        print(f"[ERROR] zones {bad} fall outside the {w}x{h} frame: they were drawn at another resolution. Redraw them.")
        sys.exit(1)
    zone_points = {z["name"]: z["points"] for z in zones}

    # init modules
    cutter = GrabCutter(output_dir=GRABS_DIR, source=args.source)
    recorder = None if args.no_record else Recorder(
        output_dir=RECORDINGS_DIR, fps=args.fps, resolution=(w, h)
    )
    trigger = MotionTrigger(zones, test_frame.shape) if zones else None
    pipe = PiPipeline(zones, test_frame.shape, cutter, camera_id=args.camera_id, trigger=trigger) if zones else None

    stream = None
    if args.stream_port:   # live feed + box editor in the laptop's browser (headless boards)
        from stream import StreamState, serve
        items = None
        if args.api_url:   # box names must be inventory items: offer exactly the API's list
            try:
                import requests
                items = sorted(r["item"] for r in requests.get(f"{args.api_url.rstrip('/')}/api/inventory", timeout=5).json())
            except Exception as e:
                print(f"[main] couldn't read items from the API ({e}); using the default list")
        stream = StreamState(zones, w, h, key=args.stream_key or os.getenv("CAMERA_KEY", ""), items=items)
        serve(stream, args.stream_port)
        print(f"[main] live view + box editor: http://<this-board>:{args.stream_port}"
              + (" (password protected)" if (args.stream_key or os.getenv("CAMERA_KEY")) else ""))

    zone_state: dict[str, bool] = {}
    frame_interval = 1.0 / args.fps
    grab_count = 0
    frame_num = 0
    disk_check_interval = 60  # check disk every 60s
    last_disk_check = 0

    print(f"[main] running. {'recording' if recorder else 'no recording'}. "
          f"{'preview on' if args.show else 'preview off'}. Ctrl+C to stop.\n")

    while running:
        t_loop_start = time.time()

        ret, frame = cap.read()
        if not ret:
            fails = locals().get("fails", 0) + 1
            if fails % 20 == 1:
                print("[WARN] frame read failed, retrying...")
            if fails >= 15:   # ~1.5 s of dead reads: the USB link dropped (cable/hub bump). Reopen the camera.
                cap.release()
                time.sleep(1.0)
                cap = open_camera(args.camera)
                print(f"[camera] reconnecting to camera {args.camera}: {'ok' if cap.isOpened() else 'not yet'}")
                fails = 0
            time.sleep(0.1)
            continue
        fails = 0

        now = time.time()
        frame_num += 1


        # record
        if recorder:
            recorder.write_frame(frame)

        # motion detection -> grab packages (before snapshotted at motion start)
        if pipe:
            folders = pipe.process(frame, now)
            for z in trigger.zones:   # live status, so a stuck or silent zone is obvious
                was = zone_state.get(z.name, False)
                if z.active != was:
                    print(f"[motion] {z.name}: {'moving' if z.active else 'still'}"
                          + ("" if z.active else f" after {now - z.motion_start:.1f}s ({'grab' if z.triggered or folders else 'too short / waiting'})"))
                    zone_state[z.name] = z.active
            for folder in folders:
                grab_count += 1
                if worker:
                    worker.nudge()

        # browser live view; boxes saved there are swapped in here
        if stream:
            new = stream.take_pending()
            if new is not None:
                if pipe:
                    for folder in pipe.flush():   # ship any grab waiting out its merge window first
                        grab_count += 1
                        if worker:
                            worker.nudge()
                tmp = ZONES_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(new, f, indent=1)
                os.replace(tmp, ZONES_PATH)
                zones = new
                trigger = MotionTrigger(zones, frame.shape) if zones else None
                pipe = PiPipeline(zones, frame.shape, cutter, camera_id=args.camera_id, trigger=trigger) if zones else None
                zone_state = {}
                with stream.lock:
                    stream.zones = zones
                print(f"[main] new boxes from the browser: {[z['name'] for z in zones]} (saved to zones.json)")
            active = {z.name: z.active for z in trigger.zones} if trigger else {}
            stream.publish(frame, active, f"grabs: {grab_count}" + ("" if zones else "  (no boxes yet)"))

        # live preview
        if args.show:
            display = frame.copy()
            if trigger:
                for z in trigger.zones:
                    color = (0, 0, 255) if z.active else (0, 255, 0)
                    cv2.polylines(display, [z.polygon.reshape((-1, 1, 2))], True, color, 2)
                    cv2.putText(display, z.name, tuple(z.polygon[0]),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            cv2.putText(display, f"grabs: {grab_count}  frames: {frame_num}",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            cv2.imshow("Part 1 - Live", display)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                running = False

        # periodic disk check
        if now - last_disk_check > disk_check_interval:
            check_disk_space(HERE)
            last_disk_check = now

        # throttle to target fps
        elapsed = time.time() - t_loop_start
        sleep_time = frame_interval - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)

    # cleanup: finalize grabs still inside their merge window so nothing is lost on Ctrl+C
    if pipe:
        grab_count += len(pipe.flush())
    print(f"\n[main] stopped. {frame_num} frames, {grab_count} grabs.")
    cap.release()
    if recorder:
        recorder.release()
    if args.show:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
