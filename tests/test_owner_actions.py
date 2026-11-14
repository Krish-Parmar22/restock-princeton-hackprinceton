"""Owner actions by text: counts, reorder plan, the live Excel sheet + its picture, and the
authenticator gate in front of bank info (codes are checked in code, never seen by the model)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import openpyxl
import psycopg
import pyotp
import pytest
from psycopg.types.json import Jsonb

from app import secure, sheet
from app.agent import context, runtime
from app.agent.tools import get_bank_statement, record_count, reorder_plan, show_sheet
from app.contracts import InboundMessage, InboundReply
from app.db.conn import apply_schema, connect
from app.ledger import impl
from app.ledger.impl import TZ

URL = "postgresql://localhost:5432/pizza_test_owner"
SECRET = pyotp.random_base32()


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
                         (name, par, Jsonb({"pinch": 1, "handful": 2, "whole_container": 6})))
            conn.execute("INSERT INTO counts (ts, item_id, qty) SELECT %s, id, %s FROM items WHERE name = %s",
                         (datetime.now(TZ) - timedelta(hours=1), have, name))
    fake = SimpleNamespace(owner_totp_secret=SECRET, sheet_path=tmp_path / "Inventory.xlsx")
    monkeypatch.setattr(secure, "settings", fake)
    monkeypatch.setattr(sheet, "settings", fake)
    yield
    impl.use_database(None)


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
    assert wb.sheetnames[:2] == ["Inventory", "Orders"]
    orders = wb["Orders"]
    assert orders["B2"].value == "Coke" and orders["C2"].value == 8 and orders["H2"].value == "approved"
    assert "ordered 8 coke" in wb["Inventory"]["A2"].value
    with open(out["png"], "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"
    with context.run_context("chat-1", inbound=True) as ctx:
        show_sheet.invoke({})
        assert runtime._sheet_picture(ctx)[0].endswith(".png")


def test_bank_info_is_locked_until_a_valid_code(db):
    with context.run_context("chat-1", inbound=True, text="what's our bank balance?"):
        locked = json.loads(get_bank_statement.invoke({"days": 7}))
    assert locked["locked"] and "6-digit code" in locked["say"] and "balance" not in locked
    assert secure.pending("chat-1") == "what's our bank balance?"

    assert secure.try_code("chat-1", "000000").status == "bad"
    code = pyotp.TOTP(SECRET).now()
    res = secure.try_code("chat-1", code[:3] + " " + code[3:])   # "123 456" is fine too
    assert res.status == "ok" and res.question == "what's our bank balance?"
    assert secure.try_code("chat-1", code).status == "reused"     # one code, one use
    with context.run_context("chat-1", inbound=True, text="balance?"):
        st = json.loads(get_bank_statement.invoke({"days": 7}))
    assert "balance" in st and st["account"].startswith("Business checking")
    secure.lock("chat-1")
    assert not secure.is_unlocked("chat-1")
    assert not secure.is_unlocked("chat-2"), "unlocking one chat never unlocks another"


def test_three_wrong_codes_lock_it_out(db):
    assert [secure.try_code("c", "111111").status for _ in range(3)] == ["bad", "bad", "locked_out"]
    assert secure.try_code("c", pyotp.TOTP(SECRET).now()).status == "locked_out", "even the right code waits"
    later = datetime.now(timezone.utc) + timedelta(minutes=secure.LOCKOUT_MIN + 1)
    assert secure.try_code("c", pyotp.TOTP(SECRET).at(later), at=later).status == "ok"


def test_unlock_expires(db):
    secure.try_code("c", pyotp.TOTP(SECRET).now())
    with connect(URL) as conn:
        conn.execute("UPDATE auth_state SET unlocked_until = now() - interval '1 minute' WHERE chat_id = 'c'")
    assert not secure.is_unlocked("c")


def test_gate_handles_codes_without_the_model(db, monkeypatch):
    seen = []
    monkeypatch.setattr(runtime, "run_inbound", lambda agent, msg: seen.append(msg.text) or InboundReply(reply="Balance: $1.00"))
    msg = lambda t: InboundMessage(chat_id="chat-9", sender="+1", text=t, ts=datetime.now(timezone.utc))
    assert runtime._security_gate(object(), msg("how much pepperoni?")) is None        # normal texts pass through
    secure.request_code("chat-9", "what's our balance?")
    bad = runtime._security_gate(object(), msg("123456"))
    assert "didn't match" in bad.reply and seen == [], "a code is never given to the agent"
    ok = runtime._security_gate(object(), msg(pyotp.TOTP(SECRET).now()))
    assert ok.reply.startswith("Verified.") and "Balance: $1.00" in ok.reply
    assert len(seen) == 1 and seen[0].startswith("what's our balance?"), "the original question is re-run"
    assert pyotp.TOTP(SECRET).now() not in seen[0], "never the code itself"
    assert runtime._security_gate(object(), msg("lock")).reply.startswith("Locked")
    assert not secure.is_unlocked("chat-9")
