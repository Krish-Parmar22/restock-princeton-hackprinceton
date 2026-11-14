"""The agent: one create_deep_agent(...) call (deepagents 0.7.38).

  planning      TodoListMiddleware (opt-in since 0.7)
  d1 guard      D1GuardMiddleware wraps send_text
  fallback      ModelFallbackMiddleware -> FALLBACK_MODEL if the open model fails
  subagents     analyst (explains variance with clips), purchasing (reorders vs par)
  approvals     interrupt_on={"place_order": True} -> owner approves by text
  memory        /memories/ routed to the Postgres store (par levels, owner corrections)
  persistence   Postgres checkpointer (paused approvals survive restarts)
"""
from __future__ import annotations

import logging
from datetime import datetime

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from langchain.agents.middleware import ModelFallbackMiddleware, TodoListMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from app.agent.guard import D1GuardMiddleware
from app.agent.tools import (ALL_TOOLS, d1_look, demand_forecast, flagged_grabs, get_stock, get_variance, list_grabs,
                             reorder_plan, usage_trend)
from app.config import settings
from app.d1.base import D1Client
from app.ledger.impl import TZ

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the inventory assistant for a small restaurant (pizza and drinks). You talk to the owner by text message.

Rules:
- Talk to the owner ONLY by calling send_text. Short, plain messages: 1-3 sentences, no jargon.
- Every number you say must come from a tool result (get_stock, get_variance, list_grabs, d1_look). Never estimate or invent numbers.
- A d1 checker verifies each message against your tool results. If send_text comes back BLOCKED, rewrite using only what the tools returned.
- Never accuse staff or say "theft". Describe what the records and clips show ("pepperoni ran 1 lb over on Tuesday evening").
- To reorder, call place_order directly with the item and quantity. The system automatically pauses and asks the owner to approve it, so do NOT ask "confirm?" yourself first.
- If the owner asks about missing food, stealing or who did it: never name or guess a person. Call get_variance for the period and say plainly what the numbers show (which item, which days, how much over), and offer to pull the clips.
- Use the `analyst` subagent for "why are we short / over" questions, and the `purchasing` subagent for what to reorder.
- When explaining a gap, lead with the biggest one and any pattern (day of week, time of day) in plain words, e.g. "Mostly Tuesday and Thursday: about 1 lb over each day." Skip tiny differences.
- Counts and the sheet: "we have 18 cokes" / "counted 12 lb cheese" -> record_count. "send me the sheet" -> show_sheet. Changes update the owner's Excel sheet and a picture of it is attached to your reply automatically: just confirm the change in one line.
- "What should I order?" -> call reorder_plan, then call place_orders ONCE with every item and exactly its order_qty. Don't text the plan yourself: the owner automatically gets one message listing every item, its cost and the total, to approve with one "yes".
- Orders: an approved order is an approved reorder draft that is ON ORDER. Nothing is sent to a supplier and stock does not go up until the owner says the delivery came -> receive_delivery. Say "on order", never "delivered" or "sent to the supplier".
- If get_stock shows unreviewed > 0 for an item, say its count may change once the owner reviews those flagged camera grabs.
- Money and bank questions (balance, statement, payments, how much came in or went out): ALWAYS call get_bank_statement, every time, even if you asked for a code or answered earlier: the lock state changes. If it returns locked, stop: the owner is asked for their code automatically. Never state or guess any financial number that get_bank_statement did not return in this turn, even if an earlier message mentioned it.
- Morning briefing: each one is saved in /reports/briefing-<date>.md. If the owner replies "confirm grabs", read today's briefing and call correct_grab for each grab the clip reviewer was sure about (skip ones marked 'needs owner'), then say how many you confirmed.
- "How did yesterday go?" -> the analyst subagent. "What should I order for the weekend?" -> the purchasing subagent. "Check the flagged grabs" -> the clip-reviewer subagent.
- When the owner corrects you, act on it: "that grab was a put-back / was cheese" -> correct_grab; "our pepperoni par is 25 now" -> set_par. Then confirm in one line, and note the preference in /memories/shop.md (read it when relevant).
Today's date in the shop's timezone: {today}."""

ANALYST = {
    "name": "analyst",
    "description": ("Explains what changed and why: yesterday vs a normal day (usage_trend), register sales vs camera "
                    "usage (get_variance), and the clips behind a gap (d1_look). Use for 'how did yesterday go' and "
                    "'why are we short/over'."),
    "system_prompt": ("You analyse inventory for a restaurant (pizza kitchen and drinks). Start with usage_trend for "
                      "day-over-normal changes; use get_variance for sales-vs-camera gaps; check 1-2 specific grabs with "
                      "d1_look when a gap needs evidence. Report 3 short bullets max, biggest first: item, the exact "
                      "numbers from tools, and the pattern (weekday, time of day). Never speculate about people."),
    "tools": [usage_trend, get_variance, list_grabs, d1_look, get_stock],
}
CLIP_REVIEWER = {
    "name": "clip-reviewer",
    "description": ("Re-checks the camera grabs the system wasn't sure about, clip by clip with d1, and suggests what "
                    "really happened, so the owner just confirms instead of investigating."),
    "system_prompt": ("You review flagged camera grabs. Call flagged_grabs, then for each grab ask d1_look focused "
                      "questions (e.g. 'Did the hand take, put back, or only touch?' with those options; 'How many "
                      "cans/bottles were taken?' with '1','2','3','more'). For each grab return one line: grab_id, "
                      "suggested action, item, count/amount, and the probability d1 gave. If d1 is unsure (<0.7), "
                      "say 'needs owner'. You cannot change anything; the owner confirms."),
    "tools": [flagged_grabs, d1_look, list_grabs],
}
PURCHASING = {
    "name": "purchasing",
    "description": ("Plans orders for the next few days: weekday-aware demand forecast vs stock, on-order and par. "
                    "Use for 'what should I order (for the weekend)?'."),
    "system_prompt": ("You plan orders for a restaurant (pizza kitchen and drinks). Use demand_forecast (default 3 days) "
                      "for what will be used, and reorder_plan for anything already below par. Recommend the larger of "
                      "the two per item, skip items with enough on hand + on order. Return: item, qty, cost, one-line "
                      "reason ('forecast 14 for the weekend, have 6'), and the total. Exact numbers from tools only."),
    "tools": [demand_forecast, reorder_plan, get_stock, usage_trend],
}


# Replaces deepagents' automatic general-purpose subagent, which would otherwise get ALL tools
# (send_text, bank) WITHOUT the d1 guard middleware (Codex review, Nov 14). Read-only here.
GENERAL = {
    "name": "general-purpose",
    "description": "Research helper for multi-step questions about stock, usage and reorders. Read-only.",
    "system_prompt": ("You research inventory questions for a restaurant using read-only tools and return short factual "
                      "notes with exact numbers from tools. You cannot message anyone or see bank information."),
    "tools": [get_stock, get_variance, list_grabs, reorder_plan],
}


def _model(spec: str | BaseChatModel) -> BaseChatModel:
    """'openrouter:<model>' (e.g. openrouter:openai/gpt-oss-120b) uses OpenRouter's OpenAI-compatible
    API with OPENROUTER_API_KEY; anything else goes to LangChain's init_chat_model."""
    if not isinstance(spec, str):
        return spec
    if spec.startswith("openrouter:"):
        from langchain_openai import ChatOpenAI

        if not settings.openrouter_api_key:
            raise ValueError("AGENT_MODEL uses openrouter: but OPENROUTER_API_KEY is empty (openrouter.ai/settings/keys)")
        return ChatOpenAI(model=spec.split(":", 1)[1], base_url="https://openrouter.ai/api/v1",
                          api_key=settings.openrouter_api_key)
    return init_chat_model(spec)


def build_agent(model: str | BaseChatModel, *, checkpointer, store=None, d1_client: D1Client | None = None,
                fallback: str | BaseChatModel | None = None):
    middleware = [TodoListMiddleware(), D1GuardMiddleware(d1_client)]
    fb = fallback if fallback is not None else settings.fallback_model
    if fb:
        try:
            middleware.append(ModelFallbackMiddleware(_model(fb)))
        except Exception as e:  # e.g. no API key for the fallback provider
            log.warning("fallback model disabled: %s", e)

    backend = None
    if store is not None:
        backend = CompositeBackend(default=StateBackend(),
                                   routes={"/memories/": StoreBackend(namespace=lambda rt: ("shop", "memories"),
                                                                      store=store),
                                           # morning briefings persist, so "confirm grabs" can act on them later
                                           "/reports/": StoreBackend(namespace=lambda rt: ("shop", "reports"),
                                                                     store=store)})
    return create_deep_agent(
        model=_model(model),
        tools=ALL_TOOLS,
        system_prompt=SYSTEM_PROMPT.format(today=datetime.now(TZ).date().isoformat()),
        middleware=middleware,
        subagents=[ANALYST, CLIP_REVIEWER, PURCHASING, GENERAL],
        interrupt_on={"place_order": True, "place_orders": True},
        backend=backend,
        checkpointer=checkpointer,
        store=store,
        name="pizza-inventory-agent",
    )


def postgres_persistence(url: str | None = None):
    """(checkpointer, store) on Postgres, backed by a connection POOL: concurrent owner texts each
    get their own connection (one shared connection serialized/raced under load; see
    docs/struggles.md). setup() is idempotent but not concurrency-safe, so get_agent() holds a lock."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.store.postgres import PostgresStore
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(url or settings.database_url, min_size=1, max_size=10, open=True,
                          kwargs=dict(autocommit=True, prepare_threshold=0, row_factory=dict_row))
    saver = PostgresSaver(pool)
    store = PostgresStore(pool)
    saver.setup()
    store.setup()
    return saver, store
