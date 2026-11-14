#!/usr/bin/env python3
"""Probe camera indexes 0–5 and save a snapshot from each working one."""

import cv2
import os
import sys

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots")


def discover():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    found = []

    for idx in range(6):
        # Use AVFoundation backend explicitly on macOS
        cap = cv2.VideoCapture(idx, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            print(f"  index {idx}: not available")
            cap.release()
            continue

        ret, frame = cap.read()
        if not ret or frame is None:
            print(f"  index {idx}: opened but no frame")
            cap.release()
            continue

        h, w = frame.shape[:2]
        path = os.path.join(SNAPSHOT_DIR, f"camera_{idx}.jpg")
        cv2.imwrite(path, frame)
        print(f"  index {idx}: {w}x{h} -> {path}")
        found.append({"index": idx, "width": w, "height": h, "path": path})
        cap.release()

    if not found:
        print("\nNo cameras found.")
        print("On macOS: grant camera access in System Settings > Privacy & Security > Camera")
        print("  for Terminal (or your IDE). Then re-run this script.")
        print("If using iPhone Continuity Camera, make sure it's connected.")
        sys.exit(1)

    print(f"\nFound {len(found)} camera(s). Check snapshots/ to identify which is which.")
    return found


if __name__ == "__main__":
    print("Probing camera indexes 0–5...")
    discover()
