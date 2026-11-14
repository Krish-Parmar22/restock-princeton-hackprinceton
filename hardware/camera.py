"""Open a camera the same way on macOS (laptop), Linux (Pi / Arduino UNO Q) and anything else.

Zones are pixel polygons, so the zone snapshot and the live run must use the SAME resolution.
Everything goes through open_camera() with the same default size for that reason.
"""
from __future__ import annotations

import sys

import cv2

DEFAULT_SIZE = (1280, 720)


def open_camera(index: int, size: tuple[int, int] | None = DEFAULT_SIZE):
    if sys.platform == "darwin":
        cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    elif sys.platform.startswith("linux"):
        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # USB webcams: MJPG gets 720p at full fps
    else:
        cap = cv2.VideoCapture(index)
    if size:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
    return cap


def read_frame(cap, tries: int = 10):
    """First frames from a USB webcam are often black or fail while it warms up."""
    frame = None
    for _ in range(tries):
        ok, f = cap.read()
        if ok and f is not None:
            frame = f
            if f.mean() > 5:
                break
    return frame


def zones_fit(zones: list[dict], width: int, height: int) -> list[str]:
    """Names of zones with points outside the frame (drawn at a different resolution)."""
    return [z["name"] for z in zones if any(not (0 <= x < width and 0 <= y < height) for x, y in z["points"])]
