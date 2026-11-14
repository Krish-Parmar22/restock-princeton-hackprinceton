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


@router.get("/api/messages", response_model=list[MessageRow])
def messages(limit: int = Query(50, ge=1, le=500)):
    return ledger.list_messages(limit)
