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
    secure._SCHEMA_DONE.clear()
    with connect(URL) as conn:
        for name, par, have in (("coke", 6, 4), ("sprite", 6, 20)):
            conn.execute("INSERT INTO items (name, unit, unit_cost, par, qty_per_label) VALUES (%s,'each',1.0,%s,%s)",
                         (name, par, Jsonb({"1": 1, "2": 2, "3": 3})))
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

def _seed_bank(conn):
    conn.execute("INSERT INTO purchases (ts, item_id, qty, unit_cost, supplier, source) "
                 "SELECT now(), id, 300, 1.0, 'Shore Foods', 'invoice' FROM items WHERE name = 'coke'")


def test_bank_amounts_never_leave_a_locked_chat(db):
    from app import bank
    with connect(URL) as conn:
        _seed_bank(conn)
    bal = bank.statement(31)["balance"]
    leaky = f"Balance is ${bal:,.2f}"
    assert secure.leaks_bank_info("chat-L", leaky), "locked chat: a statement amount is blocked"
    assert not secure.leaks_bank_info("chat-L", "We have 18 cokes, par 6."), "stock numbers still flow"
    texts, _ = runtime._safe_out("chat-L", [leaky, "18 cokes left"], [])
    assert texts == [runtime.LOCKED_NOTE, "18 cokes left"]
    secure.try_code("chat-L", pyotp.TOTP(SECRET).now())
    assert not secure.leaks_bank_info("chat-L", leaky), "unlocked chat: allowed"
    assert secure.leaks_bank_info("chat-other", leaky), "unlocking one chat never opens another"


def test_only_our_own_pictures_can_be_attached(db, tmp_path):
    from app.config import settings as real
    qr = real.sheet_path.parent / "authenticator-qr.png"
    assert not runtime.allowed_image(str(qr)), "the authenticator QR can never be texted"
    other = tmp_path / "x.png"
    other.write_bytes(b"\x89PNG")
    assert not runtime.allowed_image(str(other))
    png = sheet.refresh()["png"]
    assert runtime.allowed_image(png)


def test_new_chat_ids_get_no_extra_guesses_and_codes_work_once_anywhere(db):
    assert secure.try_code("a", "111111").status == "bad"
    assert secure.try_code("b", "222222").status == "bad"
    assert secure.try_code("c", "333333").status == "locked_out", "3 wrong codes across chats lock the owner"
    later = datetime.now(timezone.utc) + timedelta(minutes=secure.LOCKOUT_MIN + 1)
    code = pyotp.TOTP(SECRET).at(later)
    assert secure.try_code("d", code, at=later).status == "ok"
    assert secure.try_code("e", code, at=later).status == "reused", "a code can't be replayed in another chat"


def test_lockout_ends_an_open_unlock(db):
    secure.try_code("u", pyotp.TOTP(SECRET).now())
    assert secure.is_unlocked("u")
    for _ in range(3):
        secure.try_code("u", "000000")
    assert not secure.is_unlocked("u")


def test_parallel_codes_count_every_failure_and_accept_a_code_once(db):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(lambda i: secure.try_code(f"p{i}", "999999").status, range(3)))
    assert "locked_out" in res, res
    later = datetime.now(timezone.utc) + timedelta(minutes=secure.LOCKOUT_MIN + 1)
    code = pyotp.TOTP(SECRET).at(later)
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda i: secure.try_code(f"q{i}", code, at=later).status, range(4)))
    assert res.count("ok") == 1, res


@pytest.mark.parametrize("text", ["123456", "123 456", "123  456", "my code is 123456", "1 2 3 4 5 6", "code: 123-456"])
def test_code_shaped_messages_are_kept_from_the_model(text):
    assert secure.looks_like_code(text)


@pytest.mark.parametrize("text", ["we have 18 cokes", "order 120 cans by 5pm", "balance?", "1234567"])
def test_normal_messages_are_not_codes(text):
    assert not secure.looks_like_code(text)


def test_default_general_purpose_subagent_is_replaced_read_only():
    from app.agent import build
    names = {t.name for t in build.GENERAL["tools"]}
    assert build.GENERAL["name"] == "general-purpose"
    assert not names & {"send_text", "get_bank_statement", "place_order", "place_orders"}


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
