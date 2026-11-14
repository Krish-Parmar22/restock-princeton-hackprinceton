"""Seed the demo week into Postgres.

Usage: uv run python scripts/seed_demo.py [--db postgresql://localhost:5432/pizza] [--reset]
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, so `import app` works

from app.db.conn import apply_schema
from app.ledger import api, impl
from app.ledger.seed import seed

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=None)
ap.add_argument("--reset", action="store_true")
a = ap.parse_args()

impl.use_database(a.db)
apply_schema(a.db)
print(seed(a.db, reset=a.reset))
for d in range(8, 15):
    day = date(2026, 11, d)
    pep = next(v for v in api.get_variance(day, day) if v.item == "pepperoni")
    print(f"{day:%a %m-%d} pepperoni theoretical {pep.theoretical:6.2f}  actual {pep.actual:6.2f}  "
          f"variance {pep.variance_qty:+5.2f} lb  ${pep.variance_usd:+6.2f}")
print("week:", [(v.item, v.variance_usd) for v in api.get_variance(date(2026, 11, 8), date(2026, 11, 14))])
