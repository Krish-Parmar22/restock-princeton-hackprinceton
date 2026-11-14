"""API on :8000. Contract: docs/arnav-shah.md.

Run: uv run uvicorn app.api.server:app --port 8000 --reload

Routers:
  app/api/data.py  dashboard/Excel data endpoints (ledger workstream)
  app/api/chat.py  POST /inbound for the Photon bridge (agent workstream)
  app/api/upload.py POST /api/grabs/upload for the Pi (docs/handoff.md)
Both start as stubs with fake data so Arnav is never blocked.
"""
from fastapi import FastAPI

from app.api import chat, data, upload

app = FastAPI(title="Pizza inventory agent API")
app.include_router(data.router)
app.include_router(chat.router)
app.include_router(upload.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
