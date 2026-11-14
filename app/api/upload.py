"""POST /api/grabs/upload: how the Pi hands a grab package to the laptop. Spec: docs/handoff.md.

multipart/form-data: field `meta` (meta.json text) + one file per image named by its filename
(before.jpg, during_01.jpg..during_06.jpg, after.jpg). The package is validated, written to a
hidden temp folder, then atomically renamed into GRABS_DIR/<grab_id>/, so the watcher never sees
half a package.
  200 {"grab_id", "stored": true} | 409 duplicate (Pi treats as success) | 400 invalid (don't retry)
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import uuid

from fastapi import APIRouter, HTTPException, Request
from PIL import Image
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.config import settings
from app.contracts import GrabMeta

router = APIRouter()

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
FRAME = re.compile(r"^(before|after|during_0[1-6])\.(jpg|jpeg)$")  # JPEG only (what the watcher reads)
MAX_FILE_BYTES = 5_000_000
MAX_REQUEST_BYTES = 8 * MAX_FILE_BYTES


def _known_items() -> set[str] | None:
    try:
        from app.ledger.impl import _conn
        with _conn() as conn:
            return {r["name"] for r in conn.execute("SELECT name FROM items").fetchall()}
    except Exception:
        return None  # DB down: accept the zone; the watcher fails closed later if items are missing


@router.post("/api/grabs/upload")
async def upload(request: Request):
    if settings.upload_token and request.headers.get("x-upload-token") != settings.upload_token:
        raise HTTPException(401, "missing or wrong X-Upload-Token")
    if int(request.headers.get("content-length") or 0) > MAX_REQUEST_BYTES:
        raise HTTPException(413, f"package over {MAX_REQUEST_BYTES} bytes")
    form = await request.form(max_files=8, max_fields=4)
    raw = form.get("meta")
    if not isinstance(raw, str):
        raise HTTPException(400, "missing 'meta' field (meta.json text)")
    try:
        meta = GrabMeta.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        raise HTTPException(400, f"bad meta: {str(e)[:300]}") from None
    if not SAFE_ID.match(meta.grab_id):
        raise HTTPException(400, "grab_id may only contain letters, digits, _ . : -")
    items = _known_items()
    if items is not None and meta.zone not in items:
        raise HTTPException(400, f"zone {meta.zone!r} is not an item ({sorted(items)})")

    files: dict[str, bytes] = {}
    for key, value in form.multi_items():
        if key == "meta":
            continue
        if not isinstance(value, UploadFile):
            raise HTTPException(400, f"field {key!r} should be a file")
        name = (key or value.filename or "").lower()
        if not FRAME.match(name):
            raise HTTPException(400, f"unexpected file {name!r}: want before.jpg, during_01..06.jpg, after.jpg (JPEG only)")
        data = await value.read()
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(400, f"{name} is over {MAX_FILE_BYTES} bytes")
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
        except Exception:
            raise HTTPException(400, f"{name} is not a readable image") from None
        files[name.replace(".jpeg", ".jpg")] = data
    stems = {n.rsplit(".", 1)[0] for n in files}
    if "before" not in stems or "after" not in stems or not any(s.startswith("during_") for s in stems):
        raise HTTPException(400, "need before, at least one during_NN, and after")

    root = settings.grabs_dir
    root.mkdir(parents=True, exist_ok=True)
    final = root / meta.grab_id
    if final.exists():
        raise HTTPException(409, f"already have {meta.grab_id}")
    tmp = root / f".tmp-{meta.grab_id}-{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    try:
        for name, data in files.items():
            (tmp / name).write_bytes(data)
        (tmp / "meta.json").write_text(raw)
        os.rename(tmp, final)  # atomic on the same filesystem
    except FileExistsError:
        raise HTTPException(409, f"already have {meta.grab_id}") from None
    except OSError as e:
        if final.exists():
            raise HTTPException(409, f"already have {meta.grab_id}") from None
        raise HTTPException(500, f"could not store package: {e}") from None
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    return {"grab_id": meta.grab_id, "stored": True}
