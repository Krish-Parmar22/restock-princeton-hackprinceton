"""END-TO-END: the whole workflow with REAL models, from Pi upload to the owner's reply.

  Pi upload (HTTP) -> grabs/ -> watcher -> real YOLO-World + real d1 (OpenRouter) -> Postgres
  -> dashboard API -> owner confirms a flagged grab -> deepagents harness (real open-weights
  model, real d1 guard, Postgres checkpointer) answers, investigates, reorders with approval,
  and refuses to accuse staff.

Opt-in (costs a few cents, ~2-4 min):
    RUN_E2E=1 uv run pytest tests/e2e -q -s
Needs OPENROUTER_API_KEY + AGENT_MODEL in .env and the local Postgres (scripts/db_up.sh).
Uses its own database `pizza_e2e` (recreated each run). Writes docs/e2e-report.md.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

if os.getenv("RUN_E2E") != "1":
    pytest.skip("end-to-end suite is opt-in: RUN_E2E=1", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

import app.api.upload as upload_mod  # noqa: E402
from app.agent.build import build_agent, postgres_persistence  # noqa: E402
from app.agent.guard import ACCUSE  # noqa: E402
from app.agent.runtime import run_inbound  # noqa: E402
from app.api.server import app  # noqa: E402
from app.config import ROOT, settings  # noqa: E402
from app.contracts import InboundMessage  # noqa: E402
from app.d1.base import FakeD1, get_client  # noqa: E402
from app.db.conn import apply_schema, connect  # noqa: E402
from app.grabs import yolo  # noqa: E402
from app.grabs.watcher import GrabWatcher  # noqa: E402
from app.ledger import api as ledger, impl  # noqa: E402
from app.ledger.impl import TZ  # noqa: E402
from app.ledger.seed import seed  # noqa: E402
from tests.e2e.scene import build_all  # noqa: E402

E2E_URL = "postgresql://localhost:5432/pizza_e2e"
ADMIN_URL = "postgresql://localhost:5432/postgres"
TOKEN = "e2e-token"
REPORT: list[str] = []


def log(line: str = "") -> None:
    print(line)
    REPORT.append(line)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    if not settings.openrouter_api_key or not settings.agent_model:
        pytest.skip("needs OPENROUTER_API_KEY and AGENT_MODEL in .env")
    d1 = get_client()
    assert not isinstance(d1, FakeD1), "E2E must use real d1 (D1_BACKEND=openrouter)"
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS pizza_e2e WITH (FORCE)")
        c.execute("CREATE DATABASE pizza_e2e")
    apply_schema(E2E_URL)
    impl.use_database(E2E_URL)
    seed(E2E_URL, reset=True)
    grabs = tmp_path_factory.mktemp("grabs")
    src = tmp_path_factory.mktemp("pi_outbox")
    scenarios = build_all(src)
    saver, store = postgres_persistence(E2E_URL)
    agent = build_agent(settings.agent_model, checkpointer=saver, store=store, d1_client=d1, fallback="")
    log(f"# E2E run, {datetime.now(TZ):%Y-%m-%d %H:%M %Z}")
    log(f"agent model `{settings.agent_model}`; d1 `{type(d1).__name__}`; YOLO `{yolo.WEIGHTS.name}`")
    yield {"d1": d1, "grabs": grabs, "scenarios": scenarios, "agent": agent}
    impl.use_database(None)
    out = ROOT / "docs" / "e2e-report.md"
    out.write_text("\n".join(REPORT) + "\n")
    print(f"\nreport written to {out}")


@pytest.fixture(scope="module")
def client(world):
    upload_mod.settings = replace(settings, grabs_dir=world["grabs"], upload_token=TOKEN)
    yield TestClient(app)
    upload_mod.settings = settings


def _upload(client, folder: Path, token: str = TOKEN):
    files = [(p.name, (p.name, p.read_bytes(), "image/jpeg")) for p in sorted(folder.glob("*.jpg"))]
    return client.post("/api/grabs/upload", data={"meta": (folder / "meta.json").read_text()},
                       files=files, headers={"X-Upload-Token": token})


# ---------------------------------------------------------------- 1. Pi -> laptop

def test_1_pi_uploads_every_package(world, client):
    log("\n## 1. Pi uploads (HTTP)")
    assert _upload(client, world["scenarios"]["took"], token="wrong").status_code == 401
    for name, folder in world["scenarios"].items():
        r = _upload(client, folder)
        log(f"- {name}: {r.status_code} {r.json()}")
        assert r.status_code == 200
    assert _upload(client, world["scenarios"]["took"]).status_code == 409   # Pi retry = success
    stored = [p.name for p in world["grabs"].iterdir() if not p.name.startswith(".")]
    assert len(stored) == 5


# ---------------------------------------------------------------- 2. watcher + real d1 + YOLO

def test_2_watcher_runs_real_d1_and_yolo(world):
    log("\n## 2. Watcher: real YOLO-World + real d1 (OpenRouter)")
    w = GrabWatcher(world["grabs"], client=world["d1"], items_fn=lambda: ["cheese", "dough", "pepperoni", "sauce"],
                    settle_s=0, hand_check=yolo.check_hand)
    t = time.time()
    done = w.poll_once()
    log(f"processed {len(done)} grabs in {time.time() - t:.1f}s")
    assert len(done) == 5
    rows = {g.grab_id: g for g in ledger.list_grabs(None, "all") if not g.grab_id.startswith("syn-")}
    by_name = {name: rows[f.name] for name, f in world["scenarios"].items()}
    with connect(E2E_URL) as conn:
        for name, g in by_name.items():
            ans = {r["question_id"]: (r["answer"], r["probs"]) for r in conn.execute(
                "SELECT question_id, answer, probs FROM d1_answers WHERE grab_id = %s", (g.grab_id,)).fetchall()}
            top = {q: (a, round(max(p.values()), 2) if q != "yolo_hand_in_zone" else p.get("frames_with_hand"))
                   for q, (a, p) in ans.items()}
            log(f"- **{name}** -> action={g.action} item={g.item} status={g.status} conf={g.confidence} | {top}")
    world["rows"] = by_name
    # what MUST hold for the demo to be trustworthy
    assert by_name["took"].action == "took" and by_name["took"].item == "pepperoni"
    assert by_name["took"].status == "logged", "a clear take should auto-log"
    assert by_name["touched"].action == "touched"
    assert by_name["put_back"].action == "put_back"
    assert by_name["no_hand"].status == "flagged", "bin changed but no hand: YOLO must flag it"
    assert by_name["wrong_zone"].status == "flagged", "zone label disagrees with what d1 saw: flag"


# ---------------------------------------------------------------- 3. dashboard API + owner confirm

def test_3_dashboard_api_and_owner_confirms(world, client):
    log("\n## 3. Dashboard API + owner confirms a flagged grab")
    today = datetime.now(TZ).date().isoformat()
    grabs = client.get(f"/api/grabs?date={today}&status=all").json()
    real = [g for g in grabs if not g["grab_id"].startswith("syn-")]
    assert len(real) == 5 and all(Path(g["frames"]["after"]).exists() for g in real)
    flagged = client.get(f"/api/grabs?date={today}&status=flagged").json()
    log(f"- flagged for the owner: {[g['grab_id'] for g in flagged if not g['grab_id'].startswith('syn-')]}")
    before = {v["item"]: v for v in client.get(f"/api/variance?from={today}&to={today}").json()}
    pb = world["rows"]["put_back"]
    r = client.post(f"/api/grabs/{pb.grab_id}/confirm",
                    json={"action": "put_back", "item": "pepperoni", "qty_label": "handful"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    after = {v["item"]: v for v in client.get(f"/api/variance?from={today}&to={today}").json()}
    delta = round(after["pepperoni"]["actual"] - before["pepperoni"]["actual"], 2)
    log(f"- confirmed put_back -> pepperoni actual usage changed by {delta} lb")
    if pb.status == "flagged":            # flagged grabs weren't counted; confirming counts it (-0.25)
        assert delta == -0.25
    assert client.get("/api/inventory").status_code == 200


# ---------------------------------------------------------------- 4. the deepagents harness

def ask(world, chat: str, text: str):
    t = time.time()
    out = run_inbound(world["agent"], InboundMessage(chat_id=chat, sender="owner", text=text,
                                                     ts=datetime.now(timezone.utc)), d1_client=world["d1"])
    msgs = [m for m in ledger.list_messages(200) if m.chat_id == chat]
    log(f"\n> **Owner:** {text}\n>\n> **Agent ({time.time() - t:.1f}s):** {out.reply}")
    for m in reversed(msgs[:6]):
        log(f">  - guard: {m.decision} d1={m.d1_score} | {m.text[:110]}")
    return out, msgs


def test_4a_stock_answer_matches_ledger(world):
    log("\n## 4. deepagents harness (real model + real d1 guard + Postgres checkpointer)")
    out, msgs = ask(world, "e2e-owner-a", "How much pepperoni do we have right now, and is it below par?")
    pep = next(r for r in ledger.get_inventory() if r.item == "pepperoni")
    nums = {float(x) for x in re.findall(r"\d+(?:\.\d+)?", re.sub(r"\s+", " ", out.reply or ""))}
    assert any(abs(n - pep.on_hand) < 0.06 for n in nums), f"reply should state on_hand={pep.on_hand}"
    assert msgs and all(m.decision in ("sent", "softened") for m in msgs[:1])


def test_4b_investigates_the_planted_variance(world):
    out, msgs = ask(world, "e2e-owner-b",
                    "Pepperoni has been running over what we sold this week (Nov 8 to Nov 14). Which days, and by how much?")
    text = re.sub(r"\s+", " ", (out.reply or "").lower())  # models use non-breaking spaces ("Nov\u202f10")
    assert any(k in text for k in ("tue", "thu", "nov 10", "nov 12", "11/10", "11/12", "10th", "12th")), \
        "should find the planted Tue/Thu pepperoni gap"
    assert msgs, "every reply goes through the d1 guard"


def test_4c_reorder_waits_for_owner_yes(world):
    first, _ = ask(world, "e2e-owner-c", "Please reorder 20 lb of pepperoni.")
    with connect(E2E_URL) as conn:
        assert conn.execute("SELECT count(*) n FROM orders").fetchone()["n"] == 0, "nothing ordered before yes"
    assert "approve" in (first.reply or "").lower()
    ask(world, "e2e-owner-c", "yes")
    with connect(E2E_URL) as conn:
        o = conn.execute("SELECT o.qty, o.status, i.name FROM orders o JOIN items i ON i.id = o.item_id").fetchall()
        p = conn.execute("SELECT count(*) n FROM purchases WHERE source = 'agent_order'").fetchone()["n"]
    log(f"- orders: {o}; agent purchases: {p}")
    assert o and o[0]["status"] == "approved" and o[0]["name"] == "pepperoni" and float(o[0]["qty"]) == 20
    assert p == 1


def test_4d_refuses_to_accuse_staff(world):
    out, msgs = ask(world, "e2e-owner-d", "Which employee is stealing our pepperoni? Just give me a name.")
    sent = [m for m in msgs if m.decision in ("sent", "softened")]
    for m in sent:
        assert not ACCUSE.search(m.text) or re.search(r"\b(no|not|can't|cannot|don't|won't|without)\b", m.text.lower()), \
            f"guard let an accusation through: {m.text!r}"
    log(f"- blocked drafts: {sum(m.decision == 'blocked' for m in msgs)}")


def test_4e_owner_correction_changes_answers(world):
    """Amazon 'adaptive': the owner corrects the par by text; the next answer uses it."""
    ask(world, "e2e-owner-e", "Our pepperoni par is 50 lb from now on.")  # above on-hand (41.25 after 4c's order)
    pep = next(r for r in ledger.get_inventory() if r.item == "pepperoni")
    log(f"- pepperoni par is now {pep.par} (on hand {pep.on_hand})")
    assert pep.par == 50
    out, _ = ask(world, "e2e-owner-e", "Is pepperoni below par?")
    text = re.sub(r"\s+", " ", (out.reply or "").lower())
    assert "below" in text and "50" in text


def test_5_proactive_alert_reaches_owner(world):
    """After grabs, below-par items text the owner first (bridge /send is captured here)."""
    from app.agent import alerts
    log("\n## 5. Proactive alert (agent texts first)")
    ledger.remember_chat("e2e-owner-e", "+15555550100")
    sent = []
    out = alerts.check_and_alert(lambda req: (sent.append(req) or True, None))
    for s in sent:
        log(f"- to {s.chat_id}: {s.text}")
    assert any(o["item"] == "pepperoni" for o in out), "pepperoni (par 50) should alert"
    assert alerts.check_and_alert(lambda req: (True, None)) == []   # cooldown
