"""Reset to a clean, known demo state. Run right before every judge.

  uv run python scripts/demo_reset.py

- Re-seeds the demo week (pizza kitchen + drinks) and wipes today's test junk (flagged grabs, orders,
  alerts, agent messages). Removes test-only items (e.g. 'cards').
- Keeps the owner's phone chat(s) (Photon chat ids start with pho_) so the agent can text first,
  and clears their conversation history so the demo starts fresh.
- Puts cheese and Sprite JUST above par: one handful of cheese, or one can of Sprite, drops it below
  par and the owner's phone gets the alert automatically (cooldowns cleared).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db.conn import connect  # noqa: E402
from app.ledger.seed import seed  # noqa: E402

# item -> (par, count). One take crosses par: cheese handful = 0.3 lb, a Sprite = 1 can.
EDGE = {"cheese": (15, 15.2), "sprite": (6, 6), "pineapple_juice": (4, 4)}
EXTRA_ITEMS = {"pineapple_juice": ("each", 2.50)}
THREAD_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=settings.database_url)
    a = ap.parse_args()
    with connect(a.url) as c:
        phones = [r["chat_id"] for r in c.execute("SELECT chat_id, sender FROM chats WHERE chat_id LIKE 'pho_%'").fetchall()]
        senders = {r["chat_id"]: r["sender"] for r in c.execute("SELECT chat_id, sender FROM chats WHERE chat_id LIKE 'pho_%'").fetchall()}
    seed(a.url, reset=True)
    with connect(a.url) as c:
        # the seeded week runs through tonight; drop anything after "now" or it eats stock as the clock passes
        c.execute("DELETE FROM grab_events WHERE t_start > now()")
        c.execute("DELETE FROM pos_sales WHERE ts > now()")
        for name, (unit, cost) in EXTRA_ITEMS.items():
            c.execute("""INSERT INTO items (name, unit, unit_cost, par, qty_per_label)
                         VALUES (%s, %s, %s, 4, '{"1":1,"2":2,"3":3,"pinch":1,"handful":2,"whole_container":6}')
                         ON CONFLICT (name) DO NOTHING""", (name, unit, cost))
        for item, (par, count) in EDGE.items():
            c.execute("UPDATE items SET par = %s WHERE name = %s", (par, item))
            c.execute("INSERT INTO counts (ts, item_id, qty, source) SELECT now(), id, %s, 'demo_reset' FROM items WHERE name = %s",
                      (count, item))
        # nothing else starts below par: only the item the judge grabs should alert
        c.execute("""INSERT INTO counts (ts, item_id, qty, source)
                     SELECT now(), id, par * 2, 'demo_reset' FROM items
                     WHERE name <> ALL(%s)""", (list(EDGE),))
        for chat in phones:
            c.execute("INSERT INTO chats (chat_id, sender) VALUES (%s, %s) ON CONFLICT DO NOTHING", (chat, senders.get(chat)))
        for t in THREAD_TABLES:   # fresh conversation for the demo
            try:
                c.execute(f"DELETE FROM {t} WHERE thread_id = ANY(%s)", (phones,))
            except Exception:
                pass
        try:
            c.execute("DELETE FROM auth_state")
        except Exception:
            pass
    try:
        from app import sheet
        sheet.refresh("demo reset")
    except Exception as e:
        print("sheet refresh skipped:", e)
    print(f"Demo reset. Owner phone chats kept: {len(phones)}. One take of cheese or Sprite now triggers the alert.")


if __name__ == "__main__":
    main()
