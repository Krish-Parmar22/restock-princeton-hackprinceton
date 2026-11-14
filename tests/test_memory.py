"""Long-term memory: /memories/ is routed to the Postgres store, so a note written in one chat
thread is readable from a brand-new thread. Real create_deep_agent graph + real Postgres
checkpointer/store (throwaway DB pizza_test_memory), scripted fake model: no paid APIs."""
from __future__ import annotations

import psycopg
import pytest
from langchain_core.messages import ToolMessage

from app.agent.build import build_agent, postgres_persistence
from tests.test_agent import KeywordD1, ScriptedModel, ai

ADMIN = "postgresql://localhost:5432/postgres"
URL = "postgresql://localhost:5432/pizza_test_memory"
NOTE = "Owner prefers pepperoni par 25. Deliveries come Monday and Thursday."


@pytest.fixture()
def persistence():
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS pizza_test_memory WITH (FORCE)")
        c.execute("CREATE DATABASE pizza_test_memory")
    saver, store = postgres_persistence(URL)
    yield saver, store
    saver.conn.close()   # the pool shared by saver and store


def test_memory_file_persists_across_threads(persistence):
    saver, store = persistence
    model = ScriptedModel(messages=iter([
        ai(write_file={"file_path": "/memories/shop.md", "content": NOTE}),   # thread A
        ai("noted"),
        ai(read_file={"file_path": "/memories/shop.md"}),                    # thread B (new)
        ai("ok"),
    ]))
    agent = build_agent(model, checkpointer=saver, store=store, d1_client=KeywordD1(), fallback="")

    agent.invoke({"messages": [{"role": "user", "content": "remember our pepperoni par is 25"}]},
                 {"configurable": {"thread_id": "mem-thread-a"}})

    # landed in the Postgres store (CompositeBackend strips the /memories prefix from the key)
    item = store.get(("shop", "memories"), "/shop.md")
    assert item is not None and NOTE in str(item.value)

    out = agent.invoke({"messages": [{"role": "user", "content": "what do you know about the shop?"}]},
                       {"configurable": {"thread_id": "mem-thread-b"}})
    reads = [m for m in out["messages"] if isinstance(m, ToolMessage) and m.name == "read_file"]
    assert reads and NOTE in str(reads[-1].content)
    # thread B's own state never saw the write: the content came from the store, not the thread
    assert not any(isinstance(m, ToolMessage) and m.name == "write_file" for m in out["messages"])
