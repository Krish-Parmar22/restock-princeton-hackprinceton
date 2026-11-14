"""Proactive below-par alerts and owner corrections (set_par, correct_grab) on a real test DB."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.agent import alerts
from app.agent.tools import correct_grab, set_par
from app.db.conn import apply_schema, connect
from app.ledger import api, impl
from app.ledger.impl import TZ

URL = "postgresql://localhost:5432/pizza_test_alerts"


@pytest.fixture()
def db():
    with psycopg.connect("postgresql://localhost:5432/postgres", autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS pizza_test_alerts WITH (FORCE)")
        c.execute("CREATE DATABASE pizza_test_alerts")
    apply_schema(URL)
    impl.use_database(URL)
    with connect(URL) as conn:
        conn.execute("INSERT INTO items (name, unit, unit_cost, par, qty_per_label) VALUES ('pepperoni','lb',4.5,10,%s)",
                     (Jsonb({"handful": 0.25, "pinch": 0.05, "whole_container": 5}),))
        conn.execute("INSERT INTO counts (ts, item_id, qty) VALUES (%s, 1, 8)", (datetime.now(TZ) - timedelta(hours=1),))
    yield
    impl.use_database(None)


def test_below_par_alerts_known_chats_once(db):
    sent = []
    send = lambda req: (sent.append(req) or True, None)
    assert alerts.check_and_alert(send) == []                    # nobody has texted the line yet
    api.remember_chat("owner-1", "+1555")
    out = alerts.check_and_alert(send)
    assert len(out) == 1 and out[0]["item"] == "pepperoni"
    assert "8 lb" in sent[0].text and "par of 10" in sent[0].text and 'reorder pepperoni' in sent[0].text
    assert alerts.check_and_alert(send) == []                    # cooldown: not re-texted every grab
    assert api.list_messages(5)[0].decision == "sent"


def test_bridge_refusal_is_recorded_not_marked_alerted(db):
    api.remember_chat("owner-1", None)
    assert alerts.check_and_alert(lambda req: (False, "chat never texted the line")) == []
    assert api.list_messages(5)[0].decision == "blocked"
    assert not api.recently_alerted("below_par", "pepperoni")    # will retry next poll


def test_set_par_correction_changes_below_par(db):
    assert not any(r.below_par for r in api.get_inventory()) is False   # 8 < 10: below par
    out = set_par.invoke({"item": "Pepperoni", "par": 5})
    assert '"new_par": 5' in out
    assert not next(r for r in api.get_inventory() if r.item == "pepperoni").below_par
    assert "error" in set_par.invoke({"item": "anchovies", "par": 5})
    assert "error" in set_par.invoke({"item": "pepperoni", "par": -1})


def test_correct_grab_tool_updates_ledger(db):
    t = datetime.now(TZ)
    api.record_grab({"grab_id": "g1", "zone": "pepperoni", "camera_id": "c", "source": "webcam",
                     "t_start": t, "t_end": t, "item": "pepperoni", "action": "took", "qty_label": "handful",
                     "confidence": 0.9, "status": "logged", "frames": {"before": "", "during": [], "after": ""}}, [])
    before = next(v for v in api.get_variance(t.date(), t.date()) if v.item == "pepperoni").actual
    out = correct_grab.invoke({"grab_id": "g1", "action": "put_back", "item": "pepperoni", "qty_label": "handful"})
    assert '"status": "confirmed"' in out
    after = next(v for v in api.get_variance(t.date(), t.date()) if v.item == "pepperoni").actual
    assert round(after - before, 2) == -0.5                     # took 0.25 -> put back 0.25
    assert "error" in correct_grab.invoke({"grab_id": "nope", "action": "took", "item": "pepperoni", "qty_label": "pinch"})


def test_cooldown_is_per_chat_so_a_failed_chat_still_gets_retried(db):
    api.remember_chat("owner-a", None)
    api.remember_chat("owner-b", None)
    tries = []

    def flaky(req):
        tries.append(req.chat_id)
        return (req.chat_id == "owner-a", None if req.chat_id == "owner-a" else "not reachable")

    alerts.check_and_alert(flaky)                       # a succeeds, b fails
    tries.clear()
    alerts.check_and_alert(lambda req: (tries.append(req.chat_id) or True, None))
    assert tries == ["owner-b"], "b must be retried; a is in cooldown"
