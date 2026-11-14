"""Talk to the agent from the terminal, the same way the Photon bridge does.

Usage: uv run python scripts/chat_cli.py [--chat demo-owner]
Needs AGENT_MODEL in .env (otherwise you get the echo fallback).
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.runtime import handle_inbound  # noqa: E402
from app.contracts import InboundMessage  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--chat", default="cli-owner")
a = ap.parse_args()
print("Type a message to the shop agent (Ctrl-D to quit).")
for line in sys.stdin:
    text = line.strip()
    if not text:
        continue
    out = handle_inbound(InboundMessage(chat_id=a.chat, sender="cli", text=text, ts=datetime.now(timezone.utc)))
    print(f"agent> {out.reply}" + (f"  [images: {out.images}]" if out.images else ""))
