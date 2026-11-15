"""Demo: an item runs out on camera-less video takes. Sets its count, then runs the real alert
check, which texts the owner "We're out of X. Reorder N for $Y? Reply yes or no."

  uv run python scripts/demo_run_out.py pineapple_juice        # count -> 0, alert sent
  uv run python scripts/demo_run_out.py sprite --to 4          # count -> 4 (below par 10)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.alerts import check_and_alert  # noqa: E402
from app.ledger import api as ledger  # noqa: E402
from app.ledger.impl import _conn  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("item")
ap.add_argument("--to", type=float, default=0)
a = ap.parse_args()
ledger.record_count(a.item, a.to, source="demo")
with _conn() as c:   # let it alert now even if it alerted earlier today
    c.execute("DELETE FROM alerts a USING items i WHERE a.item_id = i.id AND i.name = %s", (a.item,))
sent = check_and_alert()
print(sent or "No alert sent (is the item below par, and has your phone texted the line?)")
