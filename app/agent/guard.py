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
from langchain_core.messages import HumanMessage, ToolMessage

from app.agent import context
from app.config import settings
from app.d1.base import D1Client, D1Question, get_client
from app.ledger import api as ledger

GUARDED_TOOL = "send_text"
MAX_BLOCKS = 2
BLOCK_TAG = "BLOCKED by d1 guard"
FALLBACK = "I couldn't confirm that from the records yet. Want me to pull the clips and check?"
SOFTEN_PREFIX = "Looks like: "


ACCUSE = re.compile(r"\b(steal|stole|stolen|theft|thief|thieves|took home|pocket|skim|fraud|lying|lied)", re.I)


def split_claims(text: str) -> list[str]:
    """Sentences d1 must check. Skips plain pleasantries and neutral questions, but keeps any
    sentence (question or not) with a number or an accusation. Fails closed: a substantive
    message with no extracted claims is checked as a whole."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    claims, neutral_questions = [], 0
    for p in (s.strip() for s in parts):
        if not p:
            continue
        risky = bool(re.search(r"\d", p) or ACCUSE.search(p))
        if p.endswith("?") and not risky:
            neutral_questions += 1
            continue
        if not risky and len(p.split()) < 4:
            continue
        claims.append(p)
    if not claims and not neutral_questions and len(text.split()) >= 4:
        claims = [text.strip()]
    return claims


def current_turn(messages: list) -> list:
    """Messages since the owner's latest message: evidence and block counts never come from old turns."""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return messages[i + 1:]
    return messages


def _evidence(state: dict) -> str:
    tool_results = [
        f"[{m.name}] {str(m.content)[:4000]}" for m in current_turn(state.get("messages", []))
        if isinstance(m, ToolMessage) and m.name != GUARDED_TOOL
    ]
    try:
        snapshot = json.dumps([r.model_dump() for r in ledger.get_inventory()], default=str)
    except Exception:  # ledger down: judge on tool results alone
        snapshot = "unavailable"
    try:  # so "the records are balanced" can be checked against the real gaps
        from datetime import datetime, timedelta
        from app.ledger.impl import TZ
        end = datetime.now(TZ).date()
        week = [{k: v for k, v in r.model_dump().items() if k != "grab_ids"}
                for r in ledger.get_variance(end - timedelta(days=6), end)]
        variance = json.dumps(week, default=str)
    except Exception:
        variance = "unavailable"
    return ("Tool results from this conversation:\n" + "\n".join(tool_results[-20:]) +
            f"\n\nCurrent inventory snapshot:\n{snapshot}" +
            f"\n\nLast 7 days, should-have-used vs actually-used per item:\n{variance}")


_DASH = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_DATES = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*\d{1,2}(?:st|nd|rd|th)?"
    r"(?:\s*(?:[" + _DASH + r"]|to|through|thru)\s*\d{1,2}(?:st|nd|rd|th)?)?\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b"
    r"|#\s*\d+|\border\s*#?\s*\d+|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}\s*[-\u2013]\s*\d{1,2}\s*(?:am|pm)\b",
    re.I)
_IDS = re.compile(r"\b[\w-]*[a-z][\w-]*\d[\w-]*\b|\b\d[\w-]*[a-z][\w-]*\b", re.I)  # syn-00844_pepperoni etc.
_NUM = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?(?!\w|\.\d)")
NUM_TRUST_FLOOR = 0.25  # verified numbers + d1 not strongly against -> treat as supported


def _numbers(text: str) -> list[float]:
    text = _DATES.sub(" ", re.sub(r"\s+", " ", text))
    text = _IDS.sub(" ", text)
    return [float(n.replace(",", "")) for n in _NUM.findall(text)]


def numbers_check(claim: str, evidence: str) -> bool | str | None:
    """Deterministic: every number in the claim must appear in the evidence (to rounding).
    None when the claim has no numbers. d1 is weak at arithmetic ($92.7 vs $9.27 scored 0.899 in
    calibration, docs/guard-calibration.md), so numbers are never left to d1 alone."""
    nums = _numbers(claim)
    if not nums:
        return None
    pool = set(_numbers(evidence)) | {float(n) for n in _NUM.findall(_IDS.sub(" ", evidence))}

    def close(n: float, e: float) -> bool:
        return abs(n - e) <= max(0.011, 0.005 * abs(e)) or (n == int(n) and abs(n - round(e)) < 1e-9)

    # One step of arithmetic, but only NAMED, meaningful differences inside one record:
    # par - on_hand ("8.75 lb below par", blocked live Nov 14 without this) and actual - theoretical.
    # Any-pair differences let "$8.75 per lb" through (Codex, Nov 14); cross-record sums let "$92.7".
    derived = set()
    for rec in re.findall(r"\{[^{}]*\}", evidence):
        try:
            obj = json.loads(rec)
        except ValueError:
            continue
        for a, b in (("par", "on_hand"), ("actual", "theoretical")):
            if isinstance(obj.get(a), (int, float)) and isinstance(obj.get(b), (int, float)):
                derived.add(abs(obj[a] - obj[b]))

    direct = [any(close(n, e) for e in pool) for n in nums]
    if all(direct):
        return True
    if all(d or any(close(n, x) for x in derived) for n, d in zip(nums, direct)):
        return "derived"   # plausible arithmetic: not blocked, but not boosted either (d1 judges meaning)
    return False


def score_message(text: str, state: dict, client: D1Client) -> tuple[float, list[dict]]:
    from app.secure import CODE_PROMPT
    if text.strip() == CODE_PROMPT:   # the fixed security prompt states no facts
        return 1.0, []
    claims = split_claims(text)
    if not claims:
        return 1.0, []
    qs = [D1Question(
        id=f"claim_{i}", type="noul",
        instructions=("Using ONLY the evidence in `state`, is everything this statement says or implies supported? "
                      "Judge factual claims about stock, numbers, dates, events or people: numbers must match the "
                      "evidence, and any suggestion that a person did something wrong needs direct evidence. "
                      "Offers, questions, and statements about what the assistant will do next are fine. "
                      f"Statement: {c}"))
        for i, c in enumerate(claims)]
    evidence = _evidence(state)
    answers = client.ask(qs, evidence)
    scored = []
    for c, q in zip(claims, qs):
        d1_score = answers[q.id].p_yes
        nums = numbers_check(c, evidence)
        if nums is False:
            final = 0.0                       # a number not in the evidence: block, whatever d1 says
        elif nums is True and d1_score >= NUM_TRUST_FLOOR:
            final = max(d1_score, 0.9)        # every number appears in the evidence, d1 not strongly against
        else:
            final = d1_score
        scored.append({"claim": c, "score": round(final, 4), "d1": round(d1_score, 4), "numbers_ok": nums})
    return min(s["score"] for s in scored), scored


def _prior_blocks(state: dict) -> int:
    return sum(1 for m in current_turn(state.get("messages", []))
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


def guard_final_text(text: str, state: dict, client: D1Client, chat_id: str | None) -> str:
    """Guard a plain-text model reply (no send_text call). Returns what may be sent."""
    score, claims = score_message(text, state, client)
    if score >= settings.guard_send_min:
        ledger.record_message(chat_id, text, score, "sent", claims)
        return text
    if score >= settings.guard_soften_min:
        soft = SOFTEN_PREFIX + text
        ledger.record_message(chat_id, soft, score, "softened", claims)
        return soft
    ledger.record_message(chat_id, text, score, "blocked", claims)
    ledger.record_message(chat_id, FALLBACK, None, "sent", [])
    return FALLBACK
