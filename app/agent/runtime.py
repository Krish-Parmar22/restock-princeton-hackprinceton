"""Running the agent for the Photon bridge.

handle_inbound: one owner text -> agent run (thread = chat_id) -> the guarded send_text
messages become the reply. If an order is waiting for approval, a yes/no reply resumes it.
notify: proactive message (e.g. daily summary); send_text POSTs to the bridge.
"""
from __future__ import annotations

import re
from functools import lru_cache

from langgraph.types import Command

from app.agent import context
from app.agent.build import build_agent, postgres_persistence
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
    lines = []
    for r in reqs:
        a = r.get("args", {})
        lines.append(f"{a.get('qty')} {a.get('item')}" if a else r.get("name", "this"))
    return "Approve this order: " + ", ".join(lines) + "? Reply yes or no."


def run_inbound(agent, msg: InboundMessage) -> InboundReply:
    config = {"configurable": {"thread_id": msg.chat_id}}
    with context.run_context(msg.chat_id, inbound=True) as ctx:
        waiting = pending_approval(agent, config)
        if waiting:
            ok = yes_no(msg.text)
            decision = ({"type": "approve"} if ok else
                        {"type": "reject", "message": f"Owner replied: {msg.text!r}. Do not place it."})
            agent.invoke(Command(resume={"decisions": [decision] * len(waiting)}), config, version="v2")
        else:
            agent.invoke({"messages": [{"role": "user", "content": msg.text}]}, config, version="v2")
        texts = [m["text"] for m in ctx.outbox]
        images = [i for m in ctx.outbox for i in m["images"]]
        still = pending_approval(agent, config)
        if still:
            texts.append(_approval_prompt(still))
    return InboundReply(reply="\n\n".join(texts) if texts else None, images=images)


def handle_inbound(msg: InboundMessage) -> InboundReply:
    agent = get_agent()
    if agent is None:
        return InboundReply(reply=f"(agent not configured: set AGENT_MODEL) echo: {msg.text}")
    return run_inbound(agent, msg)


def notify(agent, chat_id: str, instruction: str) -> None:
    """Proactive run: e.g. notify(agent, chat, 'Send the owner today's summary.')."""
    config = {"configurable": {"thread_id": chat_id}}
    with context.run_context(chat_id, inbound=False):
        agent.invoke({"messages": [{"role": "user", "content": f"[system task] {instruction}"}]}, config, version="v2")
