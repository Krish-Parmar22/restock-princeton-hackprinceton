"""API on :8000. Contract: docs/arnav-shah.md.

Run: uv run uvicorn app.api.server:app --port 8000 --reload

Routers:
  app/api/data.py  dashboard/Excel data endpoints (ledger workstream)
  app/api/chat.py  POST /inbound for the Photon bridge (agent workstream)
  app/api/upload.py POST /api/grabs/upload for the Pi (docs/handoff.md)
Both start as stubs with fake data so Arnav is never blocked.
"""
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import chat, data, upload
from app.config import settings

log = logging.getLogger("api")
WATCHER: dict = {}  # {"thread":..., "watcher":...} when running in-process


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """One command runs everything: the API and (WATCHER_IN_SERVER) the grab watcher thread."""
    if settings.watcher_in_server:
        from app.grabs.watcher import build_watcher

        w = build_watcher()
        t = threading.Thread(target=w.run_forever, name="grab-watcher", daemon=True)
        t.start()
        WATCHER.update(thread=t, watcher=w)
        log.info("grab watcher running on %s", w.dir)
    yield


app = FastAPI(title="Pizza inventory agent API", lifespan=lifespan)
app.include_router(data.router)
app.include_router(chat.router)
app.include_router(upload.router)


@app.get("/health")
def health() -> dict:
    w = WATCHER.get("watcher")
    return {"ok": True,
            "watcher": None if w is None else {"alive": WATCHER["thread"].is_alive(), "dir": str(w.dir),
                                               "processed": len(w.processed), "failing": len(w.failures)}}
