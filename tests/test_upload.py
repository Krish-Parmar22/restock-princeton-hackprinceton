"""Upload API tests (the Pi -> laptop handoff, docs/handoff.md)."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.upload as upload_mod
from app.api.server import app
from scripts import make_fake_grabs


@pytest.fixture()
def env(tmp_path, monkeypatch):
    src = make_fake_grabs.make(tmp_path / "src", "2026-11-14")[0]
    dest = tmp_path / "grabs"
    from app.config import settings
    monkeypatch.setattr(upload_mod, "settings", replace(settings, grabs_dir=dest))
    monkeypatch.setattr(upload_mod, "_known_items", lambda: {"pepperoni", "cheese", "dough", "sauce"})
    return TestClient(app), src, dest


def post(client, folder: Path, meta: dict | None = None, drop: str | None = None, extra: dict | None = None):
    meta_text = json.dumps(meta) if meta is not None else (folder / "meta.json").read_text()
    files = [(p.name, (p.name, p.read_bytes(), "image/jpeg")) for p in sorted(folder.glob("*.jpg")) if p.name != drop]
    for name, data in (extra or {}).items():
        files.append((name, (name, data, "image/jpeg")))
    return client.post("/api/grabs/upload", data={"meta": meta_text}, files=files)


def test_upload_stores_complete_package_atomically(env):
    client, src, dest = env
    r = post(client, src)
    assert r.status_code == 200 and r.json()["stored"] is True
    gid = json.loads((src / "meta.json").read_text())["grab_id"]
    stored = dest / gid
    assert sorted(p.name for p in stored.iterdir()) == sorted(p.name for p in src.iterdir())
    assert not [p for p in dest.iterdir() if p.name.startswith(".tmp-")]   # no temp folders left
    from app.contracts import GrabPackage
    assert GrabPackage.load(stored).meta.grab_id == gid                     # watcher can read it


def test_duplicate_is_409(env):
    client, src, _ = env
    assert post(client, src).status_code == 200
    assert post(client, src).status_code == 409


@pytest.mark.parametrize("bad", ["missing_after", "bad_zone", "bad_id", "junk_image", "stray_file", "bad_meta"])
def test_invalid_packages_are_400(env, bad):
    client, src, dest = env
    meta = json.loads((src / "meta.json").read_text())
    kw = {}
    if bad == "missing_after":
        kw["drop"] = "after.jpg"
    elif bad == "bad_zone":
        kw["meta"] = {**meta, "zone": "anchovies"}
    elif bad == "bad_id":
        kw["meta"] = {**meta, "grab_id": "../../etc/passwd"}
    elif bad == "junk_image":
        kw["extra"] = {"during_05.jpg": b"not an image"}
    elif bad == "stray_file":
        kw["extra"] = {"evil.sh": b"rm -rf /"}
    elif bad == "bad_meta":
        kw["meta"] = {"zone": "pepperoni"}
    r = post(client, src, **kw)
    assert r.status_code == 400, r.text
    assert not dest.exists() or not any(p for p in dest.iterdir() if not p.name.startswith("."))
