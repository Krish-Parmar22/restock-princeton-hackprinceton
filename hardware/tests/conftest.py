"""Shared fixtures for hardware tests."""

import sys
import os
import numpy as np
import pytest

# Add hardware/ to path so we can import modules directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def blank_frame():
    """A 720p black frame."""
    return np.zeros((720, 1280, 3), dtype=np.uint8)


@pytest.fixture
def white_frame():
    """A 720p white frame."""
    return np.ones((720, 1280, 3), dtype=np.uint8) * 255


@pytest.fixture
def sample_zones():
    """Two rectangular zones in a 1280x720 frame."""
    return [
        {"name": "cheese", "points": [[100, 100], [400, 100], [400, 400], [100, 400]]},
        {"name": "pepperoni", "points": [[600, 100], [900, 100], [900, 400], [600, 400]]},
    ]


@pytest.fixture
def tmp_grabs(tmp_path):
    """Temp directory for grab output."""
    d = tmp_path / "grabs"
    d.mkdir()
    return str(d)


@pytest.fixture
def tmp_recordings(tmp_path):
    """Temp directory for recording output."""
    d = tmp_path / "recordings"
    d.mkdir()
    return str(d)


def make_frame_with_motion(base_frame, zone_points, intensity=200):
    """Draw a bright rectangle inside a zone to simulate motion."""
    frame = base_frame.copy()
    xs = [p[0] for p in zone_points]
    ys = [p[1] for p in zone_points]
    x1, x2 = min(xs) + 10, max(xs) - 10
    y1, y2 = min(ys) + 10, max(ys) - 10
    frame[y1:y2, x1:x2] = intensity
    return frame
