"""D1GuardMiddleware: d1 fact-checks every message before it reaches the owner.

Lives inside the deepagents harness (passed to create_deep_agent(middleware=[...])) and
wraps the `send_text` tool call:
  1. split the message into factual claims
  2. evidence = this run's tool results (+ a fresh ledger snapshot)
  3. one d1 yes/no question per claim: "is this supported by the evidence?"
  4. score = lowest claim probability
       >= GUARD_SEND_MIN   -> send
       >= GUARD_SOFTEN_MIN -> send, prefixed "Looks like: "
       else                -> block; the agent gets an error and must rewrite
After MAX_BLOCKS blocks in one run, send a safe fallback instead of looping.
Every decision is recorded (ledger.record_message) for the dashboard's trust panel.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from app.agent import context
from app.config import settings
from app.d1.base import D1Client, D1Question, get_client
from app.ledger import api as ledger

GUARDED_TOOL = "send_text"
MAX_BLOCKS = 2
BLOCK_TAG = "BLOCKED by d1 guard"
FALLBACK = "I couldn't confirm that from the records yet. Want me to pull the clips and check?"
SOFTEN_PREFIX = "Looks like: "


def split_claims(text: str) -> list[str]:
    """Factual sentences only: drop questions, greetings and short filler."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    claims = []
    for p in (s.strip() for s in parts):
        if not p or p.endswith("?"):
            continue
        if len(p.split()) < 4 and not re.search(r"\d", p):
            continue
        claims.append(p)
    return claims


def _evidence(state: dict) -> str:
    tool_results = [
        f"[{m.name}] {m.content}" for m in state.get("messages", [])
        if isinstance(m, ToolMessage) and m.name != GUARDED_TOOL
    ]
    try:
        snapshot = json.dumps([r.model_dump() for r in ledger.get_inventory()], default=str)
    except Exception:  # ledger down: judge on tool results alone
        snapshot = "unavailable"
    return ("Tool results from this conversation:\n" + "\n".join(tool_results[-12:]) +
            f"\n\nCurrent inventory snapshot:\n{snapshot}")


def score_message(text: str, state: dict, client: D1Client) -> tuple[float, list[dict]]:
    claims = split_claims(text)
    if not claims:
        return 1.0, []
    qs = [D1Question(
        id=f"claim_{i}", type="noul",
        instructions=("Using ONLY the evidence in `state`, is this statement supported? "
                      "Numbers must match the evidence; claims about causes or people need direct evidence. "
                      f"Statement: {c}"))
        for i, c in enumerate(claims)]
    answers = client.ask(qs, _evidence(state))
    scored = [{"claim": c, "score": round(answers[q.id].p_yes, 4)} for c, q in zip(claims, qs)]
    return min(s["score"] for s in scored), scored


def _prior_blocks(state: dict) -> int:
    return sum(1 for m in state.get("messages", [])
               if isinstance(m, ToolMessage) and m.name == GUARDED_TOOL and BLOCK_TAG in str(m.content))


class D1GuardMiddleware(AgentMiddleware):
    """Gate `send_text` on d1's judgment of each claim."""

    def __init__(self, client: D1Client | None = None):
        super().__init__()
        self._client = client

    @property
    def client(self) -> D1Client:
        return self._client or get_client()

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        call = request.tool_call
        if call["name"] != GUARDED_TOOL:
            return handler(request)
        ctx = context.current()
        chat_id = ctx.chat_id if ctx else None
        text = str(call["args"].get("message", ""))
        score, claims = score_message(text, request.state, self.client)

        if score >= settings.guard_send_min:
            ledger.record_message(chat_id, text, score, "sent", claims)
            return handler(request)

        if score >= settings.guard_soften_min:
            soft = text if text.startswith(SOFTEN_PREFIX) else SOFTEN_PREFIX + text
            ledger.record_message(chat_id, soft, score, "softened", claims)
            return handler(request.override(tool_call={**call, "args": {**call["args"], "message": soft}}))

        ledger.record_message(chat_id, text, score, "blocked", claims)
        if _prior_blocks(request.state) + 1 >= MAX_BLOCKS:
            ledger.record_message(chat_id, FALLBACK, None, "sent", [])
            return handler(request.override(tool_call={**call, "args": {**call["args"], "message": FALLBACK}}))
        worst = min(claims, key=lambda c: c["score"])
        return ToolMessage(
            content=(f"{BLOCK_TAG} (score {score:.2f}). Unsupported claim: \"{worst['claim']}\". "
                     "Rewrite using only facts and numbers from tool results, then call send_text again."),
            tool_call_id=call["id"], name=GUARDED_TOOL, status="error")
