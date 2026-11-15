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


def _offer(row) -> dict | None:
    return next((p for p in ledger.reorder_plan() if p["item"] == row.item), None)


def below_par_text(row, offer: dict | None = None) -> str:
    """One text that asks the question, so a single "yes" places the reorder (see runtime offers)."""
    name = row.item.replace("_", " ")
    unit = "" if row.unit == "each" else f" {row.unit}"
    head = (f"We're out of {name}." if row.on_hand <= 0 else
            f"Heads up: we're down to {_fmt(row.on_hand)}{unit} {name} (par {_fmt(row.par)}).")
    if offer:
        return head + f" Reorder {_fmt(offer['order_qty'])}{unit} for ${offer['cost']:,.2f}? Reply yes or no."
    return head + " Want me to reorder?"


def check_and_alert(send=None) -> list[dict]:
    """Text owners about newly below-par items. Returns what was sent (for logs/tests)."""
    send = send or _send_via_bridge
    chats = ledger.known_chats()
    if not chats:
        return []
    sent = []
    for row in ledger.get_inventory():
        if not row.below_par:
            continue
        offer = _offer(row)
        text = below_par_text(row, offer)
        photo = None
        for chat in chats:
            if ledger.recently_alerted("below_par", row.item, ALERT_COOLDOWN_H, chat_id=chat):
                continue
            if photo is None:
                photo = grab_photo(row.item) or ""
            ok, err = send(SendRequest(chat_id=chat, text=text, images=[photo] if photo else []))
            ledger.record_message(chat, text, None, "sent" if ok else "blocked",
                                  [] if ok else [{"claim": "bridge refused", "error": err}])
            if ok:
                ledger.record_alert("below_par", row.item, chat, text)
                if offer:   # the owner's next plain "yes" places exactly this order (runtime._answer_offer)
                    ledger.record_alert("reorder_offer", row.item, chat, f"{offer['order_qty']:g}")
                _tell_agent(chat, text)
                sent.append({"chat_id": chat, "item": row.item, "text": text})
            else:
                log.warning("alert to %s not delivered: %s", chat, err)
    return sent


def _tell_agent(chat: str, text: str) -> None:
    """Put the alert in the agent's own conversation, so a follow-up like "make it 30" or "why?" makes sense."""
    try:
        from app.agent.runtime import _note, get_agent
        agent = get_agent()
        if agent is not None:
            _note(agent, chat, f"[system: you just texted the owner this alert: {text!r}. A reply about a quantity "
                               f"or yes/no refers to it.]")
    except Exception:
        log.exception("could not add the alert to the agent thread")


def grab_photo(item: str) -> str | None:
    """BEFORE | AFTER of the latest grab that took this item, as one picture for iMessage."""
    try:
        from datetime import datetime
        from pathlib import Path

        import cv2
        import numpy as np

        from app import sheet
        from app.ledger.impl import TZ
        grabs = [g for g in ledger.list_grabs(datetime.now(TZ).date(), "all")
                 if g.item == item and g.action == "took" and g.status in ("logged", "confirmed")]
        if not grabs:
            return None
        g = max(grabs, key=lambda x: str(x.t_start))
        ims = []
        for label, path in (("BEFORE", g.frames.before), ("AFTER", g.frames.after)):
            im = cv2.imread(str(path)) if path and Path(path).exists() else None
            if im is None:
                return None
            im = cv2.resize(im, (640, int(640 * im.shape[0] / im.shape[1])))
            cv2.rectangle(im, (0, 0), (150, 42), (0, 0, 0), -1)
            cv2.putText(im, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
            ims.append(im)
        sheet.PNG_DIR.mkdir(parents=True, exist_ok=True)
        out = sheet.PNG_DIR / f"grab-{g.grab_id}.jpg"
        cv2.imwrite(str(out), np.hstack(ims), [cv2.IMWRITE_JPEG_QUALITY, 85])
        return str(out)
    except Exception:
        log.exception("grab photo failed")
        return None


def _send_via_bridge(req: SendRequest) -> tuple[bool, str | None]:
    try:
        r = httpx.post(f"{settings.bridge_url}/send", json=req.model_dump(), timeout=10)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return (r.status_code == 200 and bool(body.get("ok"))), body.get("error") or (None if r.status_code == 200 else r.text[:200])
    except httpx.HTTPError as e:
        return False, f"bridge unreachable: {e}"
