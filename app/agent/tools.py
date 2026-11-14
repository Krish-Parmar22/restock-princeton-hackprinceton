"""Agent tools. Every number the agent says must come from one of these (ledger = source of truth)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
from langchain_core.tools import tool

from app.agent import context
from app.config import settings
from app.contracts import SendRequest
from app.d1.base import D1Question, get_client
from app.ledger import api as ledger


def _j(x) -> str:
    return json.dumps(x, default=str)


@tool
def get_stock(item: str | None = None) -> str:
    """Current on-hand stock per ingredient (from counts, deliveries and camera usage).
    Pass an item name (e.g. 'pepperoni') to get one item, or nothing for all."""
    rows = [r.model_dump() for r in ledger.get_inventory()]
    if item:
        rows = [r for r in rows if r["item"] == item.lower().strip()]
        if not rows:
            return _j({"error": f"unknown item {item!r}"})
    return _j(rows)


@tool
def get_variance(start: str, end: str) -> str:
    """Should-have-used (register sales x recipes) vs actually-used (camera) per ingredient,
    between two dates inclusive (YYYY-MM-DD). Positive variance = more used than sold."""
    rows = ledger.get_variance(date.fromisoformat(start), date.fromisoformat(end))
    out = []
    for r in rows:
        d = r.model_dump()
        d["grab_count"] = len(d.pop("grab_ids"))
        d["example_grab_ids"] = r.grab_ids[:5]
        out.append(d)
    return _j(out)


@tool
def list_grabs(day: str, status: str = "all") -> str:
    """Camera grab events on a day (YYYY-MM-DD). status: all | logged | flagged | confirmed.
    Returns counts per item/action plus up to 10 grab ids."""
    rows = ledger.list_grabs(date.fromisoformat(day), status)
    summary: dict[str, int] = {}
    for g in rows:
        key = f"{g.item or 'unknown'}:{g.action or 'unknown'}"
        summary[key] = summary.get(key, 0) + 1
    return _j({"total": len(rows), "by_item_action": summary,
               "grab_ids": [g.grab_id for g in rows[:10]],
               "flagged": [g.grab_id for g in rows if g.status == "flagged"][:10]})


@tool
def d1_look(grab_id: str, question: str, options: list[str]) -> str:
    """Ask the d1 vision checker one multiple-choice question about a grab's frames
    (before, during, after). Returns the probability of each option."""
    match = next((g for g in ledger.list_grabs(None, "all") if g.grab_id == grab_id), None)
    if match is None:
        return _j({"error": f"no grab {grab_id!r}"})
    frames = [match.frames.before, *match.frames.during, match.frames.after]
    frames = [Path(p) for p in frames if p and Path(p).exists()]
    if not frames:
        return _j({"error": "no frames on disk for this grab"})
    q = D1Question(id="look", type="choice", instructions=question, options=tuple(options))
    state = ("Images are in time order: first = BEFORE the hand entered the bin, "
             "middle = DURING the grab, last = AFTER the person stepped back.")
    ans = get_client().ask([q], state, frames)["look"]
    return _j({"answer": ans.answer, "probs": ans.probs})


@tool
def send_text(message: str, images: list[str] | None = None) -> str:
    """Text the shop owner. This is the ONLY way to talk to the owner. Keep it short.
    Every number must come from a tool result. Checked by the d1 guard before sending."""
    ctx = context.current()
    payload = {"text": message, "images": images or []}
    if ctx is None or ctx.inbound:
        if ctx is not None:
            ctx.outbox.append(payload)
        return "sent"
    if not ctx.chat_id:
        return "error: no chat to send to"
    r = httpx.post(f"{settings.bridge_url}/send",
                   json=SendRequest(chat_id=ctx.chat_id, **payload).model_dump(), timeout=10)
    ok = r.status_code == 200 and r.json().get("ok")
    return "sent" if ok else f"error: bridge refused ({r.text[:200]})"


@tool
def place_order(item: str, qty: float, supplier: str | None = None) -> str:
    """Place a reorder with the supplier. Pauses for the owner's approval before it runs."""
    ctx = context.current()
    oid = ledger.draft_order(item, qty, ctx.chat_id if ctx else None, supplier)
    ledger.set_order_status(oid, "approved")
    return _j({"order_id": oid, "item": item, "qty": qty, "status": "approved"})


ALL_TOOLS = [get_stock, get_variance, list_grabs, d1_look, send_text, place_order]
