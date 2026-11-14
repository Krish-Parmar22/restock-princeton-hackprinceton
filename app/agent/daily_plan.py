"""Daily reorder-plan text: at PLAN_TIME (shop-local, America/New_York) the agent texts the owner
the morning briefing (runtime.send_morning_briefing: how yesterday went, grabs to confirm, reorder). Started from app/api/server.py lifespan when
PLAN_TIME is set. Fires at most once per shop-local day; a server (re)started after the day's
PLAN_TIME waits for tomorrow rather than re-sending.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time
from typing import Callable

from app.ledger.impl import TZ

log = logging.getLogger("daily_plan")


def parse_plan_time(s: str) -> time | None:
    """'09:00' -> time(9, 0). Empty -> None (off). Raises ValueError on garbage."""
    s = (s or "").strip()
    if not s:
        return None
    h, m = s.split(":")
    return time(int(h), int(m))


def initial_last_run(now: datetime, at: time) -> date | None:
    """Starting after today's PLAN_TIME counts today as done (a restart must not send a second plan)."""
    local = now.astimezone(TZ)
    return local.date() if local.time() >= at else None


def is_due(now: datetime, at: time, last_run: date | None) -> bool:
    """True once the shop-local clock reaches `at`, unless it already ran today."""
    local = now.astimezone(TZ)
    return local.time() >= at and last_run != local.date()


class DailyPlan:
    def __init__(self, at: time, send: Callable[[], object] | None = None, poll_s: float = 30.0,
                 clock: Callable[[], datetime] = lambda: datetime.now(TZ)):
        self.at, self.poll_s, self.clock = at, poll_s, clock
        self._send = send
        self.stop = threading.Event()
        self.last_run: date | None = initial_last_run(clock(), at)

    def send(self) -> object:
        if self._send is not None:
            return self._send()
        from app.agent.runtime import send_morning_briefing   # yesterday + flagged grabs + reorder, one text
        return send_morning_briefing()

    def tick(self) -> bool:
        """Send if due. Never raises. Returns True if it attempted a send."""
        now = self.clock()
        if not is_due(now, self.at, self.last_run):
            return False
        self.last_run = now.astimezone(TZ).date()   # mark first: a failing send is not retried every 30s
        try:
            out = self.send()
            log.info("daily reorder plan sent at %s: %s", now.astimezone(TZ).isoformat(timespec="minutes"), out)
        except Exception:
            log.exception("daily reorder plan failed")
        return True

    def run_forever(self) -> None:
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:  # belt and braces: the thread must never die
                log.exception("daily plan tick crashed")
            self.stop.wait(self.poll_s)


def start(plan_time: str) -> tuple[threading.Thread, DailyPlan] | None:
    """Start the daemon thread; None if PLAN_TIME is empty or invalid (logged, never raises)."""
    try:
        at = parse_plan_time(plan_time)
    except (ValueError, TypeError):
        log.error("PLAN_TIME=%r is not HH:MM; daily reorder plan disabled", plan_time)
        return None
    if at is None:
        return None
    job = DailyPlan(at)
    t = threading.Thread(target=job.run_forever, name="daily-plan", daemon=True)
    t.start()
    log.info("daily reorder plan scheduled at %s %s", at.strftime("%H:%M"), TZ.key)
    return t, job
