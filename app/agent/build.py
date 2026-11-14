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
from app.agent.tools import ALL_TOOLS, d1_look, get_stock, get_variance, list_grabs
from app.config import settings
from app.d1.base import D1Client
from app.ledger.impl import TZ

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the inventory assistant for a small pizza shop. You talk to the owner by text message.

Rules:
- Talk to the owner ONLY by calling send_text. Short, plain messages: 1-3 sentences, no jargon.
- Every number you say must come from a tool result (get_stock, get_variance, list_grabs, d1_look). Never estimate or invent numbers.
- A d1 checker verifies each message against your tool results. If send_text comes back BLOCKED, rewrite using only what the tools returned.
- Never accuse staff or say "theft". Describe what the records and clips show ("pepperoni ran 1 lb over on Tuesday evening").
- To reorder, call place_order. The owner approves it by text before it goes through. Tell them what you're ordering and why first.
- Use the `analyst` subagent for "why are we short / over" questions, and the `purchasing` subagent for what to reorder.
- Remember the owner's preferences and corrections (par levels, supplier, "that grab was a put-back") in /memories/shop.md. Read it when relevant.
Today's date in the shop's timezone: {today}."""

ANALYST = {
    "name": "analyst",
    "description": "Explains why an ingredient is over or under: compares register sales to camera usage by day and checks grab clips with d1.",
    "system_prompt": ("You investigate inventory variance for a pizza shop. Use get_variance day by day to find when the gap "
                      "happens, list_grabs to see the camera events, and d1_look to check specific grabs. Report findings "
                      "as short factual bullet points with the exact numbers from tools. Never speculate about people."),
    "tools": [get_variance, list_grabs, d1_look, get_stock],
}
PURCHASING = {
    "name": "purchasing",
    "description": "Works out what to reorder: compares on-hand stock to par levels and recent daily usage.",
    "system_prompt": ("You plan reorders for a pizza shop. Use get_stock and get_variance to find items below par and "
                      "estimate days of stock left from recent usage. Return a short list: item, on hand, par, suggested qty."),
    "tools": [get_stock, get_variance],
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
                                                                      store=store)})
    return create_deep_agent(
        model=_model(model),
        tools=ALL_TOOLS,
        system_prompt=SYSTEM_PROMPT.format(today=datetime.now(TZ).date().isoformat()),
        middleware=middleware,
        subagents=[ANALYST, PURCHASING],
        interrupt_on={"place_order": True},
        backend=backend,
        checkpointer=checkpointer,
        store=store,
        name="pizza-inventory-agent",
    )


def postgres_persistence(url: str | None = None):
    """(checkpointer, store) on Postgres. Connections stay open for the process lifetime."""
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.store.postgres import PostgresStore
    from psycopg.rows import dict_row

    kw = dict(autocommit=True, prepare_threshold=0, row_factory=dict_row)
    url = url or settings.database_url
    saver = PostgresSaver(psycopg.connect(url, **kw))
    store = PostgresStore(psycopg.connect(url, **kw))
    saver.setup()
    store.setup()
    return saver, store
