"""The shop's business checking account, as a statement built from the ledger.

Money in:  daily card deposits = register sales x menu prices, less card fees.
Money out: supplier payments (deliveries + agent orders, from `purchases`), plus the fixed
           bills a small pizza shop has (rent, payroll, utilities, internet).
Demo data: the account number, opening balance and fixed bills are made up. Swap in a real feed
(e.g. Capital One's Nessie API) behind statement() without touching the agent.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from app.ledger.impl import TZ, _conn

ACCOUNT = "Business checking ending 4821"
OPENING_DATE = date(2026, 11, 1)
OPENING_BALANCE = 18420.55
CARD_FEE = 0.029
MENU_PRICES = {"slice": 3.50, "medium cheese": 14.00, "medium pepperoni": 16.00,
               "large cheese": 18.00, "large pepperoni": 20.00}
FIXED = [  # (day of month or weekday rule, description, amount)
    (date(2026, 11, 1), "Rent: 214 Nassau St LLC", -4200.00),
    (date(2026, 11, 5), "PSE&G electric + gas", -612.40),
    (date(2026, 11, 10), "Comcast Business internet", -129.99),
]
PAYROLL = ("Payroll: 4 staff", -3150.00)  # every Friday


def _transactions(until: date) -> list[dict]:
    with _conn() as c:
        sales = c.execute(
            """SELECT (ts AT TIME ZONE %s)::date AS d, menu_item, sum(qty) AS n
               FROM pos_sales WHERE ts >= %s GROUP BY 1, 2""", (str(TZ), OPENING_DATE)).fetchall()
        buys = c.execute(
            """SELECT (p.ts AT TIME ZONE %s)::date AS d, coalesce(p.supplier, 'Orders placed by text') AS supplier,
                      sum(p.qty * p.unit_cost) AS amt, bool_or(p.source = 'agent_order') AS agent
               FROM purchases p WHERE p.ts >= %s GROUP BY 1, 2""", (str(TZ), OPENING_DATE)).fetchall()
    tx: list[dict] = []
    gross: dict[date, float] = defaultdict(float)
    for r in sales:
        gross[r["d"]] += float(r["n"]) * MENU_PRICES.get(r["menu_item"], 0.0)
    for d, amt in gross.items():
        if amt > 0:
            tx.append({"date": d, "desc": "Card deposit: register sales", "amount": round(amt * (1 - CARD_FEE), 2)})
    for r in buys:
        desc = r['supplier'] if r['supplier'] == 'Orders placed by text' else f"{r['supplier']}: {'order placed by text' if r['agent'] else 'delivery invoice'}"
        tx.append({"date": r["d"], "desc": desc, "amount": -round(float(r["amt"]), 2)})
    for d, desc, amt in FIXED:
        tx.append({"date": d, "desc": desc, "amount": amt})
    d = OPENING_DATE
    while d <= until:
        if d.weekday() == 4:
            tx.append({"date": d, "desc": PAYROLL[0], "amount": PAYROLL[1]})
        d += timedelta(days=1)
    tx = [t for t in tx if OPENING_DATE <= t["date"] <= until]
    tx.sort(key=lambda t: (t["date"], t["amount"] < 0, t["desc"]))
    bal = OPENING_BALANCE
    for t in tx:
        bal = round(bal + t["amount"], 2)
        t["balance"] = bal
    return tx


def _by_payee(period: list[dict]) -> dict[str, float]:
    """Totals per payee, so "how much did we pay Shore Foods?" is a number the guard can verify."""
    out: dict[str, float] = defaultdict(float)
    for t in period:
        if t["amount"] < 0:
            out[t["desc"].split(":")[0]] += -t["amount"]
    return {k: round(v, 2) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def statement(days: int = 7, today: date | None = None) -> dict:
    today = today or datetime.now(TZ).date()
    days = max(1, min(int(days), 31))
    tx = _transactions(today)
    start = today - timedelta(days=days - 1)
    period = [t for t in tx if t["date"] >= start]
    money_in = round(sum(t["amount"] for t in period if t["amount"] > 0), 2)
    money_out = round(-sum(t["amount"] for t in period if t["amount"] < 0), 2)
    return {
        "account": ACCOUNT,
        "balance": tx[-1]["balance"] if tx else OPENING_BALANCE,
        "period": f"{start.isoformat()} to {today.isoformat()}",
        "money_in": money_in,
        "money_out": money_out,
        "net": round(money_in - money_out, 2),
        "paid_by_payee": _by_payee(period),
        "largest_payments": sorted(({"date": t["date"].isoformat(), "desc": t["desc"], "amount": t["amount"]}
                                    for t in period if t["amount"] < 0), key=lambda t: t["amount"])[:3],
        "transactions": [{"date": t["date"].isoformat(), "desc": t["desc"], "amount": t["amount"],
                          "balance": t["balance"]} for t in period[-12:]],
    }
