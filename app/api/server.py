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
PLAN: dict = {}     # {"thread":..., "job":...} when PLAN_TIME is set


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """One command runs everything: the API and (WATCHER_IN_SERVER) the grab watcher thread."""
    if settings.agent_model:  # build the agent now: first text isn't slow, no setup race
        try:
            from app.agent.runtime import get_agent
            get_agent()
            log.info("agent ready (%s)", settings.agent_model)
        except Exception:
            log.exception("agent warm-up failed; will retry on first text")
    if settings.watcher_in_server:
        from app.grabs.watcher import build_watcher

        w = build_watcher()
        t = threading.Thread(target=w.run_forever, name="grab-watcher", daemon=True)
        t.start()
        WATCHER.update(thread=t, watcher=w)
        log.info("grab watcher running on %s", w.dir)
    if settings.plan_time:  # daily reorder-plan text (app/agent/daily_plan.py)
        from app.agent import daily_plan

        started = daily_plan.start(settings.plan_time)
        if started:
            PLAN.update(thread=started[0], job=started[1])
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
