"""The owner's live Excel sheet, plus a picture of it for iMessage.

refresh() rewrites SHEET_PATH (exports/Inventory.xlsx by default) from the ledger: Inventory,
Orders and Camera log tabs, with live formulas. It runs after anything the owner changes by text
(a count, a par level, an approved order) and returns a PNG of the Inventory tab, which the
agent texts back (Photon 2.0 iMessage carries text and images, not file attachments).
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.config import ROOT, settings
from app.ledger import api as ledger
from app.ledger.impl import TZ

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PNG_DIR = Path(tempfile.gettempdir()) / "inventory-sheet"


def _font(size: int, bold: bool = False):
    for path in (["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf"] if bold else
                 ["/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"]) + [
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans" + ("-Bold" if bold else "") + ".ttf"]:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _fmt(x: float) -> str:
    return f"{x:g}" if abs(x - round(x)) > 1e-9 else str(int(round(x)))


def refresh(changed: str | None = None) -> dict:
    """Rewrite the Excel file and render the PNG. Returns {"xlsx": path, "png": path}."""
    from dashboard.excel import workbook_bytes  # the same builder the dashboard's download uses

    now = datetime.now(TZ)
    inv = [r.model_dump() for r in ledger.get_inventory()]
    orders = ledger.list_orders(50)
    grabs = [g.model_dump(mode="json") for g in ledger.list_grabs(now.date(), "all")]
    note = f"Updated {now.strftime('%-I:%M %p')} by text" + (f": {changed}" if changed else "")
    data = workbook_bytes(inv, now.date(), grabs, orders, note)
    path = settings.sheet_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.xlsx")
    tmp.write_bytes(data)
    os.replace(tmp, path)        # atomic: Excel never opens a half-written file
    return {"xlsx": str(path), "png": render_png(inv, orders, note, changed)}


def render_png(inv: list[dict], orders: list[dict], note: str, changed: str | None = None) -> str:
    rows = sorted(inv, key=lambda r: (not r["below_par"], r["item"]))
    cols = [("Item", 220), ("On hand", 120), ("On order", 110), ("Par", 70), ("Order", 90), ("Status", 140)]
    pad, rh, head = 28, 46, 120
    recent = [o for o in orders if o["status"] in ("approved", "received")][:3]
    w = pad * 2 + sum(c[1] for c in cols)
    h = head + rh * (len(rows) + 1) + (40 + 34 * len(recent) if recent else 0) + pad
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    f_title, f_b, f_r, f_s = _font(30, True), _font(20, True), _font(20), _font(16)
    d.text((pad, 24), "Daily Inventory", font=f_title, fill="#111827")
    d.text((pad, 70), note, font=f_s, fill="#6B7280")
    y = head
    d.rectangle([pad, y, w - pad, y + rh], fill="#1F3A5F")
    x = pad
    for name, cw in cols:
        d.text((x + 14, y + 12), name, font=f_b, fill="white")
        x += cw
    y += rh
    for r in rows:
        oo = r.get("on_order", 0) or 0
        low = r["on_hand"] + oo < r["par"]          # same rule as reorder_plan + the Excel formula
        hit = changed and r["item"] in changed
        fill = "#FDE2E1" if low else ("#E7F5EC" if hit else "white")
        d.rectangle([pad, y, w - pad, y + rh], fill=fill, outline="#D1D5DB")
        order = math.ceil(2 * r["par"] - r["on_hand"] - oo) if low else 0
        status = "REORDER" if low else ("ON ORDER" if r["on_hand"] < r["par"] else "OK")
        vals = [r["item"].replace("_", " ").title(), f"{_fmt(r['on_hand'])} {r['unit']}", _fmt(oo) if oo else "",
                _fmt(r["par"]), _fmt(order) if order else "", status]
        x = pad
        for (_, cw), v in zip(cols, vals):
            color = "#9B1C1C" if (low and v == "REORDER") else "#111827"
            d.text((x + 14, y + 12), v, font=f_b if (low and v == "REORDER") else f_r, fill=color)
            x += cw
        y += rh
    if recent:
        y += 18
        d.text((pad, y), "Recent reorders (approved drafts; stock rises on delivery)", font=f_b, fill="#111827")
        y += 30
        for o in recent:
            t = datetime.fromisoformat(o["ts"]).astimezone(TZ).strftime("%a %-I:%M %p")
            st = "on order" if o["status"] == "approved" else "received"
            d.text((pad, y), f"{t}   {_fmt(o['qty'])} {o['unit']} {o['item'].replace('_', ' ')}   ${o['cost']:.2f}   {st}",
                   font=f_r, fill="#374151")
            y += 34
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    out = PNG_DIR / f"sheet-{datetime.now().strftime('%H%M%S%f')}.png"
    img.save(out)
    return str(out)
