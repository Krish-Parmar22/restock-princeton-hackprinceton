#!/usr/bin/env python3
"""Interactive zone definition tool.

Usage: python zone_tool.py [--camera INDEX]

Everything happens in the window — no terminal interaction needed.

  Left-click     — add a corner point
  Right-click    — undo last point
  Enter          — finish polygon and name it (type name, then Enter again)
  Backspace      — delete the last saved zone
  S              — save zones.json and quit
  Q / Esc        — quit without saving
"""

import cv2
import json
import os
import sys
import argparse
import numpy as np

ZONES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zones.json")

# Zone colors (cycle through for visual distinction)
COLORS = [
    (0, 255, 0),    # green
    (255, 165, 0),  # orange (BGR: 0, 165, 255)
    (0, 255, 255),  # yellow
    (255, 0, 255),  # magenta
    (255, 255, 0),  # cyan
    (0, 128, 255),  # orange
]
# Fix BGR ordering
COLORS = [
    (0, 255, 0),
    (0, 165, 255),
    (0, 255, 255),
    (255, 0, 255),
    (255, 255, 0),
    (255, 128, 0),
]


class ZoneTool:
    def __init__(self, frame):
        self.frame_clean = frame.copy()
        self.frame_display = frame.copy()
        self.zones = []          # completed: [{"name": str, "points": [[x,y],...]}]
        self.points = []         # current polygon points
        self.naming = False      # are we in naming mode?
        self.name_buf = ""       # text being typed for the name
        self.mouse_pos = (0, 0)  # for live preview line
        self.status_msg = ""     # temporary status message
        self.window = "Zone Tool  |  click bin corners, Enter to finish, S to save"

    def color_for(self, idx):
        return COLORS[idx % len(COLORS)]

    def mouse_cb(self, event, x, y, flags, param):
        self.mouse_pos = (x, y)

        if self.naming:
            return  # ignore clicks while naming

        if event == cv2.EVENT_LBUTTONDOWN:
            self.points.append([x, y])
        elif event == cv2.EVENT_RBUTTONDOWN:
            if self.points:
                self.points.pop()

    def draw(self):
        self.frame_display = self.frame_clean.copy()
        h, w = self.frame_display.shape[:2]

        # draw completed zones with semi-transparent fill
        overlay = self.frame_display.copy()
        for i, zone in enumerate(self.zones):
            color = self.color_for(i)
            pts = np.array(zone["points"], np.int32)
            cv2.fillPoly(overlay, [pts], (*color[:3],))
            cv2.polylines(self.frame_display, [pts.reshape((-1, 1, 2))], True, color, 2)
            # label
            cx = int(np.mean(pts[:, 0]))
            cy = int(np.mean(pts[:, 1]))
            label = f"{i+1}. {zone['name']}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
            cv2.rectangle(self.frame_display, (cx - tw//2 - 4, cy - th - 4),
                          (cx + tw//2 + 4, cy + 6), (0, 0, 0), -1)
            cv2.putText(self.frame_display, label, (cx - tw//2, cy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        # blend overlay for fill
        cv2.addWeighted(overlay, 0.15, self.frame_display, 0.85, 0, self.frame_display)

        # draw current in-progress polygon
        if self.points:
            color = self.color_for(len(self.zones))
            for i, p in enumerate(self.points):
                cv2.circle(self.frame_display, tuple(p), 6, color, -1)
                cv2.circle(self.frame_display, tuple(p), 6, (255, 255, 255), 1)
                if i > 0:
                    cv2.line(self.frame_display, tuple(self.points[i-1]), tuple(p), color, 2)
            # live preview line from last point to mouse
            if not self.naming:
                cv2.line(self.frame_display, tuple(self.points[-1]),
                         self.mouse_pos, color, 1, cv2.LINE_AA)
                # close preview line
                cv2.line(self.frame_display, tuple(self.points[0]),
                         self.mouse_pos, color, 1, cv2.LINE_AA)

        # HUD bar at top
        bar_h = 40
        cv2.rectangle(self.frame_display, (0, 0), (w, bar_h), (30, 30, 30), -1)

        if self.naming:
            hud = f"Type zone name: {self.name_buf}_    (Enter=confirm, Esc=cancel)"
            hud_color = (0, 255, 255)
        elif self.points:
            hud = f"Drawing zone {len(self.zones)+1}  ({len(self.points)} pts)  |  click=add  right-click=undo  Enter=finish"
            hud_color = (200, 200, 200)
        else:
            hud = f"{len(self.zones)} zone(s) defined  |  click=start new zone  S=save  Q=quit"
            hud_color = (200, 200, 200)

        cv2.putText(self.frame_display, hud, (10, 27),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, hud_color, 1, cv2.LINE_AA)

        # status message at bottom
        if self.status_msg:
            cv2.rectangle(self.frame_display, (0, h - 35), (w, h), (30, 30, 30), -1)
            cv2.putText(self.frame_display, self.status_msg, (10, h - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (100, 200, 255), 1, cv2.LINE_AA)

        # zone list on right side
        for i, zone in enumerate(self.zones):
            y_pos = bar_h + 25 + i * 25
            color = self.color_for(i)
            text = f"{i+1}. {zone['name']} ({len(zone['points'])} pts)"
            cv2.putText(self.frame_display, text, (w - 280, y_pos),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    def handle_key(self, key):
        if key == -1 or key == 255:
            return True  # no key

        if self.naming:
            return self._handle_naming_key(key)

        # Enter — finish current polygon
        if key == 13:  # Enter
            if len(self.points) < 3:
                self.status_msg = "Need at least 3 points! Keep clicking corners."
                return True
            self.naming = True
            self.name_buf = ""
            self.status_msg = ""
            return True

        # Backspace — delete last zone
        if key == 8 or key == 127:
            if self.points:
                self.points.pop()
            elif self.zones:
                removed = self.zones.pop()
                self.status_msg = f"Deleted zone '{removed['name']}'"
            return True

        # S — save
        if key == ord("s") or key == ord("S"):
            if not self.zones:
                self.status_msg = "No zones to save! Draw at least one zone first."
                return True
            with open(ZONES_PATH, "w") as f:
                json.dump(self.zones, f, indent=2)
            print(f"\nSaved {len(self.zones)} zone(s) to {ZONES_PATH}")
            for i, z in enumerate(self.zones):
                print(f"  {i+1}. {z['name']} ({len(z['points'])} points)")
            return False  # exit

        # Q or Esc — quit
        if key == ord("q") or key == ord("Q") or key == 27:
            print("\nQuit without saving.")
            return False

        return True

    def _handle_naming_key(self, key):
        if key == 27:  # Esc — cancel naming
            self.naming = False
            self.status_msg = "Naming cancelled. Keep adding points or press Enter to try again."
            return True

        if key == 13:  # Enter — confirm name
            name = self.name_buf.strip()
            if not name:
                self.status_msg = "Name can't be empty! Type a name then press Enter."
                return True
            self.zones.append({"name": name, "points": list(self.points)})
            self.status_msg = f"Zone '{name}' saved! Click to start next zone, or S to save all."
            self.points = []
            self.naming = False
            self.name_buf = ""
            return True

        if key == 8 or key == 127:  # Backspace
            self.name_buf = self.name_buf[:-1]
            return True

        # printable ASCII
        if 32 <= key <= 126:
            self.name_buf += chr(key)
            return True

        return True

    def run(self):
        cv2.namedWindow(self.window, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.window, 1280, 720)
        cv2.setMouseCallback(self.window, self.mouse_cb)

        while True:
            self.draw()
            cv2.imshow(self.window, self.frame_display)
            key = cv2.waitKey(30) & 0xFF
            if not self.handle_key(key):
                break

        cv2.destroyAllWindows()


def main():
    parser = argparse.ArgumentParser(description="Define bin zones on camera feed")
    parser.add_argument("--camera", type=int, default=0,
                        help="Camera index (0=iPhone via USB-C, 1=built-in)")
    parser.add_argument("--load", action="store_true",
                        help="Load existing zones.json for editing")
    parser.add_argument("--image", help="Draw on a saved snapshot instead of a live camera "
                                        "(from `main.py --snapshot` on a headless board)")
    args = parser.parse_args()

    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            print(f"Cannot read {args.image}")
            sys.exit(1)
    else:
        from camera import open_camera, read_frame
        cap = open_camera(args.camera)
        if not cap.isOpened():
            print(f"Cannot open camera {args.camera}")
            sys.exit(1)
        frame = read_frame(cap)
        cap.release()
        if frame is None:
            print("Failed to grab frame")
            sys.exit(1)
    print(f"Drawing on a {frame.shape[1]}x{frame.shape[0]} frame. Name zones after the ingredient: pepperoni, cheese, dough, sauce.")

    tool = ZoneTool(frame)

    # optionally load existing zones
    if args.load and os.path.exists(ZONES_PATH):
        with open(ZONES_PATH) as f:
            tool.zones = json.load(f)
        print(f"Loaded {len(tool.zones)} existing zone(s)")

    tool.run()


if __name__ == "__main__":
    main()
