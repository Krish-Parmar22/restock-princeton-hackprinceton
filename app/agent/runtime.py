"""Running the agent for the Photon bridge.

handle_inbound: one owner text -> agent run (thread = chat_id) -> the guarded send_text
messages become the reply. If an order is waiting for approval, a yes/no reply resumes it.
notify: proactive message (e.g. daily summary); send_text POSTs to the bridge.
"""
from __future__ import annotations

import re
import threading
from collections import defaultdict
from functools import lru_cache

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


@lru_cache(maxsize=1)
def get_agent():
    """The production agent, or None when no model is configured."""
    if not settings.agent_model:
        return None
    saver, store = postgres_persistence()
    return build_agent(settings.agent_model, checkpointer=saver, store=store)


def pending_approval(agent, config) -> list[dict]:
    """Pending place_order action requests on this thread ([] if none)."""
    snap = agent.get_state(config)
    out = []
    for intr in getattr(snap, "interrupts", ()) or ():
        val = intr.value if isinstance(intr.value, dict) else {}
        out.extend(val.get("action_requests", []))
    return out


def _approval_prompt(reqs: list[dict]) -> str:
    from app.ledger import api as ledger
    try:
        units = {r.item: r.unit for r in ledger.get_inventory()}
    except Exception:
        units = {}
    lines = []
    for r in reqs:
        a = r.get("args", {})
        if a:
            qty = a.get("qty")
            qty = int(qty) if isinstance(qty, float) and qty.is_integer() else qty
            unit = units.get(str(a.get("item", "")).lower(), "")
            lines.append(f"{qty} {unit} of {a.get('item')}".replace("  ", " "))
        else:
            lines.append(r.get("name", "this"))
    return "Approve this order: " + ", ".join(lines) + "? Reply yes or no."


def run_inbound(agent, msg: InboundMessage, d1_client=None) -> InboundReply:
    config = {"configurable": {"thread_id": msg.chat_id}}
    with _chat_lock(msg.chat_id), context.run_context(msg.chat_id, inbound=True) as ctx:
        waiting = pending_approval(agent, config)
        if waiting:
            ok = approval_answer(msg.text)
            decision = ({"type": "approve"} if ok else
                        {"type": "reject", "message": (f"Owner replied: {msg.text!r}. Do not place this order. "
                                                       "If they asked for something different, handle that.")})
            agent.invoke(Command(resume={"decisions": [decision] * len(waiting)}), config, version="v2")
        else:
            agent.invoke({"messages": [{"role": "user", "content": msg.text}]}, config, version="v2")
        texts = [m["text"] for m in ctx.outbox]
        images = [i for m in ctx.outbox for i in m["images"]]
        if not texts:  # model answered in plain text instead of send_text: guard it too
            final = _final_ai_text(agent, config)
            if final:
                from app.d1.base import get_client
                texts.append(guard_final_text(final, agent.get_state(config).values, d1_client or get_client(),
                                              msg.chat_id))
        still = pending_approval(agent, config)
        if still:  # fixed template of the exact pending action, not model text
            texts.append(_approval_prompt(still))
    return InboundReply(reply="\n\n".join(texts) if texts else None, images=images)


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
    return run_inbound(agent, msg)


def notify(agent, chat_id: str, instruction: str) -> None:
    """Proactive run: e.g. notify(agent, chat, 'Send the owner today's summary.')."""
    config = {"configurable": {"thread_id": chat_id}}
    with context.run_context(chat_id, inbound=False):
        agent.invoke({"messages": [{"role": "user", "content": f"[system task] {instruction}"}]}, config, version="v2")
