"""Step-up verification for sensitive answers (bank balance, statement) over iMessage.

The owner's authenticator app (Google Authenticator, 1Password, ...) holds a TOTP secret
(OWNER_TOTP_SECRET, set up once with scripts/setup_authenticator.py).

  - A sensitive tool on a locked chat doesn't return data: it records the question as pending
    and tells the agent to ask for the 6-digit code.
  - The owner's 6-digit reply is checked HERE, in code. It never reaches the language model.
  - Right code: the chat is unlocked for UNLOCK_MIN minutes and the pending question is re-run.
  - Wrong code: MAX_FAILS tries, then the chat is locked out for LOCKOUT_MIN minutes.
  - "lock" from the owner locks it again immediately.
Stale numbers can't leak after the window closes: the d1 guard only accepts numbers from tool
results in the current turn, and the tool returns nothing while locked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pyotp

from app.config import settings

UNLOCK_MIN = 10
MAX_FAILS = 3
LOCKOUT_MIN = 15
CODE_RE = re.compile(r"^\s*(\d{3})\s?(\d{3})\s*$")
LOCK_RE = re.compile(r"^\s*lock( it)?( now)?\s*[.!]?\s*$", re.I)

SCHEMA = """CREATE TABLE IF NOT EXISTS auth_state (
    chat_id        TEXT PRIMARY KEY,
    unlocked_until TIMESTAMPTZ,
    fails          INT NOT NULL DEFAULT 0,
    locked_until   TIMESTAMPTZ,
    pending_text   TEXT,
    last_counter   BIGINT
)"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conn():
    from app.ledger.impl import _conn as ledger_conn
    conn = ledger_conn()
    return conn


def ensure_schema() -> None:
    with _conn() as c:
        c.execute(SCHEMA)


def configured() -> bool:
    return bool(settings.owner_totp_secret)


def _row(c, chat_id: str) -> dict:
    c.execute(SCHEMA)
    r = c.execute("SELECT * FROM auth_state WHERE chat_id = %s", (chat_id,)).fetchone()
    if r is None:
        c.execute("INSERT INTO auth_state (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (chat_id,))
        r = c.execute("SELECT * FROM auth_state WHERE chat_id = %s", (chat_id,)).fetchone()
    return r


def is_unlocked(chat_id: str | None) -> bool:
    if not chat_id:
        return False
    with _conn() as c:
        r = _row(c, chat_id)
    return bool(r["unlocked_until"] and r["unlocked_until"] > _now())


def request_code(chat_id: str, question: str) -> None:
    """Remember what the owner asked, to answer it once they've verified."""
    with _conn() as c:
        _row(c, chat_id)
        c.execute("UPDATE auth_state SET pending_text = %s WHERE chat_id = %s", (question, chat_id))


def pending(chat_id: str) -> str | None:
    with _conn() as c:
        return _row(c, chat_id)["pending_text"]


def lock(chat_id: str) -> None:
    with _conn() as c:
        _row(c, chat_id)
        c.execute("UPDATE auth_state SET unlocked_until = NULL, pending_text = NULL WHERE chat_id = %s", (chat_id,))


@dataclass
class CodeResult:
    status: str          # "ok" | "bad" | "locked_out" | "not_configured" | "reused"
    tries_left: int = 0
    question: str | None = None
    minutes: int = 0


def try_code(chat_id: str, text: str, at: datetime | None = None) -> CodeResult:
    """Check a 6-digit code against the owner's authenticator. Never logs the code."""
    if not configured():
        return CodeResult("not_configured")
    m = CODE_RE.match(text)
    code = (m.group(1) + m.group(2)) if m else ""
    now = at or _now()
    totp = pyotp.TOTP(settings.owner_totp_secret)
    with _conn() as c:
        r = _row(c, chat_id)
        if r["locked_until"] and r["locked_until"] > now:
            return CodeResult("locked_out", minutes=max(1, int((r["locked_until"] - now).total_seconds() // 60) + 1))
        # accept the current 30 s step or the one either side (clock drift / slow typing)
        counter = None
        for drift in (0, -1, 1):
            t = now + timedelta(seconds=30 * drift)
            if code and hmac_eq(totp.at(t), code):
                counter = totp.timecode(t)
                break
        if counter is not None and r["last_counter"] is not None and counter <= r["last_counter"]:
            return CodeResult("reused")       # a code works once (replay protection)
        if counter is not None:
            c.execute("""UPDATE auth_state SET unlocked_until = %s, fails = 0, locked_until = NULL,
                         pending_text = NULL, last_counter = %s WHERE chat_id = %s""",
                      (now + timedelta(minutes=UNLOCK_MIN), counter, chat_id))
            return CodeResult("ok", question=r["pending_text"], minutes=UNLOCK_MIN)
        fails = r["fails"] + 1
        if fails >= MAX_FAILS:
            c.execute("""UPDATE auth_state SET fails = 0, locked_until = %s, pending_text = NULL
                         WHERE chat_id = %s""", (now + timedelta(minutes=LOCKOUT_MIN), chat_id))
            return CodeResult("locked_out", minutes=LOCKOUT_MIN)
        c.execute("UPDATE auth_state SET fails = %s WHERE chat_id = %s", (fails, chat_id))
        return CodeResult("bad", tries_left=MAX_FAILS - fails)


def hmac_eq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


def looks_like_code(text: str) -> bool:
    return bool(CODE_RE.match(text or ""))


def looks_like_lock(text: str) -> bool:
    return bool(LOCK_RE.match(text or ""))
