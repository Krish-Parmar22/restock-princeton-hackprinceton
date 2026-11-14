"""Drive the UNO Q LED matrix from the laptop over USB (no Wi-Fi).

  adb forward tcp:7500 localfilesystem:/var/run/arduino-router.sock   (done for you by `connect()`)
  then a msgpack-RPC call to the sketch's provided "show" function.

Codes: 0 idle, 1 logged (check), 2 needs your OK (?), 3 motion (eye), 4 below par (!)

  python led.py 1          # one-off
  python led.py --follow   # watch the API and mirror every new grab / below-par item
"""
from __future__ import annotations

import argparse
import itertools
import shutil
import socket
import subprocess
import time

import msgpack

PORT = 7500
IDLE, LOGGED, FLAGGED, MOTION, BELOW_PAR = range(5)
_ids = itertools.count(1)


def connect(port: int = PORT) -> None:
    adb = shutil.which("adb") or "/opt/homebrew/bin/adb"
    subprocess.run([adb, "forward", f"tcp:{port}", "localfilesystem:/var/run/arduino-router.sock"],
                   check=True, capture_output=True)


def show(code: int, port: int = PORT, timeout: float = 3.0) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        msgid = next(_ids)
        s.sendall(msgpack.packb([0, msgid, "show", [int(code)]]))
        unpacker = msgpack.Unpacker(raw=False)
        while True:
            data = s.recv(4096)
            if not data:
                raise ConnectionError("router closed the connection")
            unpacker.feed(data)
            for msg in unpacker:
                if isinstance(msg, list) and len(msg) == 4 and msg[0] == 1 and msg[1] == msgid:
                    if msg[2]:
                        raise RuntimeError(f"router error: {msg[2]}")
                    return


def follow(api: str, poll: float = 2.0) -> None:
    """Mirror the ledger onto the LEDs: check for a logged grab, ? for a flagged one, ! for below par."""
    import httpx
    from datetime import date

    seen: set[str] = set()
    first = True
    below: set[str] = set()
    while True:
        try:
            grabs = httpx.get(f"{api}/api/grabs", params={"date": date.today().isoformat()}, timeout=5).json()
            inv = httpx.get(f"{api}/api/inventory", timeout=5).json()
            new = [g for g in grabs if g["grab_id"] not in seen]
            seen.update(g["grab_id"] for g in grabs)
            now_below = {r["item"] for r in inv if r["below_par"]}
            if not first:
                for g in sorted(new, key=lambda g: g["grab_id"]):
                    code = LOGGED if g["status"] == "logged" else FLAGGED
                    print(f"[led] {g['grab_id']}: {g['status']} -> {'check' if code == LOGGED else '?'}")
                    show(code)
                    time.sleep(1.5)
                if now_below - below:
                    print(f"[led] below par: {sorted(now_below - below)} -> !")
                    show(BELOW_PAR)
            below, first = now_below, False
        except Exception as e:  # board unplugged, API restarting: keep trying
            print(f"[led] {e}")
            try:
                connect()
            except Exception:
                pass
        time.sleep(poll)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("code", nargs="?", type=int)
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    a = ap.parse_args()
    connect()
    if a.follow:
        follow(a.api)
    else:
        show(a.code if a.code is not None else LOGGED)
        print("ok")
