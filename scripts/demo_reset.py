"""Reset to a clean, known demo state. Run right before every judge.

  uv run python scripts/demo_reset.py

- Re-seeds the demo week (pizza kitchen + drinks) and wipes today's test junk (flagged grabs, orders,
  alerts, agent messages). Removes test-only items (e.g. 'cards').
- Keeps the owner's phone chat(s) (Photon chat ids start with pho_) so the agent can text first,
  and clears their conversation history so the demo starts fresh.
- Puts cheese, Sprite and pineapple juice just above par: the SECOND handful / can / bottle drops it
  below par and the owner's phone gets the alert automatically (cooldowns cleared).
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

# item -> (par, count). The SECOND take crosses par, so the judge sees one normal grab, then the alert:
# cheese handful = 0.3 lb (15.5 -> 15.2 -> 14.9 < 15); cans/bottles = 1 each (par + 1 -> par -> below).
EDGE = {"cheese": (15, 15.5), "sprite": (10, 11), "pineapple_juice": (4, 5)}
EXTRA_ITEMS = {"pineapple_juice": ("each", 2.50)}
THREAD_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=settings.database_url)
    a = ap.parse_args()
    with connect(a.url) as c:
        phones = [r["chat_id"] for r in c.execute("SELECT chat_id, sender FROM chats WHERE chat_id LIKE 'pho_%'").fetchall()]
        senders = {r["chat_id"]: r["sender"] for r in c.execute("SELECT chat_id, sender FROM chats WHERE chat_id LIKE 'pho_%'").fetchall()}
    with connect(a.url) as c:   # the real Casa Piada grabs (re-checked by d1): keep them in "Needs your OK"
        shop = c.execute("SELECT * FROM grab_events WHERE grab_id LIKE '%%-shop'").fetchall()
        shop_ans = c.execute("SELECT grab_id, question_id, answer, probs FROM d1_answers WHERE grab_id LIKE '%%-shop'").fetchall()
        shop_items = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM items").fetchall()}
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
        from psycopg.types.json import Jsonb
        ids = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM items").fetchall()}
        for g in shop:
            item = shop_items.get(g["item_id"])
            c.execute("""INSERT INTO grab_events (grab_id, zone, camera_id, source, t_start, t_end, item_id, action,
                                                  qty_label, qty, confidence, status, frames)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                      (g["grab_id"], g["zone"], g["camera_id"], g["source"], g["t_start"], g["t_end"],
                       ids.get(item) if item else None, g["action"], g["qty_label"], g["qty"], g["confidence"],
                       g["status"], Jsonb(g["frames"])))
        for x in shop_ans:
            c.execute("INSERT INTO d1_answers (grab_id, question_id, answer, probs) VALUES (%s,%s,%s,%s)",
                      (x["grab_id"], x["question_id"], x["answer"], Jsonb(x["probs"])))
        for chat in phones:
            c.execute("INSERT INTO chats (chat_id, sender) VALUES (%s, %s) ON CONFLICT DO NOTHING", (chat, senders.get(chat)))
        for t in THREAD_TABLES:   # fresh conversation for the demo
            try:
                c.execute(f"DELETE FROM {t} WHERE thread_id = ANY(%s)", (phones,))
            except Exception:
                pass
    try:
        from app import sheet
        sheet.refresh("demo reset")
    except Exception as e:
        print("sheet refresh skipped:", e)
    print(f"Demo reset. Owner phone chats kept: {len(phones)}. The 2nd grab of cheese, Sprite or pineapple juice triggers the alert.")


if __name__ == "__main__":
    main()
