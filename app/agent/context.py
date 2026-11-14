"""Per-run context: which chat we're talking to and what we've queued to send.

During an inbound message, send_text queues into `outbox` and /inbound returns it as
the reply. With no inbound (proactive alerts), send_text POSTs to Arnav's bridge.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class RunContext:
    chat_id: str | None
    inbound: bool                     # True: collect replies; False: push via bridge
    outbox: list[dict] = field(default_factory=list)  # [{"text":..., "images": [...]}]


_CTX: ContextVar[RunContext | None] = ContextVar("run_ctx", default=None)


def current() -> RunContext | None:
    return _CTX.get()


@contextmanager
def run_context(chat_id: str | None, inbound: bool) -> Iterator[RunContext]:
    ctx = RunContext(chat_id=chat_id, inbound=inbound)
    token = _CTX.set(ctx)
    try:
        yield ctx
    finally:
        _CTX.reset(token)
