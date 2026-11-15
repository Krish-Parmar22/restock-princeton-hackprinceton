"""Owner actions by text: counts, reorder plan, the live Excel sheet + its picture, and replies to alerts."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import openpyxl
import psycopg
import pytest
from psycopg.types.json import Jsonb

from app import sheet
from app.agent import context, runtime
from app.agent.tools import record_count, reorder_plan, show_sheet
from app.contracts import InboundMessage, InboundReply
from app.db.conn import apply_schema, connect
from app.ledger import impl
from app.ledger.impl import TZ

URL = "postgresql://localhost:5432/pizza_test_owner"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    with psycopg.connect("postgresql://localhost:5432/postgres", autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS pizza_test_owner WITH (FORCE)")
        c.execute("CREATE DATABASE pizza_test_owner")
    apply_schema(URL)
    impl.use_database(URL)
    with connect(URL) as conn:
        for name, par, have in (("coke", 6, 4), ("sprite", 6, 20)):
            conn.execute("INSERT INTO items (name, unit, unit_cost, par, qty_per_label) VALUES (%s,'each',1.0,%s,%s)",
                         (name, par, Jsonb({"1": 1, "2": 2, "3": 3})))
            conn.execute("INSERT INTO counts (ts, item_id, qty) SELECT %s, id, %s FROM items WHERE name = %s",
                         (datetime.now(TZ) - timedelta(hours=1), have, name))
    fake = SimpleNamespace(sheet_path=tmp_path / "Inventory.xlsx")
    monkeypatch.setattr(sheet, "settings", fake)
    yield
    impl.use_database("postgresql://localhost:5432/pizza_test_default")   # never back to live


def test_count_by_text_sets_on_hand_and_marks_the_sheet(db):
    with context.run_context("chat-1", inbound=True, text="we have 18 cokes") as ctx:
        out = json.loads(record_count.invoke({"item": "Coke", "qty": 18}))
        assert "on_hand" in out, out
        assert out["on_hand"] == 18 and out["below_par"] is False
        assert ctx.sheet_changes == ["coke count 18"]
        assert "error" in json.loads(record_count.invoke({"item": "coke", "qty": -3}))
        assert "error" in json.loads(record_count.invoke({"item": "pepsi", "qty": 3}))


def test_reorder_plan_tops_up_to_twice_par(db):
    plan = json.loads(reorder_plan.invoke({}))
    assert plan["items"] == [{"item": "coke", "unit": "each", "on_hand": 4.0, "on_order": 0.0, "par": 6.0,
                              "order_qty": 8, "unit_cost": 1.0, "cost": 8.0}]
    assert plan["total_cost"] == 8.0


def test_sheet_refresh_writes_xlsx_with_orders_and_a_picture(db):
    impl.set_order_status(impl.draft_order("coke", 8, "chat-1"), "approved")
    out = sheet.refresh("ordered 8 coke")
    wb = openpyxl.load_workbook(out["xlsx"])
    assert wb.sheetnames[:3] == ["Hoja 1", "Inventory", "Orders"]   # Casa Piada's own sheet first
    orders = wb["Orders"]
    assert orders["B2"].value == "Coke" and orders["C2"].value == 8 and orders["H2"].value == "approved"
    assert "ordered 8 coke" in wb["Inventory"]["A2"].value
    with open(out["png"], "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"
    with context.run_context("chat-1", inbound=True) as ctx:
        show_sheet.invoke({})
        assert runtime._sheet_picture(ctx)[0].endswith(".png")


def test_drinks_questions_ask_for_an_exact_count(monkeypatch):
    from pathlib import Path
    from app.contracts import GrabMeta, GrabPackage
    from app.grabs import checks
    monkeypatch.setattr(checks, "settings", SimpleNamespace(scene="drinks"))
    now = datetime.now(timezone.utc)
    pkg = GrabPackage(folder=Path("/tmp"), meta=GrabMeta(grab_id="g", camera_id="c", zone="coke", t_start=now,
                      t_end=now + timedelta(seconds=2), frame_times=[], source="webcam"),
                      before=Path("/tmp/b.jpg"), during=[Path("/tmp/d.jpg")], after=Path("/tmp/a.jpg"))
    _, grab = checks.build_questions(pkg, ["coke", "sprite"])
    q = next(q for q in grab["questions"] if q.id == "qty")
    assert q.options == ("1", "2", "3", "more") and "cans or bottles" in q.instructions
    assert "drinks fridge" in grab["state"] and "pizza" not in grab["state"]


@pytest.mark.parametrize("label,action,expect,status", [
    ("1", "took", 3.0, "logged"), ("2", "took", 2.0, "logged"), ("3", "took", 1.0, "logged"),
    ("2", "put_back", 6.0, "logged"), ("1", "touched", 4.0, "logged"), ("more", "took", 4.0, "flagged")])
def test_exact_can_counts_move_stock_exactly(db, label, action, expect, status):
    now = datetime.now(TZ)
    impl.record_grab({"grab_id": f"g-{label}-{action}", "zone": "coke", "camera_id": "c", "source": "webcam",
                      "t_start": now, "t_end": now, "item": "coke", "action": action, "qty_label": label,
                      "confidence": 0.95, "status": "logged", "frames": {"before": "", "during": [], "after": ""}}, [])
    inv = {r.item: r for r in impl.get_inventory(now + timedelta(minutes=1))}
    assert inv["coke"].on_hand == expect           # started at 4 cans
    assert impl.list_grabs(now.date(), "all")[0].status == status


# ---- Codex adversarial review (Nov 14) regressions -------------------------------------------

def test_only_our_own_pictures_can_be_attached(db, tmp_path):
    other = tmp_path / "x.png"
    other.write_bytes(b"\x89PNG")
    assert not runtime.allowed_image(str(other))
    png = sheet.refresh()["png"]
    assert runtime.allowed_image(png)


def test_default_general_purpose_subagent_is_replaced_read_only():
    from app.agent import build
    names = {t.name for t in build.GENERAL["tools"]}
    assert build.GENERAL["name"] == "general-purpose"
    assert not names & {"send_text", "place_order", "place_orders"}


def test_wording_is_chosen_per_box(db, monkeypatch):
    """Cheese bowl and Sprite shelf in one demo: counted items get exact counts, bulk food pinch/handful."""
    from app.grabs import checks
    monkeypatch.setattr(checks, "settings", SimpleNamespace(scene="auto"))
    with connect(URL) as conn:
        conn.execute("INSERT INTO items (name, unit, unit_cost, par, qty_per_label) VALUES ('cheese','lb',3.8,15,%s)",
                     (Jsonb({"pinch": 0.05, "handful": 0.3, "whole_container": 5}),))
    assert checks.scene_for("coke") == "drinks" and checks.scene_for("cheese") == "pizza"
    monkeypatch.setattr(checks, "settings", SimpleNamespace(scene="drinks"))
    assert checks.scene_for("cheese") == "drinks", "SCENE forces one wording"


def test_briefing_facts_are_computed_not_counted_by_the_model(db):
    """The morning text's numbers come from one tool, with units, so the d1 guard can verify them."""
    from app.agent.tools import briefing_facts
    now = datetime.now(TZ)
    impl.record_grab({"grab_id": "f1", "zone": "coke", "camera_id": "c", "source": "webcam", "t_start": now,
                      "t_end": now, "item": "coke", "action": "took", "qty_label": "more", "confidence": 0.5,
                      "status": "flagged", "frames": {"before": "", "during": [], "after": ""}}, [])
    f = json.loads(briefing_facts.invoke({}))
    assert f["grabs_need_ok"] == 1 and "1 camera grab need" in f["suggested_text"]
    assert f["order"] and f["order"][0]["item"] == "coke" and f"${f['order_total']:,.2f}" in f["suggested_text"]


def test_one_yes_answers_the_alerts_reorder_offer(db, monkeypatch):
    """Alert: "Reorder 8 for $8.00? Reply yes or no." -> "yes" places exactly that, in code."""
    from app.agent import alerts
    from app.ledger import api
    api.remember_chat("owner-x", "+1")
    sent = []
    monkeypatch.setattr(alerts, "_tell_agent", lambda chat, text: None)
    out = alerts.check_and_alert(lambda req: (sent.append(req) or True, None))
    assert out and "Reorder 8 for $8.00? Reply yes or no." in sent[0].text      # coke 4 on hand, par 6 -> 2x par
    monkeypatch.setattr(runtime, "pending_approval", lambda agent, cfg: [])
    msg = lambda t: InboundMessage(chat_id="owner-x", sender="+1", text=t, ts=datetime.now(timezone.utc))
    r = runtime._answer_offer(object(), msg("yes"))
    assert r.reply.startswith("Done: 8 coke on order ($8.00)")
    assert [o["status"] for o in api.list_orders()] == ["approved"]
    assert runtime._answer_offer(object(), msg("yes")) is None, "an offer is answered once"
    assert runtime._answer_offer(object(), msg("how much coke?")) is None


@pytest.mark.parametrize("reply,qty", [("Reorder 30 for Thanksgiving!", 30), ("make it 12", 12), ("yes, 15", 15)])
def test_answering_the_offer_with_a_different_number(db, monkeypatch, reply, qty):
    from app.agent import alerts
    from app.ledger import api
    api.remember_chat("owner-q", "+1")
    monkeypatch.setattr(alerts, "_tell_agent", lambda chat, text: None)
    alerts.check_and_alert(lambda req: (True, None))
    monkeypatch.setattr(runtime, "pending_approval", lambda agent, cfg: [])
    r = runtime._answer_offer(object(), InboundMessage(chat_id="owner-q", sender="+1", text=reply,
                                                       ts=datetime.now(timezone.utc)))
    assert r.reply.startswith(f"Done: {qty} coke on order")
