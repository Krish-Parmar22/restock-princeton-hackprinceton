"""POST /inbound for Photon bridge (contract: app/contracts.py).
Runs the deepagents agent; echoes if AGENT_MODEL isn't set, so the bridge always works."""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.agent.runtime import handle_inbound
from app.contracts import InboundMessage, InboundReply

router = APIRouter()
log = logging.getLogger(__name__)


@router.post("/inbound", response_model=InboundReply)
def inbound(msg: InboundMessage) -> InboundReply:
    try:
        return handle_inbound(msg)
    except Exception:
        log.exception("agent failed on inbound from %s", msg.chat_id)
        return InboundReply(reply="Sorry, I hit a snag checking that. Try again in a minute.")


@router.get("/api/agent/trace")
def agent_trace(chat_id: str | None = None) -> dict:
    """The agent's latest run for a chat, step by step, read from the LangGraph checkpoint:
    subagents delegated (and in parallel), tools called, files written, the d1 guard's verdicts,
    and whether it's paused waiting for the owner's approval. For the dashboard's 'Under the hood'."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from app.agent.runtime import get_agent, pending_approval
    from app.ledger import api as ledger
    agent = get_agent()
    chat = chat_id or next((c for c in ledger.known_chats() if c.startswith("pho_")), None)
    if agent is None or not chat:
        return {"chat_id": chat, "steps": []}
    config = {"configurable": {"thread_id": chat}}
    msgs = agent.get_state(config).values.get("messages", [])
    start = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)
                 and not str(m.content).startswith("[system: the owner")), default=0)
    steps: list[dict] = []
    task_names: dict[str, str] = {}
    for m in msgs[start:]:
        if isinstance(m, HumanMessage):
            t = str(m.content)
            steps.append({"kind": "trigger", "text": ("Morning briefing (scheduled)" if "[morning briefing" in t
                                                      else "Owner texted: " + t[:160])})
        elif isinstance(m, AIMessage):
            calls = m.tool_calls or []
            subs = [c for c in calls if c["name"] == "task"]
            if len(subs) > 1:
                steps.append({"kind": "parallel", "text": f"{len(subs)} subagents in parallel"})
            for c in calls:
                a = c.get("args", {}) or {}
                if c["name"] == "task":
                    task_names[c.get("id", "")] = a.get("subagent_type", "subagent")
                    steps.append({"kind": "subagent", "name": a.get("subagent_type", "subagent"),
                                  "text": str(a.get("description", ""))[:220]})
                elif c["name"] in ("write_file", "read_file", "edit_file", "ls"):
                    steps.append({"kind": "file", "name": c["name"], "text": str(a.get("file_path") or a.get("path") or "")})
                elif c["name"] == "send_text":
                    steps.append({"kind": "draft", "text": str(a.get("message", ""))[:400]})
                elif c["name"] in ("place_order", "place_orders"):
                    steps.append({"kind": "approval", "name": c["name"], "text": str(a.get("orders") or a)[:200]})
                elif c["name"] == "write_todos":
                    steps.append({"kind": "plan", "text": "; ".join(t.get("content", "") for t in a.get("todos", []))[:300]})
                else:
                    steps.append({"kind": "tool", "name": c["name"], "text": str(a)[:160]})
            if not calls and isinstance(m.content, str) and m.content.strip():
                steps.append({"kind": "reply", "text": m.content.strip()[:400]})
        elif isinstance(m, ToolMessage):
            body = str(m.content)
            if m.name == "task":
                steps.append({"kind": "subagent_result", "name": task_names.get(m.tool_call_id, "subagent"),
                              "text": body[:500]})
            elif m.name == "send_text":
                steps.append({"kind": "guard", "ok": not body.startswith("BLOCKED"),
                              "text": "d1 guard: verified, sent" if not body.startswith("BLOCKED") else body[:300]})
    if pending_approval(agent, config):
        steps.append({"kind": "paused", "text": "Paused (human-in-the-loop): waiting for the owner's 'yes' by text"})
    return {"chat_id": chat, "steps": steps}
