"""Data endpoints for Arnav's dashboard + Excel. STUB: fake data until the ledger is wired.
The ledger workstream replaces the bodies with calls to app.ledger.api (same shapes)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter

from app.config import settings
from app.contracts import GrabConfirm, GrabFrames, GrabRow, InventoryRow, MessageRow, VarianceRow

router = APIRouter()


def _fake_grabs() -> list[GrabRow]:
    from app.contracts import GrabPackage

    rows = []
    for folder in sorted(settings.grabs_dir.glob("*_*")):
        try:
            p = GrabPackage.load(folder)
        except ValueError:
            continue
        rows.append(GrabRow(
            grab_id=p.meta.grab_id, zone=p.meta.zone, t_start=p.meta.t_start, t_end=p.meta.t_end,
            action="took", item=p.meta.zone, qty_label="handful", confidence=0.88,
            status="logged", frames=GrabFrames(**p.frames_json()),
        ))
    return rows


@router.get("/api/inventory", response_model=list[InventoryRow])
def inventory():
    return [
        InventoryRow(item="pepperoni", unit="lb", on_hand=6.2, par=10, used_today=3.1, unit_cost=4.50, below_par=True),
        InventoryRow(item="cheese", unit="lb", on_hand=18.0, par=15, used_today=7.4, unit_cost=3.80, below_par=False),
        InventoryRow(item="dough", unit="each", on_hand=40, par=30, used_today=22, unit_cost=0.60, below_par=False),
        InventoryRow(item="sauce", unit="qt", on_hand=5.0, par=4, used_today=2.0, unit_cost=2.20, below_par=False),
    ]


@router.get("/api/variance", response_model=list[VarianceRow])
def variance(start: date | None = None, end: date | None = None):
    ids = [g.grab_id for g in _fake_grabs() if g.zone == "pepperoni"]
    return [
        VarianceRow(item="pepperoni", theoretical=10.0, actual=12.0, unit="lb", variance_qty=2.0, variance_usd=9.00, grab_ids=ids),
        VarianceRow(item="cheese", theoretical=20.0, actual=20.6, unit="lb", variance_qty=0.6, variance_usd=2.28, grab_ids=[]),
    ]


@router.get("/api/grabs", response_model=list[GrabRow])
def grabs(day: date | None = None, status: str = "all"):
    rows = _fake_grabs()
    return rows if status == "all" else [g for g in rows if g.status == status]


@router.post("/api/grabs/{grab_id}/confirm", response_model=GrabRow)
def confirm(grab_id: str, body: GrabConfirm):
    for g in _fake_grabs():
        if g.grab_id == grab_id:
            return g.model_copy(update={"action": body.action, "item": body.item, "qty_label": body.qty_label, "status": "confirmed"})
    from fastapi import HTTPException
    raise HTTPException(404, f"no grab {grab_id}")


@router.get("/api/messages", response_model=list[MessageRow])
def messages(limit: int = 50):
    now = datetime.now(timezone.utc)
    return [
        MessageRow(ts=now, chat_id="demo", text="Pepperoni ran 18% over on Tuesday close. Here are 6 clips.", d1_score=0.93, decision="sent"),
        MessageRow(ts=now, chat_id="demo", text="Someone is stealing pepperoni.", d1_score=0.21, decision="blocked"),
    ][:limit]
