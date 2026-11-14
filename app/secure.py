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

CODE_PROMPT = "That's sensitive. Reply with the 6-digit code from your authenticator app."
UNLOCK_MIN = 10
MAX_FAILS = 3
LOCKOUT_MIN = 15
CODE_RE = re.compile(r"^\s*(\d{3})\s?(\d{3})\s*$")
# "my code is 123 456", "1 2 3 4 5 6", "123  456": anything short carrying exactly 6 digits is treated
# as a code attempt and kept away from the model (Codex review, Nov 14).
LOOSE_CODE_RE = re.compile(r"(?<!\d)(\d(?:[\s\-.]*\d){5})(?!\d)")
OWNER = "__owner__"   # attempts + replay are per owner credential, not per chat (a new chat_id resets nothing)
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


_SCHEMA_DONE: set[str] = set()
_SCHEMA_LOCK = __import__("threading").Lock()


def ensure_schema() -> None:
    """Create the table once per database per process: concurrent CREATE ... IF NOT EXISTS races in
    Postgres (duplicate pg_type key), found by the parallel-code test."""
    from app.ledger import impl
    key = impl._URL or settings_db_url()
    if key in _SCHEMA_DONE:
        return
    with _SCHEMA_LOCK:
        if key not in _SCHEMA_DONE:
            with _conn() as c:
                c.execute(SCHEMA)
            _SCHEMA_DONE.add(key)


def settings_db_url() -> str:
    from app.config import settings as s
    return s.database_url


def configured() -> bool:
    return bool(settings.owner_totp_secret)


def _row(c, chat_id: str) -> dict:
    ensure_schema()
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
        o = _row(c, OWNER)
    now = _now()
    if o["locked_until"] and o["locked_until"] > now:
        return False
    return bool(r["unlocked_until"] and r["unlocked_until"] > now)


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
    """Check a 6-digit code against the owner's authenticator. Never logs the code.
    Failures, lockout and replay are tracked for the OWNER (one TOTP secret), under a row lock, so
    neither parallel requests nor invented chat ids get extra guesses or reuse a code."""
    if not configured():
        return CodeResult("not_configured")
    code = extract_code(text) or ""
    now = at or _now()
    totp = pyotp.TOTP(settings.owner_totp_secret)
    ensure_schema()
    with _conn() as c:
        for cid in (OWNER, chat_id):
            c.execute("INSERT INTO auth_state (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (cid,))
        with c.transaction():
            o = c.execute("SELECT * FROM auth_state WHERE chat_id = %s FOR UPDATE", (OWNER,)).fetchone()
            r = c.execute("SELECT * FROM auth_state WHERE chat_id = %s FOR UPDATE", (chat_id,)).fetchone()
            if o["locked_until"] and o["locked_until"] > now:
                return CodeResult("locked_out", minutes=max(1, int((o["locked_until"] - now).total_seconds() // 60) + 1))
            counter = None
            for drift in (0, -1, 1):   # current 30 s step or one either side (clock drift / slow typing)
                t = now + timedelta(seconds=30 * drift)
                if code and hmac_eq(totp.at(t), code):
                    counter = totp.timecode(t)
                    break
            if counter is not None and o["last_counter"] is not None and counter <= o["last_counter"]:
                return CodeResult("reused")       # a code works once, in any chat
            if counter is not None:
                c.execute("UPDATE auth_state SET fails = 0, last_counter = %s WHERE chat_id = %s", (counter, OWNER))
                c.execute("UPDATE auth_state SET unlocked_until = %s, pending_text = NULL WHERE chat_id = %s",
                          (now + timedelta(minutes=UNLOCK_MIN), chat_id))
                return CodeResult("ok", question=r["pending_text"], minutes=UNLOCK_MIN)
            fails = o["fails"] + 1
            if fails >= MAX_FAILS:   # lock the owner out everywhere and end every open unlock
                c.execute("UPDATE auth_state SET fails = 0, locked_until = %s WHERE chat_id = %s",
                          (now + timedelta(minutes=LOCKOUT_MIN), OWNER))
                c.execute("UPDATE auth_state SET unlocked_until = NULL, pending_text = NULL WHERE chat_id <> %s",
                          (OWNER,))
                return CodeResult("locked_out", minutes=LOCKOUT_MIN)
            c.execute("UPDATE auth_state SET fails = %s WHERE chat_id = %s", (fails, OWNER))
            return CodeResult("bad", tries_left=MAX_FAILS - fails)


def hmac_eq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


def extract_code(text: str) -> str | None:
    t = text or ""
    m = CODE_RE.match(t)
    if m:
        return m.group(1) + m.group(2)
    if len(t) <= 60 and len(t.split()) <= 8:
        found = LOOSE_CODE_RE.findall(t)
        digits = re.sub(r"\D", "", t)
        if len(found) == 1 and len(digits) == 6:
            return digits
    return None


def looks_like_code(text: str) -> bool:
    return extract_code(text) is not None


# ---- what may leave the building while bank info is locked
_BANK_NUM_FLOOR = 100.0   # inventory numbers are small; statement amounts below $100 aren't checked


def _bank_numbers() -> set[float]:
    try:
        from app import bank
        st = bank.statement(31)
    except Exception:
        return set()
    nums = {st["balance"], st["money_in"], st["money_out"], abs(st["net"]), *st["paid_by_payee"].values()}
    for t in st["transactions"] + st["largest_payments"]:
        nums.add(abs(t["amount"]))
        if "balance" in t:
            nums.add(t["balance"])
    return {round(abs(n), 2) for n in nums if abs(n) >= _BANK_NUM_FLOOR}


_AMOUNT = re.compile(r"\d[\d,]*(?:\.\d+)?")


def leaks_bank_info(chat_id: str | None, text: str) -> bool:
    """True if `text` carries a bank-statement amount and this chat is not unlocked right now.
    Checked at EMISSION for every path (agent text, subagents, plain answers, approval prompts,
    proactive texts), so history, files, memories or an expired window can't leak a number."""
    if not text or not configured():
        return False
    if chat_id and is_unlocked(chat_id):
        return False
    bank = _bank_numbers()
    if not bank:
        return False
    for raw in _AMOUNT.findall(text):
        try:
            n = round(float(raw.replace(",", "")), 2)
        except ValueError:
            continue
        if n >= _BANK_NUM_FLOOR and any(abs(n - b) < 0.006 or (n == int(n) and abs(n - round(b)) < 1) for b in bank):
            return True
    return False


def looks_like_lock(text: str) -> bool:
    return bool(LOCK_RE.match(text or ""))
