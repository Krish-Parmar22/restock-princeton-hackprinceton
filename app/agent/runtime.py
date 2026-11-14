"""Running the agent for the Photon bridge.

handle_inbound: one owner text -> agent run (thread = chat_id) -> the guarded send_text
messages become the reply. If an order is waiting for approval, a yes/no reply resumes it.
notify: proactive message (e.g. daily summary); send_text POSTs to the bridge.
"""
from __future__ import annotations

import re
import threading
from collections import defaultdict

from langchain_core.messages import AIMessage
from langgraph.types import Command

from app.agent import context
from app.agent.build import build_agent, postgres_persistence
from app.agent.guard import current_turn, guard_final_text
from app.config import settings
from app.contracts import InboundMessage, InboundReply

YES = re.compile(r"^\s*(y|yes|yep|yeah|yup|ok|okay|sure|do it|go ahead|approve|approved|sounds good)\b", re.I)
NO = re.compile(r"^\s*(n|no|nope|nah|don'?t|do not|cancel|stop|reject|not now)\b", re.I)


def yes_no(text: str) -> bool | None:
    if NO.search(text):
        return False
    if YES.search(text):
        return True
    return None


def approval_answer(text: str) -> bool | None:
    """Strict: only a short, plain yes/no settles a pending order. 'Yes, but only 2 lb' is None
    (the order is rejected and the agent reconsiders with the owner's words)."""
    t = text.strip()
    if re.search(r"\d", t) or len(t.split()) > 4 or "but" in t.lower():
        return None
    return yes_no(t)


_chat_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def _chat_lock(chat_id: str) -> threading.Lock:
    """One run at a time per chat, so two quick 'yes' texts can't resume the same order twice."""
    with _locks_guard:
        return _chat_locks[chat_id]


_AGENT = {}
_AGENT_LOCK = threading.Lock()


def get_agent():
    """The production agent (built once, thread-safe), or None when no model is configured.
    Concurrent first texts used to race Postgres setup() (duplicate-key errors, live test Nov 14)."""
    if not settings.agent_model:
        return None
    if "agent" not in _AGENT:
        with _AGENT_LOCK:
            if "agent" not in _AGENT:
                saver, store = postgres_persistence()
                _AGENT["agent"] = build_agent(settings.agent_model, checkpointer=saver, store=store)
    return _AGENT["agent"]


def pending_approval(agent, config) -> list[dict]:
    """Pending place_order action requests on this thread ([] if none)."""
    snap = agent.get_state(config)
    out = []
    for intr in getattr(snap, "interrupts", ()) or ():
        val = intr.value if isinstance(intr.value, dict) else {}
        out.extend(val.get("action_requests", []))
    return out


def _approval_prompt(reqs: list[dict]) -> str:
    """Fixed template (not model text): every line, its cost from the ledger, and the total."""
    from app.ledger import api as ledger
    try:
        inv = {r.item: r for r in ledger.get_inventory()}
    except Exception:
        inv = {}
    lines, total = [], 0.0
    for r in reqs:
        a = r.get("args", {}) or {}
        orders = a.get("orders") if isinstance(a.get("orders"), list) else ([a] if a.get("item") else [])
        for o in orders:
            name = str(o.get("item", "")).lower().strip().replace(" ", "_")
            try:
                qty = float(o.get("qty"))
            except (TypeError, ValueError):
                continue
            row = inv.get(name)
            q = f"{qty:g}"
            if row is not None:
                cost = qty * row.unit_cost
                total += cost
                have = f"{row.on_hand:g}/{row.par:g}"
                lines.append(f"- {name.replace('_', ' ').title()}: {q} {row.unit} (${cost:,.2f}, on hand {have} par)")
            else:
                lines.append(f"- {name.replace('_', ' ')}: {q}")
        if not orders:
            lines.append(f"- {r.get('name', 'this')}")
    head = "Reorder plan:" if len(lines) > 1 else "Order:"
    tail = f"Total ${total:,.2f}. Approve? Reply yes or no." if total else "Approve? Reply yes or no."
    return "\n".join([head, *lines, tail])


def run_inbound(agent, msg: InboundMessage, d1_client=None) -> InboundReply:
    config = {"configurable": {"thread_id": msg.chat_id}}
    with _chat_lock(msg.chat_id), context.run_context(msg.chat_id, inbound=True, text=msg.text) as ctx:
        waiting = pending_approval(agent, config)
        if waiting:
            ok = approval_answer(msg.text)
            decision = ({"type": "approve"} if ok else
                        {"type": "reject", "message": (f"Owner replied: {msg.text!r}. Do not place this order. "
                                                       "If they asked for something different, handle that.")})
            agent.invoke(Command(resume={"decisions": [decision] * len(waiting)}), config, version="v2")
        else:
            agent.invoke({"messages": [{"role": "user", "content": msg.text}]}, config, version="v2")
        texts = [m["text"] for m in ctx.outbox if not NON_ANSWERS.match(m["text"] or "")]
        images = [i for m in ctx.outbox for i in m["images"]]
        if ctx.locked_say:   # sensitive + locked: exactly the code prompt, whatever the model wrote
            texts = [ctx.locked_say]
        elif not texts:  # model answered in plain text instead of send_text: guard it too
            final = _final_ai_text(agent, config)
            if final and not NON_ANSWERS.match(final):
                from app.d1.base import get_client
                texts.append(guard_final_text(final, agent.get_state(config).values, d1_client or get_client(),
                                              msg.chat_id))
        still = pending_approval(agent, config)
        if still:  # fixed template of the exact pending action, not model text
            texts.append(_approval_prompt(still))
        images += _sheet_picture(ctx)
    return InboundReply(reply="\n\n".join(texts) if texts else None, images=images)


def _sheet_picture(ctx) -> list[str]:
    """The owner changed something (count, par, order) or asked for the sheet: rewrite the Excel file
    and attach a picture of it. Deterministic: the model doesn't decide whether the sheet updates."""
    if not ctx.sheet_changes:
        return []
    try:
        from app import sheet
        return [sheet.refresh("; ".join(c for c in ctx.sheet_changes if c != "requested") or None)["png"]]
    except Exception:
        import logging
        logging.getLogger(__name__).exception("sheet refresh failed")
        return []


def _final_ai_text(agent, config) -> str | None:
    msgs = current_turn(agent.get_state(config).values.get("messages", []))
    for m in reversed(msgs):
        if isinstance(m, AIMessage) and not m.tool_calls and isinstance(m.content, str) and m.content.strip():
            return m.content.strip()
    return None


def handle_inbound(msg: InboundMessage) -> InboundReply:
    try:  # remember who texted the line: Photon only lets us text chats that texted us first
        from app.ledger import api as ledger
        ledger.remember_chat(msg.chat_id, msg.sender)
    except Exception:
        pass
    agent = get_agent()
    if agent is None:
        return InboundReply(reply=f"(agent not configured: set AGENT_MODEL) echo: {msg.text}")
    gate = _security_gate(agent, msg)
    if gate is not None:
        return gate
    return run_inbound(agent, msg)


def _note(agent, chat_id: str, text: str) -> None:
    """Append a system note to the chat's thread (no model call)."""
    try:
        from langchain_core.messages import HumanMessage
        agent.update_state({"configurable": {"thread_id": chat_id}}, {"messages": [HumanMessage(content=text)]})
    except Exception:
        pass


NON_ANSWERS = re.compile(r"^\W*\(?\s*(no reply|no response|none|n/?a)\s*\)?\W*$", re.I)


def _security_gate(agent, msg: InboundMessage) -> InboundReply | None:
    """Authenticator codes and "lock" are handled here, in code: a code never reaches the model."""
    from app import secure
    if not secure.configured():
        return None
    if secure.looks_like_lock(msg.text):
        secure.lock(msg.chat_id)
        _note(agent, msg.chat_id, "[system: the owner locked bank info. Any balance or statement numbers earlier "
                                  "in this chat are stale and must not be repeated; call get_bank_statement for any "
                                  "money question.]")
        return InboundReply(reply="Locked. Bank info needs a new code next time.")
    if not secure.looks_like_code(msg.text):
        return None
    res = secure.try_code(msg.chat_id, msg.text)
    if res.status == "bad":
        return InboundReply(reply=f"That code didn't match. {res.tries_left} "
                                  f"{'try' if res.tries_left == 1 else 'tries'} left.")
    if res.status == "locked_out":
        return InboundReply(reply=f"Too many wrong codes. Bank info is locked for {res.minutes} minutes.")
    if res.status == "reused":
        return InboundReply(reply="That code was already used. Wait for the next one and send it.")
    head = f"Verified. Unlocked for {res.minutes} minutes (text \"lock\" to lock it now)."
    if not res.question:
        return InboundReply(reply=head)
    rerun = (f"{res.question}\n\n[system: the owner just verified with their authenticator code. Bank info is "
             f"UNLOCKED now: call get_bank_statement again and answer from its result.]")
    out = run_inbound(agent, InboundMessage(chat_id=msg.chat_id, sender=msg.sender, text=rerun, ts=msg.ts))
    return InboundReply(reply=head + ("\n\n" + out.reply if out.reply else ""), images=out.images)


def notify(agent, chat_id: str, instruction: str) -> dict:
    """Proactive run (the agent texts first), e.g. the morning reorder plan. send_text goes straight
    to the bridge. If the agent proposed orders, the owner gets the fixed approval prompt, and their
    "yes" later resumes the paused orders in run_inbound."""
    import httpx

    from app.contracts import SendRequest
    config = {"configurable": {"thread_id": chat_id}}
    with _chat_lock(chat_id), context.run_context(chat_id, inbound=False) as ctx:
        if pending_approval(agent, config):
            return {"chat_id": chat_id, "skipped": "an approval is already waiting"}
        agent.invoke({"messages": [{"role": "user", "content": f"[system task] {instruction}"}]}, config, version="v2")
        still = pending_approval(agent, config)
        extra = _approval_prompt(still) if still else None
        images = _sheet_picture(ctx)
        if extra or images:
            httpx.post(f"{settings.bridge_url}/send", timeout=15, json=SendRequest(
                chat_id=chat_id, text=extra or "Here's the sheet.", images=images).model_dump())
    return {"chat_id": chat_id, "pending_orders": len(still), "approval_prompt": extra}


PLAN_TASK = ("Morning reorder check. Call reorder_plan. If items are below par, send the owner ONE short text "
             "listing each item: on hand vs par, order qty, cost, and the total. Then call place_order once per "
             "item with exactly the plan's order_qty (the owner approves by text). If nothing is below par, send "
             "one short line saying stock is fine and nothing needs ordering.")


def send_reorder_plan(chat_ids: list[str] | None = None) -> list[dict]:
    from app.ledger import api as ledger
    agent = get_agent()
    if agent is None:
        return []
    return [notify(agent, c, PLAN_TASK) for c in (chat_ids or ledger.known_chats())]
