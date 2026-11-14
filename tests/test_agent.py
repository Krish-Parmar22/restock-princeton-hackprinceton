"""Agent tests: the real create_deep_agent graph, driven by a scripted fake model.

No API keys needed. The ledger is mocked, so these never touch Postgres.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.build import build_agent
from app.agent.guard import FALLBACK, split_claims
from app.agent.runtime import run_inbound, yes_no
from app.contracts import InboundMessage, InventoryRow
from app.d1.base import D1Answer
from app.ledger import api as ledger


class ScriptedModel(GenericFakeChatModel):
    """Fake chat model that emits a fixed sequence of AIMessages (incl. tool calls)."""

    def bind_tools(self, tools, **kwargs):
        return self


def ai(text="", **calls):
    tcs = [{"name": name, "args": args, "id": f"call_{name}_{i}", "type": "tool_call"}
           for i, (name, args) in enumerate(calls.items())]
    return AIMessage(content=text, tool_calls=tcs)


class KeywordD1:
    """d1 stand-in: claims containing a 'bad' keyword score low, a 'meh' keyword mid, else high."""

    def __init__(self, bad=("stole", "stealing"), meh=("probably",)):
        self.bad, self.meh, self.calls = bad, meh, []

    def ask(self, questions, state, images=None):
        self.calls.append([q.instructions for q in questions])
        out = {}
        for q in questions:
            t = q.instructions.lower()
            p = 0.1 if any(b in t for b in self.bad) else 0.7 if any(m in t for m in self.meh) else 0.95
            out[q.id] = D1Answer(answer="yes" if p >= 0.5 else "no", probs={"yes": p, "no": 1 - p}, confidence=max(p, 1 - p))
        return out


@pytest.fixture()
def fake_ledger(monkeypatch):
    rec = {"messages": [], "orders": []}
    monkeypatch.setattr(ledger, "get_inventory", lambda at=None: [
        InventoryRow(item="pepperoni", unit="lb", on_hand=4.25, par=10, used_today=3, unit_cost=4.5, below_par=True)])
    monkeypatch.setattr(ledger, "record_message",
                        lambda chat_id, text, score, decision, claims: rec["messages"].append((decision, text, score)))
    monkeypatch.setattr(ledger, "draft_order", lambda item, qty, chat_id=None, supplier=None: rec["orders"].append(("draft", item, qty)) or 7)
    monkeypatch.setattr(ledger, "set_order_status", lambda oid, status: rec["orders"].append((status, oid)))
    return rec


def make_agent(script, d1=None):
    model = ScriptedModel(messages=iter(script))
    return build_agent(model, checkpointer=InMemorySaver(), d1_client=d1 or KeywordD1(), fallback="")


def msg(text, chat="owner-1"):
    return InboundMessage(chat_id=chat, sender="owner", text=text, ts=datetime.now(timezone.utc))


def test_split_claims_drops_questions_and_filler():
    assert split_claims("Hi! Pepperoni is at 4.25 lb. Want me to reorder? Thanks.") == ["Pepperoni is at 4.25 lb."]


def test_yes_no():
    assert yes_no("yes please") is True and yes_no("Yep, do it") is True
    assert yes_no("no") is False and yes_no("don't") is False and yes_no("how much?") is None


def test_guard_blocks_then_agent_rewrites(fake_ledger):
    agent = make_agent([
        ai(get_stock={"item": "pepperoni"}),
        ai(send_text={"message": "Someone stole 5 lb of pepperoni last night."}),
        ai(send_text={"message": "Pepperoni is at 4.25 lb, below the par of 10."}),
        ai("done"),
    ])
    out = run_inbound(agent, msg("how much pepperoni?"))
    assert out.reply == "Pepperoni is at 4.25 lb, below the par of 10."
    assert [d for d, *_ in fake_ledger["messages"]] == ["blocked", "sent"]


def test_guard_softens_mid_confidence(fake_ledger):
    agent = make_agent([ai(send_text={"message": "Pepperoni probably ran over on Tuesday evening."}), ai("done")])
    out = run_inbound(agent, msg("why are we short?"))
    assert out.reply.startswith("Looks like: ")
    assert fake_ledger["messages"][0][0] == "softened"


def test_guard_caps_retries_with_fallback(fake_ledger):
    agent = make_agent([
        ai(send_text={"message": "Someone stole the pepperoni on Tuesday."}),
        ai(send_text={"message": "Staff are stealing pepperoni every Thursday."}),
        ai("done"),
    ])
    out = run_inbound(agent, msg("what happened?"))
    assert out.reply == FALLBACK
    assert [d for d, *_ in fake_ledger["messages"]] == ["blocked", "blocked", "sent"]


def test_order_waits_for_owner_then_resumes_on_yes(fake_ledger):
    agent = make_agent([
        ai(send_text={"message": "Pepperoni is at 4.25 lb, below the par of 10."}),
        ai(place_order={"item": "pepperoni", "qty": 20}),
        ai(send_text={"message": "Ordered 20 lb of pepperoni from the supplier."}),
        ai("done"),
    ])
    first = run_inbound(agent, msg("should I reorder?"))
    assert "below the par of 10" in first.reply and "Approve this order: 20 lb of pepperoni" in first.reply
    assert fake_ledger["orders"] == []                     # nothing placed before the owner says yes
    second = run_inbound(agent, msg("yes"))
    assert fake_ledger["orders"] == [("draft", "pepperoni", 20), ("approved", 7)]
    assert second.reply == "Ordered 20 lb of pepperoni from the supplier."


def test_order_rejected_on_no(fake_ledger):
    agent = make_agent([
        ai(place_order={"item": "pepperoni", "qty": 20}),
        ai(send_text={"message": "Okay, I did not place the pepperoni order."}),
        ai("done"),
    ])
    run_inbound(agent, msg("reorder pepperoni"))
    out = run_inbound(agent, msg("no"))
    assert fake_ledger["orders"] == []
    assert out.reply == "Okay, I did not place the pepperoni order."


def test_inbound_endpoint_echoes_without_model(monkeypatch):
    from fastapi.testclient import TestClient
    from app.agent import runtime
    from app.api.server import app
    monkeypatch.setattr(runtime, "get_agent", lambda: None)  # independent of AGENT_MODEL in .env
    r = TestClient(app).post("/inbound", json={"chat_id": "c", "sender": "s", "text": "hi", "ts": "2026-11-14T12:00:00Z"})
    assert r.status_code == 200 and "echo: hi" in r.json()["reply"]


# ---- regressions from Codex review ----

def test_accusing_question_is_checked():
    from app.agent.guard import split_claims
    assert split_claims("Did Maria steal 5 lb?") == ["Did Maria steal 5 lb?"]
    assert split_claims("Want me to check the clips?") == []          # neutral question: skipped
    assert split_claims("Thanks!") == []


def test_partial_yes_does_not_approve_original_order(fake_ledger):
    from app.agent.runtime import approval_answer
    assert approval_answer("Yes, but only order 2 lb") is None
    agent = make_agent([
        ai(place_order={"item": "pepperoni", "qty": 20}),
        ai(send_text={"message": "Okay, I did not place the 20 lb pepperoni order."}),
        ai("done"),
    ])
    run_inbound(agent, msg("reorder pepperoni"))
    run_inbound(agent, msg("Yes, but only order 2 lb"))
    assert fake_ledger["orders"] == []


def test_plain_text_reply_still_goes_through_guard(fake_ledger):
    agent = make_agent([ai("Pepperoni is at 4.25 lb, below the par of 10.")])
    out = run_inbound(agent, msg("how much pepperoni?"), d1_client=KeywordD1())
    assert out.reply == "Pepperoni is at 4.25 lb, below the par of 10."
    assert fake_ledger["messages"][0][0] == "sent"


def test_plain_text_accusation_is_replaced(fake_ledger):
    agent = make_agent([ai("Your night cook stole 3 lb of pepperoni.")])
    out = run_inbound(agent, msg("what happened?"), d1_client=KeywordD1())
    assert out.reply == FALLBACK


def test_evidence_scoped_to_current_turn():
    from langchain_core.messages import HumanMessage, ToolMessage
    from app.agent.guard import current_turn
    old = ToolMessage(content="old", tool_call_id="1", name="get_stock")
    new = ToolMessage(content="new", tool_call_id="2", name="get_stock")
    assert current_turn([HumanMessage("a"), old, HumanMessage("b"), new]) == [new]


def test_bad_order_qty_rejected():
    from app.ledger import impl
    for q in (0, -5, float("nan"), 10_000):
        with pytest.raises(ValueError):
            impl.draft_order("pepperoni", q)


def test_guard_numbers_are_checked_in_code_not_by_d1():
    """Calibration (docs/guard-calibration.md): real d1 scored '$92.7' (true: $9.27) at 0.899."""
    from app.agent.guard import numbers_check
    ev = '[{"on_hand": 21.25, "variance_qty": 0.98, "variance_usd": 9.27, "example_grab_ids": ["syn-00844_pepperoni"]}]'
    assert numbers_check("That gap is about $9.27.", ev) is True
    assert numbers_check("That gap is $92.7.", ev) is False
    assert numbers_check("Pepperoni ran 0.98 lb over on Nov 10.", ev) is True   # date ignored
    assert numbers_check("Ran 844 lb over.", ev) is False                       # ids aren't evidence
    assert numbers_check("Mostly on Tuesday.", ev) is None


def test_guard_blocks_wrong_number_even_if_d1_says_yes(fake_ledger):
    from langchain_core.messages import HumanMessage, ToolMessage
    from app.agent.guard import score_message
    state = {"messages": [HumanMessage("q"), ToolMessage('{"variance_usd": 9.27}', tool_call_id="1", name="get_variance")]}
    yes = KeywordD1(bad=(), meh=())                         # d1 says 0.95 to everything
    assert score_message("That gap is worth about $92.70 this week.", state, yes)[0] == 0.0
    assert score_message("That gap is worth about $9.27 this week.", state, yes)[0] >= 0.9
