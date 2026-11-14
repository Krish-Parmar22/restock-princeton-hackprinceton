# dashboard

What the owner (and the judges) see: stock, where the food went with camera proof, flagged grabs to confirm, what the agent said, and their Excel sheet filled in. Reads the agent API on `:8000` (contract: `docs/arnav-shah.md`).

## Run
```bash
cd dashboard
uv sync
uv run streamlit run app.py                         # http://localhost:8501
uv run python export_excel.py --date 2026-11-14 --out sheet.xlsx
uv run pytest                                       # 9 tests, incl. a full render of the app
```
Settings:
- `API_URL`: defaults to `http://localhost:8000`.
- If the API is down, the dashboard shows **demo data**, with a banner saying so. `DASHBOARD_DEMO=1` forces demo data; `DASHBOARD_DEMO=0` disables it.
- `export_excel.py` never falls back silently: it fails unless you pass `--demo`.
- Grab frames are local file paths from the API, so run the dashboard on the same machine as `app/`.

## Panels
- **Stock cards:** on hand, par, used today, unit cost. Below-par items are red and listed first.
- **Needs your OK:** the day's flagged grabs. Each shows before / during / after frames and a ▶ clip, and has action / item / amount pickers. ✓ Confirm calls `POST /api/grabs/{id}/confirm`.
- **Where the food went:** variance for the chosen dates, biggest $ gap first. Click a row to see that item's grabs with frames.
- **What the agent said:** recent agent texts with their d1 score and decision (sent / softened / blocked).
- **⬇ Download today's sheet:** the Excel auto-fill.

## Excel
`excel.py` writes:
- An `Inventory` sheet: item, unit, par, on hand, used today, order qty, unit cost, order cost, status. Below-par rows are red, with a total to reorder.
- A `Camera log` sheet.

Order qty, cost, status and the total are live formulas, so the sheet still works when the owner edits a number. **To match the shop's real sheet** (photo Saturday), edit `COLUMNS` and `SHOP_NAME` at the top of `excel.py`.
