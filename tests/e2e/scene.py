"""Build realistic grab packages from two real photos (sources: fixtures.md).

Scene: an overhead shot of a topping spread. Bin zones, boxed by hand from the photo:
  pepperoni: the salami/pepperoni slice stack (bottom-left board)
  cheese:    the bowl of shredded cheese (top-left)
A real overhead hand photo is cut out and pasted reaching into a zone for the DURING frames.
"after" for a take = the slice stack with part of it covered by plain board texture.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from tests.e2e.fetch_fixtures import fetch

ZONES = {
    "pepperoni": [[290, 400], [462, 400], [462, 565], [290, 565]],
    "cheese": [[95, 5], [368, 5], [368, 172], [95, 172]],
}


def scene() -> Image.Image:
    return Image.open(fetch() / "scene_toppings.jpg").convert("RGB")


def hand_cutout(width: int = 300) -> Image.Image:
    """RGBA hand (wrist at top, fingers pointing down), background removed by colour."""
    im = Image.open(fetch() / "hand_overhead.jpg").convert("RGB")
    a = np.asarray(im).astype(int)
    r, b = a[..., 0], a[..., 2]
    mask = ((r - b) > 28) & (r > 95)  # skin vs the bluish-white backdrop
    m = Image.fromarray((mask * 255).astype("uint8")).filter(ImageFilter.MedianFilter(5)).filter(ImageFilter.GaussianBlur(1.5))
    rgba = im.copy()
    rgba.putalpha(m)
    return rgba.resize((width, int(im.height * width / im.width)), Image.LANCZOS)


def with_hand(base: Image.Image, zone: str, depth: float) -> Image.Image:
    """Paste the hand reaching down into `zone`; depth 0..1 = how far the fingertips go in."""
    out = base.copy()
    hand = hand_cutout()
    (x0, y0), (x1, y1) = ZONES[zone][0], ZONES[zone][2]
    left = (x0 + x1) // 2 - hand.width // 2
    tip_y = int(y0 + (y1 - y0) * (0.25 + 0.6 * depth))
    top = tip_y - hand.height
    wrist = hand.crop((0, 0, hand.width, 12))  # forearm: repeat the wrist strip up to the frame edge
    y = top
    while y > -12:
        y -= 12
        out.paste(wrist, (left, y), wrist)
    out.paste(hand, (left, top), hand)
    return out


def remove_slices(base: Image.Image, fraction: float = 0.75) -> Image.Image:
    """Make slices really disappear: find the red/pink salami pixels in the right `fraction` of the
    pepperoni zone and replace them with bare cutting-board texture from the same photo, feathered
    at the edges, so the bin looks genuinely emptier (not a pasted-on patch)."""
    from PIL import ImageFilter as F

    img = np.asarray(base).astype(float)
    (x0, y0), (x1, y1) = ZONES["pepperoni"][0], ZONES["pepperoni"][2]
    cut_x = int(x1 - (x1 - x0) * fraction)
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    salami = (r > 100) & ((r - g) > 30) & ((r - b) > 15)
    region = np.zeros(salami.shape, bool)
    region[y0 - 15:y1 + 15, cut_x:x1 + 15] = True
    m = Image.fromarray(((salami & region) * 255).astype("uint8"))
    m = m.filter(F.MaxFilter(9)).filter(F.MaxFilter(9)).filter(F.GaussianBlur(4))
    alpha = np.asarray(m).astype(float)[..., None] / 255.0
    # bare board, tiled from a clean strip of the same board (below the food)
    strip = np.asarray(base.crop((20, 600, 460, 700))).astype(float)
    board = np.zeros_like(img)
    for yy in range(0, img.shape[0], strip.shape[0]):
        for xx in range(0, img.shape[1], strip.shape[1]):
            h = min(strip.shape[0], img.shape[0] - yy)
            w = min(strip.shape[1], img.shape[1] - xx)
            board[yy:yy + h, xx:xx + w] = strip[:h, :w]
    out = img * (1 - alpha) + board * alpha
    return Image.fromarray(out.clip(0, 255).astype("uint8"))


def write_package(root: Path, grab_id: str, zone: str, before: Image.Image, during: list[Image.Image],
                  after: Image.Image, t0: datetime) -> Path:
    d = root / grab_id
    d.mkdir(parents=True)
    before.save(d / "before.jpg", quality=88)
    for i, f in enumerate(during, 1):
        f.save(d / f"during_{i:02d}.jpg", quality=88)
    after.save(d / "after.jpg", quality=88)
    times = [t0 - timedelta(seconds=1)] + [t0 + timedelta(seconds=0.5 * i) for i in range(1, len(during) + 1)]
    t_end = times[-1] + timedelta(seconds=0.5)
    times.append(t_end + timedelta(seconds=1))
    (d / "meta.json").write_text(json.dumps({
        "grab_id": grab_id, "camera_id": "e2e-cam", "zone": zone,
        "t_start": t0.isoformat(), "t_end": t_end.isoformat(),
        "frame_times": [t.isoformat() for t in times], "source": "webcam",
        "zone_polygon": ZONES[zone],
    }, indent=1))
    return d


def build_all(root: Path, t0: datetime | None = None) -> dict[str, Path]:
    """The scenario set. Returns {scenario: folder}."""
    t0 = t0 or datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=30)
    s = scene()
    fewer = remove_slices(s)
    reach = [with_hand(s, "pepperoni", d) for d in (0.2, 0.6, 0.9)]
    reach_fewer = [with_hand(fewer, "pepperoni", d) for d in (0.2, 0.6, 0.9)]

    def at(m: int) -> tuple[str, datetime]:
        t = t0 + timedelta(minutes=m)
        return t.strftime("%Y-%m-%dT%H-%M-%S"), t

    out = {}
    for name, minute, zone, before, during, after in [
        ("took", 0, "pepperoni", s, reach, fewer),            # slices gone: took, auto-log
        ("touched", 2, "pepperoni", s, reach, s),             # nothing changed: touched
        ("put_back", 4, "pepperoni", fewer, reach_fewer, s),  # slices come back: put_back
        ("no_hand", 6, "pepperoni", s, [s, s, s], fewer),     # change but no hand anywhere: YOLO must flag
        ("wrong_zone", 8, "cheese", s, reach, fewer),         # Pi said cheese, hand was in pepperoni: flag
    ]:
        stamp, t = at(minute)
        out[name] = write_package(root, f"{stamp}_{zone}", zone, before, during, after, t)
    return out


if __name__ == "__main__":
    import sys
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/e2e-grabs")
    for k, v in build_all(dest).items():
        print(k, v)
