"""Write today's inventory as the shop's Excel sheet.

Usage: uv run python export_excel.py --date 2026-11-14 --out sheet.xlsx [--api http://localhost:8000] [--demo]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from client import API_URL, ApiError, connect
from excel import workbook_bytes


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", type=date.fromisoformat, default=date.today())
    p.add_argument("--out", type=Path, default=None, help="default: inventory-<date>.xlsx")
    p.add_argument("--api", default=API_URL)
    p.add_argument("--demo", action="store_true", help="use demo data instead of the API")
    args = p.parse_args(argv)

    api = connect(args.api, demo=True if args.demo else False)
    try:
        inventory = api.inventory()
        grabs = api.grabs(args.date)
    except ApiError as e:
        print(f"error: {e}\n(use --demo to export demo data)", file=sys.stderr)
        return 1
    out = args.out or Path(f"inventory-{args.date}.xlsx")
    out.write_bytes(workbook_bytes(inventory, args.date, grabs))
    print(f"wrote {out} ({len(inventory)} items, {len(grabs)} camera events){' [demo data]' if api.demo else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
