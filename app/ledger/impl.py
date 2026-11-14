"""Ledger implementation. Call through app.ledger.api, not directly.

Definitions (docs/architecture.md):
  theoretical usage = sum(pos_sales.qty * recipes.qty)
  actual usage      = sum(qty of 'took' grabs) - sum(qty of 'put_back' grabs),
                      counting only status logged/confirmed (flagged waits for the owner)
  on_hand           = latest count + purchases since - actual usage since
                      (a count at time T includes everything at or before T; events strictly
                       after T are applied on top. Same-ts counts: highest id wins.)
Days are America/New_York local days; naive datetimes are treated as shop-local.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from app.contracts import GrabConfirm, GrabFrames, GrabRow, InventoryRow, MessageRow, VarianceRow
from app.db.conn import connect

TZ = ZoneInfo("America/New_York")
_URL: str | None = None  # override (tests); None -> settings.database_url

NET = ("COALESCE(SUM(CASE WHEN g.action = 'took' THEN g.qty "
       "WHEN g.action = 'put_back' THEN -g.qty ELSE 0 END), 0)")
COUNTED = "g.status IN ('logged', 'confirmed')"


def use_database(url: str | None) -> None:
    global _URL
    _URL = url


def _conn():
    return connect(_URL)


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, TZ)


def _f(x) -> float:
    return round(float(x or 0), 2)


# ---------- reads ----------

def get_inventory(at: datetime | None = None) -> list[InventoryRow]:
    at = at or datetime.now(TZ)
    if at.tzinfo is None:  # naive -> shop-local, never host-local
        at = at.replace(tzinfo=TZ)
    day_start = _day_start(at.astimezone(TZ).date())
    sql = f"""
    SELECT i.name, i.unit, i.par, i.unit_cost,
           COALESCE(c.qty, 0) AS base,
           (SELECT COALESCE(SUM(p.qty), 0) FROM purchases p
             WHERE p.item_id = i.id AND p.ts > COALESCE(c.ts, '-infinity') AND p.ts <= %(at)s) AS bought,
           (SELECT {NET} FROM grab_events g
             WHERE g.item_id = i.id AND {COUNTED}
               AND g.t_start > COALESCE(c.ts, '-infinity') AND g.t_start <= %(at)s) AS used_since,
           (SELECT {NET} FROM grab_events g
             WHERE g.item_id = i.id AND {COUNTED}
               AND g.t_start >= %(day_start)s AND g.t_start <= %(at)s) AS used_today
      FROM items i
      LEFT JOIN LATERAL (SELECT qty, ts FROM counts
                          WHERE item_id = i.id AND ts <= %(at)s
                          ORDER BY ts DESC, id DESC LIMIT 1) c ON true
     ORDER BY i.name"""
    with _conn() as conn:
        rows = conn.execute(sql, {"at": at, "day_start": day_start}).fetchall()
    out = []
    for r in rows:
        on_hand = r["base"] + r["bought"] - r["used_since"]  # Decimal math; float only for output
        out.append(InventoryRow(
            item=r["name"], unit=r["unit"], on_hand=_f(on_hand), par=_f(r["par"]),
            used_today=_f(r["used_today"]), unit_cost=_f(r["unit_cost"]),
            below_par=on_hand < r["par"],
        ))
    return out


def get_variance(start: date, end: date) -> list[VarianceRow]:
    lo, hi = _day_start(start), _day_start(end) + timedelta(days=1)
    sql = f"""
    SELECT i.name, i.unit, i.unit_cost,
           (SELECT COALESCE(SUM(s.qty * r.qty), 0) FROM pos_sales s
              JOIN recipes r ON r.menu_item = s.menu_item
             WHERE r.item_id = i.id AND s.ts >= %(lo)s AND s.ts < %(hi)s) AS theoretical,
           (SELECT {NET} FROM grab_events g
             WHERE g.item_id = i.id AND {COUNTED}
               AND g.t_start >= %(lo)s AND g.t_start < %(hi)s) AS actual,
           (SELECT COALESCE(array_agg(g.grab_id ORDER BY g.t_start), '{{}}') FROM grab_events g
             WHERE g.item_id = i.id AND {COUNTED} AND g.action = 'took'
               AND g.t_start >= %(lo)s AND g.t_start < %(hi)s) AS grab_ids
      FROM items i"""
    with _conn() as conn:
        rows = conn.execute(sql, {"lo": lo, "hi": hi}).fetchall()
    out = []
    for r in rows:
        theo, act = float(r["theoretical"]), float(r["actual"])
        vq = act - theo
        out.append(VarianceRow(
            item=r["name"], theoretical=_f(theo), actual=_f(act), unit=r["unit"],
            variance_qty=_f(vq), variance_usd=_f(vq * float(r["unit_cost"])),
            grab_ids=list(r["grab_ids"]),
        ))
    return sorted(out, key=lambda v: v.variance_usd, reverse=True)


def _grab_row(r: dict) -> GrabRow:
    frames = r["frames"] or {}
    return GrabRow(
        grab_id=r["grab_id"], zone=r["zone"], t_start=r["t_start"], t_end=r["t_end"],
        action=r["action"], item=r["item"], qty_label=r["qty_label"],
        confidence=float(r["confidence"]) if r["confidence"] is not None else None,
        status=r["status"],
        frames=GrabFrames(before=frames.get("before", ""), during=frames.get("during", []),
                          after=frames.get("after", "")),
    )


_GRAB_SELECT = """SELECT g.grab_id, g.zone, g.t_start, g.t_end, g.action, i.name AS item, g.qty_label,
                         g.confidence, g.status, g.frames
                    FROM grab_events g LEFT JOIN items i ON i.id = g.item_id"""


def list_grabs(day: date | None = None, status: str = "all") -> list[GrabRow]:
    where, params = [], {}
    if day is not None:
        where.append("g.t_start >= %(lo)s AND g.t_start < %(hi)s")
        params.update(lo=_day_start(day), hi=_day_start(day) + timedelta(days=1))
    if status != "all":
        where.append("g.status = %(status)s")
        params["status"] = status
    sql = _GRAB_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY g.t_start"
    with _conn() as conn:
        return [_grab_row(r) for r in conn.execute(sql, params).fetchall()]


def list_messages(limit: int = 50) -> list[MessageRow]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, chat_id, text, d1_score, decision FROM messages ORDER BY ts DESC, id DESC LIMIT %s",
            (limit,)).fetchall()
    return [MessageRow(ts=r["ts"], chat_id=r["chat_id"], text=r["text"],
                       d1_score=float(r["d1_score"]) if r["d1_score"] is not None else None,
                       decision=r["decision"]) for r in rows]


# ---------- writes ----------

def _item(conn, name: str) -> dict:
    r = conn.execute("SELECT id, qty_per_label, unit_cost FROM items WHERE name = %s", (name,)).fetchone()
    if r is None:
        raise KeyError(f"unknown item {name!r}")
    return r


def _qty(item: dict | None, label: str | None) -> float | None:
    if item is None or label is None:
        return None
    return item["qty_per_label"].get(label)


def record_grab(row: dict, d1_answers: list[dict]) -> None:
    with _conn() as conn:
        item = _item(conn, row["item"]) if row.get("item") else None
        if item and row.get("qty_label") and _qty(item, row["qty_label"]) is None:
            row = {**row, "status": "flagged"}  # unknown amount: owner must confirm, never silently uncounted
        inserted = conn.execute(
            """INSERT INTO grab_events (grab_id, zone, camera_id, source, t_start, t_end, item_id, action,
                                        qty_label, qty, confidence, status, frames)
               VALUES (%(grab_id)s, %(zone)s, %(camera_id)s, %(source)s, %(t_start)s, %(t_end)s, %(item_id)s,
                       %(action)s, %(qty_label)s, %(qty)s, %(confidence)s, %(status)s, %(frames)s)
               ON CONFLICT (grab_id) DO NOTHING RETURNING grab_id""",
            {**{k: row.get(k) for k in ("grab_id", "zone", "camera_id", "source", "t_start", "t_end",
                                         "action", "qty_label", "confidence", "status")},
             "item_id": item["id"] if item else None,
             "qty": _qty(item, row.get("qty_label")),
             "frames": Jsonb(row.get("frames") or {})},
        ).fetchone()
        if inserted:
            for a in d1_answers:
                conn.execute(
                    "INSERT INTO d1_answers (grab_id, question_id, answer, probs) VALUES (%s, %s, %s, %s)",
                    (row["grab_id"], a["question_id"], a["answer"], Jsonb(a.get("probs", {}))))


def confirm_grab(grab_id: str, body: GrabConfirm) -> GrabRow:
    with _conn() as conn:
        item = _item(conn, body.item)
        if _qty(item, body.qty_label) is None:
            raise ValueError(f"{body.item} has no amount for {body.qty_label!r}")
        updated = conn.execute(
            """UPDATE grab_events SET action = %s, item_id = %s, qty_label = %s, qty = %s, status = 'confirmed'
                WHERE grab_id = %s RETURNING grab_id""",
            (body.action, item["id"], body.qty_label, _qty(item, body.qty_label), grab_id)).fetchone()
        if updated is None:
            raise KeyError(f"no grab {grab_id!r}")
        r = conn.execute(_GRAB_SELECT + " WHERE g.grab_id = %s", (grab_id,)).fetchone()
    return _grab_row(r)


MAX_ORDER_QTY = 1000


def draft_order(item: str, qty: float, chat_id: str | None = None, supplier: str | None = None) -> int:
    import math
    if not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty <= 0 or qty > MAX_ORDER_QTY:
        raise ValueError(f"order qty must be between 0 and {MAX_ORDER_QTY}, got {qty!r}")
    with _conn() as conn:
        it = _item(conn, item)
        return conn.execute(
            "INSERT INTO orders (item_id, qty, supplier, chat_id) VALUES (%s, %s, %s, %s) RETURNING id",
            (it["id"], qty, supplier, chat_id)).fetchone()["id"]


def set_order_status(order_id: int, status: str) -> None:
    if status not in ("draft", "approved", "rejected"):
        raise ValueError(f"bad status {status!r}")
    with _conn() as conn:
        # Only a draft can change status, so approving twice (or concurrently) can't double-purchase.
        o = conn.execute(
            "UPDATE orders SET status = %s WHERE id = %s AND status = 'draft' RETURNING item_id, qty, supplier",
            (status, order_id)).fetchone()
        if o is None:
            cur = conn.execute("SELECT status FROM orders WHERE id = %s", (order_id,)).fetchone()
            if cur is None:
                raise KeyError(f"no order {order_id}")
            if cur["status"] == status:
                return  # already done; idempotent
            raise ValueError(f"order {order_id} is already {cur['status']}")
        if status == "approved":
            conn.execute(
                """INSERT INTO purchases (ts, item_id, qty, unit_cost, supplier, source)
                   SELECT now(), id, %s, unit_cost, %s, 'agent_order' FROM items WHERE id = %s""",
                (o["qty"], o["supplier"], o["item_id"]))


def record_message(chat_id: str | None, text: str, d1_score: float | None, decision: str, claims: list[dict]) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (chat_id, text, d1_score, decision, claims) VALUES (%s, %s, %s, %s, %s)",
            (chat_id, text, d1_score, decision, Jsonb(claims)))


def set_par(item: str, par: float) -> dict:
    import math
    if not isinstance(par, (int, float)) or not math.isfinite(par) or par < 0 or par > 100_000:
        raise ValueError(f"par must be a non-negative number, got {par!r}")
    with _conn() as conn:
        _item(conn, item)
        old = conn.execute("SELECT par FROM items WHERE name = %s", (item,)).fetchone()["par"]
        conn.execute("UPDATE items SET par = %s WHERE name = %s", (par, item))
    return {"item": item, "old_par": float(old), "new_par": float(par)}


def remember_chat(chat_id: str, sender: str | None) -> None:
    with _conn() as conn:
        conn.execute("""INSERT INTO chats (chat_id, sender) VALUES (%s, %s)
                        ON CONFLICT (chat_id) DO UPDATE SET last_seen = now(), sender = EXCLUDED.sender""",
                     (chat_id, sender))


def known_chats() -> list[str]:
    with _conn() as conn:
        return [r["chat_id"] for r in conn.execute("SELECT chat_id FROM chats ORDER BY last_seen DESC").fetchall()]


def recently_alerted(kind: str, item: str, hours: float, chat_id: str | None = None) -> bool:
    """Cooldown per item AND chat: one chat's successful alert must not silence another's retry."""
    with _conn() as conn:
        return conn.execute(
            """SELECT 1 FROM alerts a JOIN items i ON i.id = a.item_id
                WHERE a.kind = %s AND i.name = %s AND a.ts > now() - make_interval(secs => %s)
                  AND (%s::text IS NULL OR a.chat_id = %s) LIMIT 1""",
            (kind, item, hours * 3600, chat_id, chat_id)).fetchone() is not None


def record_alert(kind: str, item: str, chat_id: str, text: str) -> None:
    with _conn() as conn:
        it = _item(conn, item)
        conn.execute("INSERT INTO alerts (kind, item_id, chat_id, text) VALUES (%s, %s, %s, %s)",
                     (kind, it["id"], chat_id, text))


def record_count(item: str, qty: float, source: str = "owner_text") -> dict:
    """The owner counted an item ("we have 18 cokes"): a count resets on-hand to exactly this."""
    import math
    if not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty < 0 or qty > 100000:
        raise ValueError(f"count must be a number between 0 and 100000, got {qty!r}")
    with _conn() as conn:
        it = _item(conn, item)
        conn.execute("INSERT INTO counts (ts, item_id, qty, source) VALUES (now(), %s, %s, %s)",
                     (it["id"], qty, source))
    row = next(r for r in get_inventory() if r.item == item)
    return {"item": row.item, "on_hand": row.on_hand, "par": row.par, "unit": row.unit, "below_par": row.below_par}


def list_orders(limit: int = 20) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT o.id, o.ts, i.name AS item, i.unit, o.qty, i.unit_cost, o.qty * i.unit_cost AS cost,
                      coalesce(o.supplier, '') AS supplier, o.status
               FROM orders o JOIN items i ON i.id = o.item_id ORDER BY o.ts DESC LIMIT %s""", (limit,)).fetchall()
    return [{"id": r["id"], "ts": r["ts"].isoformat(), "item": r["item"], "unit": r["unit"], "qty": _f(r["qty"]),
             "unit_cost": _f(r["unit_cost"]), "cost": round(_f(r["cost"]), 2), "supplier": r["supplier"],
             "status": r["status"]} for r in rows]


def reorder_plan() -> list[dict]:
    """What to order now: every item below par, topped back up to 2x par (order-up-to level),
    whole units only. Pure arithmetic on the ledger, so the agent never invents a quantity."""
    import math
    out = []
    for r in get_inventory():
        if r.on_hand < r.par:
            qty = math.ceil(max(0.0, 2 * r.par - r.on_hand))
            out.append({"item": r.item, "unit": r.unit, "on_hand": r.on_hand, "par": r.par, "order_qty": qty,
                        "unit_cost": r.unit_cost, "cost": round(qty * r.unit_cost, 2)})
    return sorted(out, key=lambda x: -x["cost"])
