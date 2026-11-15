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
    """Current on-hand stock per item (from counts, received deliveries and camera usage), plus
    on_order (approved, not yet delivered) and unreviewed (flagged camera grabs today that the owner
    hasn't confirmed; on_hand may change once they do). Pass an item name, or nothing for all."""
    rows = [r.model_dump() for r in ledger.get_inventory()]
    if item:
        rows = [r for r in rows if r["item"] == _item_name(item)]
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
    from app.agent.runtime import allowed_image
    payload["images"] = [i for i in payload["images"] if allowed_image(i)]
    r = httpx.post(f"{settings.bridge_url}/send",
                   json=SendRequest(chat_id=ctx.chat_id, **payload).model_dump(), timeout=10)
    ok = r.status_code == 200 and r.json().get("ok")
    return "sent" if ok else f"error: bridge refused ({r.text[:200]})"


@tool
def place_order(item: str, qty: float, supplier: str | None = None) -> str:
    """Place a reorder with the supplier. Pauses for the owner's approval before it runs."""
    ctx = context.current()
    try:
        oid = ledger.draft_order(item, qty, ctx.chat_id if ctx else None, supplier)
    except (KeyError, ValueError) as e:
        return _j({"error": str(e)})
    ledger.set_order_status(oid, "approved")
    _changed(f"{qty:g} {item} on order")
    return _j({"order_id": oid, "item": item, "qty": qty, "status": "approved draft: on order, not yet received",
               "note": "No supplier integration: nothing was sent. Stock goes up when the delivery is recorded."})


@tool
def place_orders(orders: list[dict], supplier: str | None = None) -> str:
    """Place several reorders at once (e.g. the whole reorder_plan), as ONE approval for the owner.
    orders: [{"item": "coke", "qty": 9}, ...]. Pauses for the owner's yes before anything is ordered."""
    ctx = context.current()
    done, errors = [], []
    for o in orders:
        item, qty = _item_name(str(o.get("item", ""))), o.get("qty")
        try:
            oid = ledger.draft_order(item, float(qty), ctx.chat_id if ctx else None, supplier)
            ledger.set_order_status(oid, "approved")
            done.append({"order_id": oid, "item": item, "qty": float(qty)})
        except (KeyError, ValueError, TypeError) as e:
            errors.append({"item": item, "error": str(e)})
    if done:
        _changed("on order: " + ", ".join(f"{d['qty']:g} {d['item']}" for d in done))
    return _j({"approved_drafts": done, "errors": errors, "status": "on order, not yet received",
               "note": "No supplier integration: nothing was sent. Stock goes up when the delivery is recorded."})


@tool
def receive_delivery(item: str | None = None) -> str:
    """The delivery arrived ("the coke delivery came", "we got the order"): every approved order
    (or just this item's) is received and added to stock."""
    done = ledger.receive_orders(_item_name(item) if item else None)
    if not done:
        return _j({"received": [], "note": "Nothing was on order" + (f" for {item}" if item else "") + "."})
    _changed("received " + ", ".join(f"{d['qty']:g} {d['item']}" for d in done))
    return _j({"received": done})


@tool
def correct_grab(grab_id: str, action: str, item: str, qty_label: str) -> str:
    """Owner correction of one camera grab, e.g. "that was a put-back" or "that was cheese, not
    dough". action: took | put_back | touched. qty_label: pinch | handful | whole_container for
    bulk food, or "1" | "2" | "3" for cans/bottles (exact count).
    The ledger is updated immediately and the variance changes."""
    from app.contracts import GrabConfirm
    try:
        row = ledger.confirm_grab(grab_id, GrabConfirm(action=action, item=_item_name(item), qty_label=qty_label))
    except (KeyError, ValueError) as e:
        return _j({"error": str(e)})
    _changed(f"grab corrected: {action} {qty_label} {item}")
    return _j({"grab_id": row.grab_id, "action": row.action, "item": row.item,
               "qty_label": row.qty_label, "status": row.status})


@tool
def set_par(item: str, par: float) -> str:
    """Owner correction: change an ingredient's par level (the reorder line), e.g.
    "our pepperoni par is 25 lb now". Also note it in /memories/shop.md."""
    try:
        out = ledger.set_par(item.lower().strip(), par)
    except (KeyError, ValueError) as e:
        return _j({"error": str(e)})
    _changed(f"{item} par {par:g}")
    return _j(out)


def _changed(what: str) -> None:
    """Note a change the owner made; the runtime refreshes the Excel sheet and texts its picture."""
    ctx = context.current()
    if ctx is not None:
        ctx.sheet_changes.append(what)


def _item_name(item: str) -> str:
    return item.lower().strip().replace(" ", "_")


@tool
def record_count(item: str, qty: float) -> str:
    """The owner counted an item, e.g. "we have 18 cokes" or "counted 12 lb of cheese".
    Sets on-hand to exactly this number and updates the Excel sheet."""
    try:
        out = ledger.record_count(_item_name(item), qty)
    except (KeyError, ValueError) as e:
        return _j({"error": str(e)})
    _changed(f"{out['item']} count {qty:g}")
    return _j(out)


@tool
def reorder_plan() -> str:
    """What should be ordered right now: every item below par, with the quantity that brings it
    back to 2x par and the cost. Use these exact quantities with place_order."""
    plan = ledger.reorder_plan()
    return _j({"items": plan, "total_cost": round(sum(p["cost"] for p in plan), 2)})


@tool
def show_sheet() -> str:
    """Text the owner a picture of the current inventory sheet (also refreshes the Excel file)."""
    _changed("requested")
    return _j({"ok": True, "note": "The sheet picture is attached to your reply automatically."})


@tool
def usage_trend(day: str | None = None) -> str:
    """How a day (YYYY-MM-DD, default yesterday) compares with a normal day, per item: used vs typical
    (same weekday in past weeks, or the previous 7 days), % change, days of stock left at the recent
    rate, and flagged grabs waiting on the owner. Every number is computed in the ledger."""
    rows = ledger.usage_trend(date.fromisoformat(day) if day else None)
    return _j([r for r in rows if r["used"] or r["typical"] or r["flagged_grabs"]] or rows)


@tool
def demand_forecast(days: int = 3) -> str:
    """Expected usage per item for the next `days` days (weekday-aware where history exists) and the
    order that covers it plus par as a safety buffer. Use for "what should I order for the weekend?"."""
    rows = ledger.demand_forecast(max(1, min(int(days), 14)))
    return _j({"days": days, "items": rows, "total_cost": round(sum(r["cost"] for r in rows), 2)})


@tool
def flagged_grabs(day: str | None = None) -> str:
    """Camera grabs the system wasn't sure about (status flagged) on a day (default today), with what
    d1 thought. Use with d1_look to re-check each clip and suggest a correction to the owner."""
    from datetime import datetime as _dt
    from app.ledger.impl import TZ
    d = date.fromisoformat(day) if day else _dt.now(TZ).date()
    rows = ledger.list_grabs(d, "flagged")
    by_item: dict[str, int] = {}
    for g in rows:
        by_item[g.item or g.zone] = by_item.get(g.item or g.zone, 0) + 1
    # counts are returned, never left for the model to count (the d1 guard only accepts tool numbers)
    return _j({"day": d.isoformat(), "total_flagged": len(rows), "flagged_by_item": by_item,
               "grabs": [{"grab_id": g.grab_id, "zone": g.zone, "action": g.action, "item": g.item,
                          "qty_label": g.qty_label, "confidence": g.confidence} for g in rows[:12]]})


@tool
def briefing_facts(day: str | None = None) -> str:
    """The numbers for the owner's morning text, computed in code: yesterday's biggest change vs a
    normal day (with its unit), how many grabs need the owner's OK, and the reorder total. Text the
    owner ONLY these numbers; the subagents' reports are for detail."""
    from datetime import datetime as _dt, timedelta as _td
    from app.ledger.impl import TZ
    today = _dt.now(TZ).date()
    y = date.fromisoformat(day) if day else today - _td(days=1)
    trend = [r for r in ledger.usage_trend(y) if r["typical"] > 0 and r["change_pct"] is not None]
    top = max(trend, key=lambda r: abs(r["change_pct"]), default=None)
    flagged = len(ledger.list_grabs(y, "flagged")) + (len(ledger.list_grabs(today, "flagged")) if y != today else 0)
    # Order only what is below par right now; the forecast is a heads-up, not an order to approve.
    items = sorted(({"item": p["item"], "qty": p["order_qty"], "unit": p["unit"], "cost": p["cost"]}
                    for p in ledger.reorder_plan()), key=lambda o: -o["cost"])
    soon = sorted((r for r in ledger.usage_trend(y) if r["days_of_stock"] is not None and r["days_of_stock"] < 3
                   and r["on_hand"] >= r["par"]), key=lambda r: r["days_of_stock"])[:2]
    total = round(sum(o["cost"] for o in items), 2)
    lines = ["Good morning! Here's your debrief:"]
    if top:
        word = "up" if top["change_pct"] >= 0 else "down"
        lines.append(f"Yesterday: {top['item'].replace('_', ' ')} {word} {abs(top['change_pct'])}% "
                     f"({top['used']:g} {top['unit']} vs a typical {round(top['typical'], 1):g}).")
    else:
        lines.append("Yesterday looked like a normal day.")
    lines.append(f"{flagged} camera grab{'s' if flagged != 1 else ''} need your OK." if flagged else "No grabs need your OK.")
    lines.append(f"Planned order: " + ", ".join(f"{o['qty']:g} {o['item'].replace('_', ' ')}" for o in items)
                 + f" (${total:,.2f})." if items else "Nothing needs ordering today.")
    if soon:
        lines.append("Heads up: " + ", ".join(f"{r['item'].replace('_', ' ')} runs out in about {r['days_of_stock']:g} days"
                                              for r in soon) + " at this pace.")
    return _j({"day": y.isoformat(), "biggest_change": top, "grabs_need_ok": flagged, "running_low_soon": soon,
               "order": items, "order_total": total, "suggested_text": "\n".join(lines)})


ALL_TOOLS = [get_stock, get_variance, list_grabs, d1_look, send_text, place_order, place_orders, receive_delivery,
             correct_grab, set_par, record_count, reorder_plan, show_sheet,
             usage_trend, demand_forecast, flagged_grabs, briefing_facts]
