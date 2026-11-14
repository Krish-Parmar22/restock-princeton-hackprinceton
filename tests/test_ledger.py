"""Ledger tests against a throwaway database (pizza_test_ledger). Never touches the dev DB."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.contracts import GrabConfirm
from app.db.conn import apply_schema, connect
from app.ledger import api, impl
from app.ledger.impl import TZ

ADMIN = "postgresql://localhost:5432/postgres"
URL = "postgresql://localhost:5432/pizza_test_ledger"
D = date(2026, 11, 10)


def _t(h: int, m: int = 0, d: date = D) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


@pytest.fixture()
def db():
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS pizza_test_ledger WITH (FORCE)")
        c.execute("CREATE DATABASE pizza_test_ledger")
    apply_schema(URL)
    impl.use_database(URL)
    with connect(URL) as conn:
        conn.execute("""INSERT INTO items (name, unit, unit_cost, par, qty_per_label) VALUES
            ('pepperoni', 'lb', 4.00, 10, %s), ('cheese', 'lb', 2.00, 5, %s)""",
                     (Jsonb({"handful": 0.25, "pinch": 0.05, "whole_container": 5}),
                      Jsonb({"handful": 0.5, "pinch": 0.05, "whole_container": 5})))
        conn.execute("INSERT INTO recipes VALUES ('pep pie', 1, 0.5), ('pep pie', 2, 1.0)")
        conn.execute("INSERT INTO pos_sales (ts, menu_item, qty) VALUES (%s, 'pep pie', 4)", (_t(18),))  # theo: pep 2.0, cheese 4.0
        conn.execute("INSERT INTO counts (ts, item_id, qty) VALUES (%s, 1, 12), (%s, 2, 20)", (_t(9), _t(9)))
    yield URL
    impl.use_database(None)


def grab(gid, item, action="took", label="handful", status="logged", h=18):
    return {"grab_id": gid, "zone": item or "pepperoni", "camera_id": "c", "source": "fake",
            "t_start": _t(h), "t_end": _t(h) + timedelta(seconds=3), "item": item, "action": action,
            "qty_label": label, "confidence": 0.9, "status": status,
            "frames": {"before": "/b.jpg", "during": ["/d1.jpg"], "after": "/a.jpg"}}


def test_variance_math_exact(db):
    for i in range(10):                      # 10 handfuls x 0.25 = 2.5 lb took
        api.record_grab(grab(f"g{i}", "pepperoni"), [])
    api.record_grab(grab("pb", "pepperoni", action="put_back"), [])  # -0.25
    v = {r.item: r for r in api.get_variance(D, D)}
    assert v["pepperoni"].theoretical == 2.0
    assert v["pepperoni"].actual == 2.25
    assert v["pepperoni"].variance_qty == 0.25
    assert v["pepperoni"].variance_usd == 1.0
    assert len(v["pepperoni"].grab_ids) == 10   # only 'took' grabs
    assert v["cheese"].actual == 0 and v["cheese"].variance_qty == -4.0


def test_flagged_excluded_until_confirmed(db):
    api.record_grab(grab("f1", "pepperoni", status="flagged"), [])
    assert {r.item: r for r in api.get_variance(D, D)}["pepperoni"].actual == 0
    row = api.confirm_grab("f1", GrabConfirm(action="took", item="pepperoni", qty_label="whole_container"))
    assert row.status == "confirmed"
    assert {r.item: r for r in api.get_variance(D, D)}["pepperoni"].actual == 5.0


def test_confirm_missing_raises(db):
    with pytest.raises(KeyError):
        api.confirm_grab("nope", GrabConfirm(action="took", item="pepperoni", qty_label="pinch"))


def test_record_grab_idempotent_and_answers_once(db):
    ans = [{"question_id": "item", "answer": "pepperoni", "probs": {"pepperoni": 0.9}}]
    api.record_grab(grab("x", "pepperoni"), ans)
    api.record_grab(grab("x", "pepperoni"), ans)
    with connect(URL) as conn:
        assert conn.execute("SELECT count(*) n FROM grab_events").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) n FROM d1_answers").fetchone()["n"] == 1


def test_grab_with_no_item(db):
    api.record_grab(grab("n", None, status="flagged"), [])
    g = api.list_grabs(D, "flagged")
    assert len(g) == 1 and g[0].item is None


def test_inventory_on_hand(db):
    for i in range(8):                       # 8 x 0.25 = 2 lb pepperoni used after the count
        api.record_grab(grab(f"g{i}", "pepperoni"), [])
    inv = {r.item: r for r in api.get_inventory(_t(23))}
    assert inv["pepperoni"].on_hand == 10.0      # 12 - 2
    assert inv["pepperoni"].used_today == 2.0
    assert inv["pepperoni"].below_par is False   # 10 is not < 10
    oid = api.draft_order("pepperoni", 5)
    api.set_order_status(oid, "approved")        # approval records a purchase
    inv = {r.item: r for r in api.get_inventory(datetime.now(TZ) + timedelta(minutes=1))}
    assert inv["pepperoni"].on_hand == 15.0


def test_order_reject_no_purchase(db):
    oid = api.draft_order("cheese", 3)
    api.set_order_status(oid, "rejected")
    with connect(URL) as conn:
        assert conn.execute("SELECT count(*) n FROM purchases").fetchone()["n"] == 0


def test_messages_newest_first(db):
    api.record_message("c", "first", 0.9, "sent", [])
    api.record_message("c", "second", 0.2, "blocked", [{"claim": "x", "score": 0.2}])
    m = api.list_messages(10)
    assert [x.text for x in m] == ["second", "first"] and m[0].decision == "blocked"


def test_seeded_week_shows_planted_pepperoni(db):
    from app.ledger.seed import seed
    seed(URL, reset=True)
    days = {d: {r.item: r for r in api.get_variance(date(2026, 11, d), date(2026, 11, d))} for d in range(8, 15)}
    pep = {d: days[d]["pepperoni"].variance_qty for d in days}
    assert pep[10] > 0.5 and pep[12] > 0.5                       # Tue, Thu
    assert all(abs(pep[d]) < 0.3 for d in (8, 9, 11, 13, 14))     # other days near zero
    week = api.get_variance(date(2026, 11, 8), date(2026, 11, 14))
    assert week[0].item == "pepperoni"                            # biggest $ gap


def test_api_shapes(db):
    from fastapi.testclient import TestClient
    from app.api.server import app
    api.record_grab(grab("g1", "pepperoni"), [])
    c = TestClient(app)
    assert c.get("/api/inventory").status_code == 200
    v = c.get("/api/variance?from=2026-11-10&to=2026-11-10").json()
    assert {"item", "theoretical", "actual", "unit", "variance_qty", "variance_usd", "grab_ids"} <= set(v[0])
    g = c.get("/api/grabs?date=2026-11-10&status=all").json()
    assert g[0]["frames"]["before"] == "/b.jpg"
    assert c.get("/api/variance?from=2026-11-12&to=2026-11-10").status_code == 400
    assert c.post("/api/grabs/nope/confirm", json={"action": "took", "item": "pepperoni", "qty_label": "pinch"}).status_code == 404
    ok = c.post("/api/grabs/g1/confirm", json={"action": "put_back", "item": "pepperoni", "qty_label": "pinch"})
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed"
    assert c.get("/api/messages?limit=5").status_code == 200
