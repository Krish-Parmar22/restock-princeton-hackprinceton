"""Data endpoints for Arnav's dashboard + Excel. Contract: docs/arnav-shah.md.
All data comes from app.ledger.api (Postgres); shapes come from app.contracts."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

from app.contracts import GrabConfirm, GrabRow, InventoryRow, MessageRow, VarianceRow
from app.ledger import api as ledger
from app.ledger.impl import TZ

router = APIRouter()


def _today() -> date:
    return datetime.now(TZ).date()


@router.get("/api/inventory", response_model=list[InventoryRow])
def inventory():
    return ledger.get_inventory()


@router.get("/api/variance", response_model=list[VarianceRow])
def variance(start: date | None = Query(None, alias="from"), end: date | None = Query(None, alias="to")):
    end = end or _today()
    start = start or end - timedelta(days=6)
    if start > end:
        raise HTTPException(400, "'from' must be on or before 'to'")
    return ledger.get_variance(start, end)


@router.get("/api/grabs", response_model=list[GrabRow])
def grabs(day: date | None = Query(None, alias="date"), status: str = "all"):
    if status not in ("all", "logged", "flagged", "confirmed"):
        raise HTTPException(400, "status must be all|logged|flagged|confirmed")
    return ledger.list_grabs(day or _today(), status)


@router.post("/api/grabs/{grab_id}/confirm", response_model=GrabRow)
def confirm(grab_id: str, body: GrabConfirm):
    try:
        return ledger.confirm_grab(grab_id, body)
    except KeyError as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/api/orders")
def orders(limit: int = Query(30, ge=1, le=200)):
    return ledger.list_orders(limit)


@router.get("/api/reorder-plan")
def plan():
    p = ledger.reorder_plan()
    return {"items": p, "total_cost": round(sum(x["cost"] for x in p), 2)}


@router.post("/api/reorder-plan/send")
def send_plan(chat_id: str | None = None):
    """The agent texts the owner today's reorder plan and asks to approve it (demo button / daily job)."""
    from app.agent.runtime import send_reorder_plan
    return send_reorder_plan([chat_id] if chat_id else None)


@router.post("/api/briefing/send")
def send_briefing(chat_id: str | None = None):
    """The morning briefing now (demo button; the daily timer calls the same thing)."""
    from app.agent.runtime import send_morning_briefing
    return send_morning_briefing([chat_id] if chat_id else None)


@router.get("/api/messages", response_model=list[MessageRow])
def messages(limit: int = Query(50, ge=1, le=500)):
    return ledger.list_messages(limit)
