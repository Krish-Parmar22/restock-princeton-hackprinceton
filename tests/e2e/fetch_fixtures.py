"""Download the two CC-licensed source images for the E2E suite (see fixtures.md).

Usage: uv run python -m tests.e2e.fetch_fixtures     (idempotent; ~250 KB total)
"""
from __future__ import annotations

from pathlib import Path

import httpx

CACHE = Path(__file__).parent / ".cache"
SOURCES = {
    "scene_toppings.jpg": "https://live.staticflickr.com/3505/3463281604_a5e185e771_b.jpg",
    "hand_overhead.jpg": "https://upload.wikimedia.org/wikipedia/commons/2/2c/Arthritis_on_the_Right_Hand.jpg",
}


def fetch() -> Path:
    CACHE.mkdir(exist_ok=True)
    for name, url in SOURCES.items():
        dest = CACHE / name
        if dest.exists() and dest.stat().st_size > 10_000:
            continue
        r = httpx.get(url, follow_redirects=True, timeout=30, headers={"User-Agent": "princeton-hacks-e2e/0.1"})
        r.raise_for_status()
        dest.write_bytes(r.content)
    return CACHE


if __name__ == "__main__":
    print(fetch())
