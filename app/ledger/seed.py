"""Seed a believable demo week (Sun 2026-11-08 .. Sat 2026-11-14, America/New_York).

Planted problem for the agent to find: pepperoni camera usage runs ~18% over what the
register says should have been used, on Tuesday and Thursday evenings (5-10pm).
Everything else tracks sales within a few percent.
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from psycopg.types.json import Jsonb

from app.db.conn import connect
from app.ledger.impl import TZ

WEEK_START = date(2026, 11, 8)
ITEMS = [  # name, unit, unit_cost, par, qty_per_label
    ("pepperoni", "lb", 4.50, 10, {"pinch": 0.05, "handful": 0.25, "whole_container": 5}),
    ("cheese", "lb", 3.80, 15, {"pinch": 0.05, "handful": 0.3, "whole_container": 5}),
    ("dough", "each", 0.60, 30, {"pinch": 1, "handful": 1, "whole_container": 20}),
    ("sauce", "qt", 2.20, 4, {"pinch": 0.02, "handful": 0.1, "whole_container": 2}),
]
DRINK_QPL = {"1": 1, "2": 2, "3": 3, "pinch": 1, "handful": 2, "whole_container": 6}
# Casa Piada's real soft-drinks list and levels (their inventory sheet, Nov 14): par = their
# "Replacement", opening count = their "On Hand". Prices are our estimates (their sheet leaves them blank).
DRINKS = [  # (name, unit, unit_cost, par = Replacement, qty_per_label)
    ("coke", "each", 1.00, 20, DRINK_QPL),
    ("sprite", "each", 1.00, 10, DRINK_QPL),
    ("diet_coke", "each", 1.00, 28, DRINK_QPL),
    ("water", "each", 0.50, 24, DRINK_QPL),
    ("siciliana_orange", "each", 1.50, 14, DRINK_QPL),
    ("siciliana_limonata", "each", 1.50, 14, DRINK_QPL),
    ("fresca", "each", 1.00, 14, DRINK_QPL),
    ("sparkling_water", "each", 0.75, 20, DRINK_QPL),
    ("ice_tea_peach", "each", 1.25, 12, DRINK_QPL),
    ("ice_tea_lemon", "each", 1.25, 12, DRINK_QPL),
]
DRINK_ON_HAND = {"coke": 31, "sprite": 5, "diet_coke": 28, "water": 44, "siciliana_orange": 35,
                 "siciliana_limonata": 35, "fresca": 16, "sparkling_water": 28, "ice_tea_peach": 15,
                 "ice_tea_lemon": 31}
DRINK_START_COUNT = 24   # fallback for a drink missing from DRINK_ON_HAND
RECIPES = {  # menu item -> {ingredient: qty per unit sold}
    "large cheese": {"dough": 1, "sauce": 0.25, "cheese": 0.5},
    "large pepperoni": {"dough": 1, "sauce": 0.25, "cheese": 0.45, "pepperoni": 0.25},
    "medium cheese": {"dough": 1, "sauce": 0.18, "cheese": 0.35},
    "medium pepperoni": {"dough": 1, "sauce": 0.18, "cheese": 0.3, "pepperoni": 0.18},
    "slice": {"dough": 0.125, "sauce": 0.03, "cheese": 0.06},
}
MIX = {"large pepperoni": 0.35, "large cheese": 0.25, "medium pepperoni": 0.15, "medium cheese": 0.10, "slice": 0.15}
HOUR_WEIGHT = {11: 6, 12: 10, 13: 8, 14: 4, 15: 3, 16: 4, 17: 8, 18: 11, 19: 10, 20: 7, 21: 4}
DAY_MULT = [0.9, 0.7, 0.75, 0.8, 0.85, 1.2, 1.3]  # Sun..Sat
PIZZAS_PER_DAY = 110
START_COUNT = {"pepperoni": 15, "cheese": 40, "dough": 80, "sauce": 20}
DELIVERIES = {  # day offset from Sunday -> {item: qty}, 9am
    1: {"pepperoni": 55, "cheese": 150, "dough": 400, "sauce": 90},   # Mon
    4: {"pepperoni": 20, "cheese": 150, "dough": 400, "sauce": 90},   # Thu (short on pepperoni)
}
PLANTED = {"item": "pepperoni", "weekdays": {1, 3}, "hours": range(17, 22), "over": 0.18}  # Tue=1, Thu=3 (Mon=0)

TABLES = "alerts, chats, messages, orders, d1_answers, grab_events, counts, purchases, pos_sales, recipes, items"


def _local(d: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(d, time(h, m), TZ)


def seed(url: str | None = None, reset: bool = False, rng_seed: int = 7) -> dict:
    rnd = random.Random(rng_seed)
    with connect(url) as conn:
        if reset:
            conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        if conn.execute("SELECT 1 FROM pos_sales LIMIT 1").fetchone():
            return {"skipped": "already seeded (use reset=True)"}

        ids = {}
        for name, unit, cost, par, qpl in ITEMS:
            ids[name] = conn.execute(
                """INSERT INTO items (name, unit, unit_cost, par, qty_per_label) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (name) DO UPDATE SET unit = EXCLUDED.unit RETURNING id""",
                (name, unit, cost, par, Jsonb(qpl))).fetchone()["id"]
        drink_ids = {}
        for name, unit, cost, par, dq in DRINKS:  # kept out of `ids` so the pizza week's RNG stream is unchanged
            drink_ids[name] = conn.execute(
                """INSERT INTO items (name, unit, unit_cost, par, qty_per_label) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (name) DO UPDATE SET unit = EXCLUDED.unit, unit_cost = EXCLUDED.unit_cost,
                       par = EXCLUDED.par, qty_per_label = EXCLUDED.qty_per_label RETURNING id""",
                (name, unit, cost, par, Jsonb(dq))).fetchone()["id"]
        qpl = {name: q for name, _, _, _, q in ITEMS}
        for menu, parts in RECIPES.items():
            for ing, q in parts.items():
                conn.execute("INSERT INTO recipes VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (menu, ids[ing], q))

        first = _local(WEEK_START, 10)
        for name, q in START_COUNT.items():
            conn.execute("INSERT INTO counts (ts, item_id, qty, source) VALUES (%s, %s, %s, 'manual')",
                         (first, ids[name], q))
        for name, did in drink_ids.items():
            conn.execute("INSERT INTO counts (ts, item_id, qty, source) VALUES (%s, %s, %s, 'manual')",
                         (first, did, DRINK_ON_HAND.get(name, DRINK_START_COUNT)))
        for off, delivery in DELIVERIES.items():
            d = WEEK_START + timedelta(days=off)
            for name, q in delivery.items():
                cost = next(c for n, _, c, _, _ in ITEMS if n == name)
                conn.execute("""INSERT INTO purchases (ts, item_id, qty, unit_cost, supplier, source)
                                VALUES (%s, %s, %s, %s, 'Shore Foods', 'synthetic')""",
                             (_local(d, 9), ids[name], q, cost))

        sales, grabs, n_grab = [], [], 0
        carry = {k: 0.0 for k in ids}  # fractional handfuls carried hour to hour, so totals don't drift
        total_w = sum(HOUR_WEIGHT.values())
        for di in range(7):
            d = WEEK_START + timedelta(days=di)
            weekday = d.weekday()  # Mon=0 .. Sun=6
            mult = DAY_MULT[(weekday + 1) % 7]
            for h, w in HOUR_WEIGHT.items():
                n_pies = PIZZAS_PER_DAY * mult * w / total_w
                usage = {k: 0.0 for k in ids}
                for menu, share in MIX.items():
                    qty = round(n_pies * share * (3 if menu == "slice" else 1) * rnd.uniform(0.8, 1.2))
                    if qty <= 0:
                        continue
                    sales.append((_local(d, h, rnd.randint(0, 59)), menu, qty))
                    for ing, q in RECIPES[menu].items():
                        usage[ing] += qty * q
                for ing, u in usage.items():
                    factor = rnd.gauss(1.0, 0.01)
                    if ing == PLANTED["item"] and weekday in PLANTED["weekdays"] and h in PLANTED["hours"]:
                        factor += PLANTED["over"]
                    handful = qpl[ing]["handful"]
                    want = u * factor / handful + carry[ing]
                    n = max(0, int(want))
                    carry[ing] = want - n
                    for _ in range(n):
                        n_grab += 1
                        t0 = _local(d, h, rnd.randint(0, 59)) + timedelta(seconds=rnd.randint(0, 59))
                        grabs.append((f"syn-{n_grab:05d}_{ing}", ing, t0, t0 + timedelta(seconds=3),
                                      ids[ing], handful))

        with conn.cursor() as cur:
            cur.executemany("INSERT INTO pos_sales (ts, menu_item, qty, source) VALUES (%s, %s, %s, 'synthetic')", sales)
            cur.executemany(
                """INSERT INTO grab_events (grab_id, zone, camera_id, source, t_start, t_end, item_id, action,
                                            qty_label, qty, confidence, status, frames)
                   VALUES (%s, %s, 'cam0', 'synthetic', %s, %s, %s, 'took', 'handful', %s, 0.95, 'logged',
                           '{"before": "", "during": [], "after": ""}')""",
                grabs)
    return {"sales_rows": len(sales), "grabs": len(grabs)}
