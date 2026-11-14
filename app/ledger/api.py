"""Ledger interface: the ONLY way the API and the agent read/write inventory data.

Signatures are the contract between workstreams. The ledger workstream implements them
in app/ledger/impl.py. Everything here returns contract models from app/contracts.py
or plain dicts, and all numbers are computed in SQL/Python, never by an LLM.
"""
from __future__ import annotations

from datetime import date, datetime

from app.contracts import GrabConfirm, GrabRow, InventoryRow, MessageRow, VarianceRow


def get_inventory(at: datetime | None = None) -> list[InventoryRow]:
    """On-hand stock per item = latest count + purchases since - camera usage since."""
    from app.ledger import impl
    return impl.get_inventory(at)


def get_variance(start: date, end: date) -> list[VarianceRow]:
    """Theoretical (POS x recipes) vs actual (camera grabs) per item, in qty and $."""
    from app.ledger import impl
    return impl.get_variance(start, end)


def list_grabs(day: date | None = None, status: str = "all") -> list[GrabRow]:
    from app.ledger import impl
    return impl.list_grabs(day, status)


def confirm_grab(grab_id: str, body: GrabConfirm) -> GrabRow:
    from app.ledger import impl
    return impl.confirm_grab(grab_id, body)


def record_grab(row: dict, d1_answers: list[dict]) -> None:
    """Insert one grab_events row plus its raw d1_answers. Idempotent on grab_id.
    row keys: grab_id, zone, camera_id, source, t_start, t_end, item (name or None), action,
    qty_label, confidence, status, frames."""
    from app.ledger import impl
    impl.record_grab(row, d1_answers)


def draft_order(item: str, qty: float, chat_id: str | None = None, supplier: str | None = None) -> int:
    """Create a draft order and return its id."""
    from app.ledger import impl
    return impl.draft_order(item, qty, chat_id, supplier)


def set_order_status(order_id: int, status: str) -> None:
    """'approved' also records a purchase (source='agent_order')."""
    from app.ledger import impl
    impl.set_order_status(order_id, status)


def record_message(chat_id: str | None, text: str, d1_score: float | None, decision: str, claims: list[dict]) -> None:
    from app.ledger import impl
    impl.record_message(chat_id, text, d1_score, decision, claims)


def list_messages(limit: int = 50) -> list[MessageRow]:
    from app.ledger import impl
    return impl.list_messages(limit)


def set_par(item: str, par: float) -> dict:
    """Owner correction: change an item's par level. Returns {item, old_par, new_par}."""
    from app.ledger import impl
    return impl.set_par(item, par)


def remember_chat(chat_id: str, sender: str | None) -> None:
    from app.ledger import impl
    impl.remember_chat(chat_id, sender)


def known_chats() -> list[str]:
    from app.ledger import impl
    return impl.known_chats()


def recently_alerted(kind: str, item: str, hours: float = 12, chat_id: str | None = None) -> bool:
    from app.ledger import impl
    return impl.recently_alerted(kind, item, hours, chat_id)


def record_alert(kind: str, item: str, chat_id: str, text: str) -> None:
    from app.ledger import impl
    impl.record_alert(kind, item, chat_id, text)


def record_count(item: str, qty: float, source: str = "owner_text") -> dict:
    from app.ledger import impl
    return impl.record_count(item, qty, source)


def list_orders(limit: int = 20) -> list[dict]:
    from app.ledger import impl
    return impl.list_orders(limit)


def reorder_plan() -> list[dict]:
    from app.ledger import impl
    return impl.reorder_plan()


def receive_orders(item: str | None = None) -> list[dict]:
    from app.ledger import impl
    return impl.receive_orders(item)
