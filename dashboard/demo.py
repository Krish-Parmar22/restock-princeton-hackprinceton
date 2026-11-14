"""Demo data in the exact API shapes, for working offline (no API, no Postgres).
Frames are drawn with Pillow into dashboard/.demo/ (gitignored). The dashboard labels it as demo data."""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

DEMO_DIR = Path(__file__).parent / ".demo" / "grabs"
COLORS = {"pepperoni": (176, 40, 40), "cheese": (240, 214, 120), "dough": (230, 220, 200), "sauce": (200, 60, 30)}

INVENTORY = [
    {"item": "pepperoni", "unit": "lb", "on_hand": 4.25, "par": 10, "used_today": 3.1, "unit_cost": 4.50, "below_par": True},
    {"item": "cheese", "unit": "lb", "on_hand": 14.0, "par": 20, "used_today": 9.5, "unit_cost": 3.80, "below_par": True},
    {"item": "dough", "unit": "ball", "on_hand": 42, "par": 30, "used_today": 38, "unit_cost": 0.65, "below_par": False},
    {"item": "sauce", "unit": "qt", "on_hand": 11, "par": 8, "used_today": 4, "unit_cost": 2.10, "below_par": False},
]

# (time, zone, action, qty_label, confidence, status)
GRABS = [
    ("12:14:03", "pepperoni", "took", "handful", 0.88, "logged"),
    ("12:31:40", "cheese", "took", "handful", 0.91, "logged"),
    ("13:02:11", "pepperoni", "took", "handful", 0.54, "flagged"),
    ("17:48:22", "dough", "took", "whole_container", 0.79, "logged"),
    ("18:05:09", "sauce", "touched", "pinch", 0.47, "flagged"),
    ("18:40:55", "pepperoni", "took", "handful", 0.86, "logged"),
    ("19:12:30", "cheese", "put_back", "handful", 0.62, "flagged"),
]


def _frame(path: Path, zone: str, n: int, hand: bool) -> None:
    img = Image.new("RGB", (480, 360), (70, 72, 78))
    d = ImageDraw.Draw(img)
    d.rectangle([110, 80, 370, 300], fill=(200, 200, 205), outline=(40, 40, 40), width=4)
    d.text((118, 60), zone, fill=(255, 255, 255))
    for i in range(n):
        x, y = 135 + (i % 6) * 38, 105 + (i // 6) * 38
        d.ellipse([x, y, x + 28, y + 28], fill=COLORS[zone])
    if hand:
        d.ellipse([230, 140, 330, 230], fill=(224, 172, 140))
        d.rectangle([265, 220, 300, 360], fill=(224, 172, 140))
    img.save(path, quality=85)


def _grab_rows(day: date) -> list[dict]:
    rows = []
    for i, (t, zone, action, qty, conf, status) in enumerate(GRABS):
        start = datetime.fromisoformat(f"{day}T{t}")
        gid = f"{day}T{t.replace(':', '-')}_{zone}"
        folder = DEMO_DIR / gid
        if not (folder / "after.jpg").exists():
            folder.mkdir(parents=True, exist_ok=True)
            n = 12 + i % 5
            delta = {"took": -4, "put_back": 3, "touched": 0}[action]
            _frame(folder / "before.jpg", zone, n, False)
            _frame(folder / "during_01.jpg", zone, n, True)
            _frame(folder / "during_02.jpg", zone, n + delta, True)
            _frame(folder / "after.jpg", zone, n + delta, False)
        rows.append({
            "grab_id": gid, "zone": zone, "t_start": start.isoformat(),
            "t_end": (start + timedelta(seconds=4)).isoformat(),
            "action": action, "item": zone, "qty_label": qty, "confidence": conf, "status": status,
            "frames": {"before": str((folder / "before.jpg").resolve()),
                       "during": [str((folder / f"during_0{k}.jpg").resolve()) for k in (1, 2)],
                       "after": str((folder / "after.jpg").resolve())},
        })
    return rows


class DemoApi:
    demo = True

    def __init__(self):
        self._confirmed: dict[str, dict] = {}

    def healthy(self) -> bool:
        return True

    def inventory(self) -> list[dict]:
        return copy.deepcopy(INVENTORY)

    def grabs(self, day: date, status: str = "all") -> list[dict]:
        rows = []
        for g in _grab_rows(day):
            if g["grab_id"] in self._confirmed:
                g.update(self._confirmed[g["grab_id"]], status="confirmed")
            if status == "all" or g["status"] == status:
                rows.append(g)
        return rows

    def variance(self, start: date, end: date) -> list[dict]:
        days = [start + timedelta(d) for d in range((end - start).days + 1)]
        ids = {z: [g["grab_id"] for d in days for g in self.grabs(d) if g["zone"] == z] for z in COLORS}
        n = len(days)
        base = [("pepperoni", 10.0, 12.0, "lb", 4.50), ("cheese", 40.0, 41.2, "lb", 3.80),
                ("dough", 250.0, 251.0, "ball", 0.65), ("sauce", 20.0, 19.5, "qt", 2.10)]
        rows = []
        for item, theo, act, unit, cost in base:
            t, a = round(theo * n / 7, 2), round(act * n / 7, 2)
            rows.append({"item": item, "theoretical": t, "actual": a, "unit": unit,
                         "variance_qty": round(a - t, 2), "variance_usd": round((a - t) * cost, 2),
                         "grab_ids": ids[item]})
        return rows

    def messages(self, limit: int = 50) -> list[dict]:
        now = datetime.now().replace(microsecond=0)
        rows = [
            (2, "Pepperoni is at 4.25 lb, below your par of 10.", 0.95, "sent"),
            (9, "Looks like: pepperoni ran over on Tuesday evening.", 0.71, "softened"),
            (15, "I couldn't confirm that from the records yet. Want me to pull the clips and check?", 0.32, "blocked"),
            (40, "Approve this order: 20 lb pepperoni? Reply yes or no.", 0.97, "sent"),
            (95, "Cheese dropped below par (14 lb).", 0.93, "sent"),
        ]
        return [{"ts": (now - timedelta(minutes=m)).isoformat(), "chat_id": "demo-owner", "text": t,
                 "d1_score": s, "decision": dec} for m, t, s, dec in rows][:limit]

    def orders(self, limit: int = 30) -> list[dict]:
        return []

    def reorder_plan(self) -> dict:
        import math
        items = [{"item": r["item"], "unit": r["unit"], "on_hand": r["on_hand"], "par": r["par"],
                  "order_qty": math.ceil(2 * r["par"] - r["on_hand"]), "unit_cost": r["unit_cost"],
                  "cost": round(math.ceil(2 * r["par"] - r["on_hand"]) * r["unit_cost"], 2)}
                 for r in self.inventory() if r["on_hand"] < r["par"]]
        return {"items": items, "total_cost": round(sum(i["cost"] for i in items), 2)}

    def send_plan(self) -> list[dict]:
        return []

    def send_briefing(self) -> dict:
        return {"started": False}

    def trace(self, chat_id: str | None = None) -> dict:
        return {"chat_id": None, "steps": []}

    def confirm(self, grab_id: str, action: str, item: str, qty_label: str) -> dict:
        self._confirmed[grab_id] = {"action": action, "item": item, "qty_label": qty_label, "confidence": 1.0}
        day = date.fromisoformat(grab_id[:10])
        return next(g for g in self.grabs(day) if g["grab_id"] == grab_id)
