"""Proactive alerts: the agent texts the owner first when something needs attention.

After the watcher records new grabs, `check_and_alert()` looks for items that dropped below
par and texts every known owner chat (Photon only allows chats that texted the line first)
through the bridge's POST /send. Each item alerts at most once per ALERT_COOLDOWN_H hours.

The text is built from ledger numbers (no LLM), so it's instant, can't hallucinate, and
needs no d1 check. The owner can reply "reorder pepperoni" and the agent takes it from there.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.contracts import SendRequest
from app.ledger import api as ledger

log = logging.getLogger("alerts")
ALERT_COOLDOWN_H = 12


def _fmt(x: float) -> str:
    return f"{x:g}"


def below_par_text(row) -> str:
    gap = max(0.0, row.par - row.on_hand)
    return (f"Heads up: {row.item} is at {_fmt(row.on_hand)} {row.unit}, below your par of {_fmt(row.par)} "
            f"({_fmt(round(gap, 2))} {row.unit} short). Want me to reorder? Reply \"reorder {row.item}\".")


def check_and_alert(send=None) -> list[dict]:
    """Text owners about newly below-par items. Returns what was sent (for logs/tests)."""
    send = send or _send_via_bridge
    chats = ledger.known_chats()
    if not chats:
        return []
    sent = []
    for row in ledger.get_inventory():
        if not row.below_par or ledger.recently_alerted("below_par", row.item, ALERT_COOLDOWN_H):
            continue
        text = below_par_text(row)
        for chat in chats:
            ok, err = send(SendRequest(chat_id=chat, text=text))
            ledger.record_message(chat, text, None, "sent" if ok else "blocked",
                                  [] if ok else [{"claim": "bridge refused", "error": err}])
            if ok:
                ledger.record_alert("below_par", row.item, chat, text)
                sent.append({"chat_id": chat, "item": row.item, "text": text})
            else:
                log.warning("alert to %s not delivered: %s", chat, err)
    return sent


def _send_via_bridge(req: SendRequest) -> tuple[bool, str | None]:
    try:
        r = httpx.post(f"{settings.bridge_url}/send", json=req.model_dump(), timeout=10)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return (r.status_code == 200 and bool(body.get("ok"))), body.get("error") or (None if r.status_code == 200 else r.text[:200])
    except httpx.HTTPError as e:
        return False, f"bridge unreachable: {e}"
