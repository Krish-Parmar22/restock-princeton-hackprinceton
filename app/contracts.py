"""Shared data contracts. Single source in code for:

1. The grab package (hardware -> software). Spec: docs/architecture.md, "The handoff".
2. The API at :8000 that the Photon bridge and dashboard call. Spec: app/contracts.py.

Change these only after telling the other side.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Action = Literal["took", "put_back", "touched"]
QtyLabel = Literal["pinch", "handful", "whole_container", "1", "2", "3", "more"]  # numbers: discrete items (cans)
GrabStatus = Literal["logged", "flagged", "confirmed"]


# ---------- 1. Grab package ----------

class GrabMeta(BaseModel):
    grab_id: str
    camera_id: str = "cam0"
    zone: str
    t_start: datetime
    t_end: datetime
    frame_times: list[datetime] = Field(default_factory=list)
    source: Literal["webcam", "iphone", "fake"] = "webcam"
    # The bin's box on the camera frame: [[x, y], ...] in pixels. Optional; used to crop/highlight.
    zone_polygon: list[list[float]] | None = None

    @field_validator("t_start", "t_end")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps need a timezone offset, e.g. 2026-11-14T12:14:03-05:00")
        return v

    @model_validator(mode="after")
    def _ordered(self):
        if self.t_end < self.t_start:
            raise ValueError("t_end is before t_start")
        if self.zone_polygon is not None and (len(self.zone_polygon) < 3 or any(len(p) != 2 for p in self.zone_polygon)):
            raise ValueError("zone_polygon needs at least 3 [x, y] points")
        return self


class GrabPackage(BaseModel):
    meta: GrabMeta
    before: Path
    during: list[Path]
    after: Path

    @classmethod
    def load(cls, folder: Path) -> "GrabPackage":
        """Read a grab package folder. Raises ValueError if it is incomplete."""
        folder = Path(folder)
        meta_path = folder / "meta.json"
        before, after = folder / "before.jpg", folder / "after.jpg"
        during = sorted(folder.glob("during_*.jpg"))
        missing = [p.name for p in (meta_path, before, after) if not p.exists()]
        if missing or not during:
            raise ValueError(f"incomplete grab package {folder}: missing {missing or ['during_*.jpg']}")
        meta = GrabMeta.model_validate(json.loads(meta_path.read_text()))
        return cls(meta=meta, before=before.resolve(), during=[p.resolve() for p in during], after=after.resolve())

    def frames_json(self) -> dict:
        return {"before": str(self.before), "during": [str(p) for p in self.during], "after": str(self.after)}


# ---------- 2. API :8000 ----------

class InboundMessage(BaseModel):
    chat_id: str
    sender: str
    text: str
    ts: datetime
    images: list[str] = Field(default_factory=list)


class InboundReply(BaseModel):
    reply: str | None
    images: list[str] = Field(default_factory=list)


class InventoryRow(BaseModel):
    item: str
    unit: str
    on_hand: float
    par: float
    used_today: float
    unit_cost: float
    below_par: bool
    on_order: float = 0.0          # approved reorders not yet received (NOT on the shelf)
    unreviewed: int = 0            # flagged camera grabs today that could still change on_hand


class VarianceRow(BaseModel):
    item: str
    theoretical: float
    actual: float
    unit: str
    variance_qty: float
    variance_usd: float
    grab_ids: list[str]


class GrabFrames(BaseModel):
    before: str
    during: list[str]
    after: str


class GrabRow(BaseModel):
    grab_id: str
    zone: str
    t_start: datetime
    t_end: datetime
    action: Action | None
    item: str | None
    qty_label: QtyLabel | None
    confidence: float | None
    status: GrabStatus
    frames: GrabFrames


class GrabConfirm(BaseModel):
    action: Action
    item: str
    qty_label: QtyLabel


class MessageRow(BaseModel):
    ts: datetime
    chat_id: str | None
    text: str
    d1_score: float | None
    decision: Literal["sent", "softened", "blocked", "received"]   # received = the owner's own text


class SendRequest(BaseModel):
    """What we POST to the Photon bridge at BRIDGE_URL/send."""
    chat_id: str
    text: str
    images: list[str] = Field(default_factory=list)
