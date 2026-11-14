"""Ledger implementation. Call through app.ledger.api, not directly.

Definitions (docs/architecture.md):
  theoretical usage = sum(pos_sales.qty * recipes.qty)
  actual usage      = sum(qty of 'took' grabs) - sum(qty of 'put_back' grabs),
                      counting only status logged/confirmed (flagged waits for the owner)
  on_hand           = latest count + purchases since - actual usage since
Days are America/New_York local days.
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
                          ORDER BY ts DESC LIMIT 1) c ON true
     ORDER BY i.name"""
    with _conn() as conn:
        rows = conn.execute(sql, {"at": at, "day_start": day_start}).fetchall()
    out = []
    for r in rows:
        on_hand = float(r["base"]) + float(r["bought"]) - float(r["used_since"])
        out.append(InventoryRow(
            item=r["name"], unit=r["unit"], on_hand=_f(on_hand), par=_f(r["par"]),
            used_today=_f(r["used_today"]), unit_cost=_f(r["unit_cost"]),
            below_par=on_hand < float(r["par"]),
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
        updated = conn.execute(
            """UPDATE grab_events SET action = %s, item_id = %s, qty_label = %s, qty = %s, status = 'confirmed'
                WHERE grab_id = %s RETURNING grab_id""",
            (body.action, item["id"], body.qty_label, _qty(item, body.qty_label), grab_id)).fetchone()
        if updated is None:
            raise KeyError(f"no grab {grab_id!r}")
        r = conn.execute(_GRAB_SELECT + " WHERE g.grab_id = %s", (grab_id,)).fetchone()
    return _grab_row(r)


def draft_order(item: str, qty: float, chat_id: str | None = None, supplier: str | None = None) -> int:
    with _conn() as conn:
        it = _item(conn, item)
        return conn.execute(
            "INSERT INTO orders (item_id, qty, supplier, chat_id) VALUES (%s, %s, %s, %s) RETURNING id",
            (it["id"], qty, supplier, chat_id)).fetchone()["id"]


def set_order_status(order_id: int, status: str) -> None:
    if status not in ("draft", "approved", "rejected"):
        raise ValueError(f"bad status {status!r}")
    with _conn() as conn:
        o = conn.execute("UPDATE orders SET status = %s WHERE id = %s RETURNING item_id, qty, supplier",
                         (status, order_id)).fetchone()
        if o is None:
            raise KeyError(f"no order {order_id}")
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
