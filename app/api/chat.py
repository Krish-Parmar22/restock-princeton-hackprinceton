"""POST /inbound for Arnav's Photon bridge (contract: docs/arnav-shah.md).
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
