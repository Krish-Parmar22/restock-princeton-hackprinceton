"""POST /inbound for Arnav's Photon bridge. STUB: echoes until the agent is wired.
The agent workstream replaces handle_inbound with a call into the deepagents agent."""
from __future__ import annotations

from fastapi import APIRouter

from app.contracts import InboundMessage, InboundReply

router = APIRouter()


@router.post("/inbound", response_model=InboundReply)
def inbound(msg: InboundMessage) -> InboundReply:
    return InboundReply(reply=f"echo: {msg.text}")
