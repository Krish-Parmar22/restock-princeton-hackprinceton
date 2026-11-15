"""Excel auto-fill: today's inventory as an .xlsx shaped like the shop's own sheet.

We get a photo of their real sheet Saturday morning. To match it, edit COLUMNS (order, headers)
and SHOP_NAME; everything else follows. Order qty and totals are live Excel formulas, so the
sheet still works if the owner changes a number by hand.
"""
from __future__ import annotations

import io
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

SHOP_NAME = "Daily Inventory"

# Order rule (same as the agent's reorder_plan): if on hand + on order < par, order back up to 2x par.
# (key, header, width, number format). Keys: item, unit, par, on_hand, on_order, used_today, order_qty,
# unit_cost, order_cost, status. order_qty / order_cost / status are formulas.
COLUMNS = [
    ("item", "Item", 18, None),
    ("unit", "Unit", 8, None),
    ("par", "Par", 8, "0.##"),
    ("on_hand", "On Hand", 10, "0.##"),
    ("on_order", "On Order", 10, "0.##"),
    ("used_today", "Used Today", 11, "0.##"),
    ("order_qty", "Order Qty", 10, "0"),
    ("unit_cost", "Unit Cost", 10, '"$"#,##0.00'),
    ("order_cost", "Order Cost", 11, '"$"#,##0.00'),
    ("status", "Status", 11, None),
]

HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
LOW_FILL = PatternFill("solid", fgColor="FDE2E1")
THIN = Side(style="thin", color="C8CDD3")
GRID = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


# Casa Piada's own inventory sheet (the "Example Inventory.xlsx" they gave us, tab "Hoja 1"),
# reproduced cell for cell so they can adopt it with no change: our item -> their row label, in their order.
SHOP_SHEET_ITEMS = [
    ("coke", "Coca Cola"), ("sprite", "Sprite"), ("diet_coke", "Diet Coke"), ("water", "Water 500ml Flat"),
    ("siciliana_orange", "Siciliana drinks-Orange"), ("siciliana_limonata", "Siciliana drinks-Limonata"),
    ("fresca", "Fresca"), ("sparkling_water", "Sparkling water"), ("ice_tea_peach", "Ice Tea Peach"),
    ("ice_tea_lemon", "Ice Tea Lemon "),
]
SHOP_FONT = "Times New Roman"
SHOP_ITEM_FILL = PatternFill("solid", fgColor="FAE2D5")


def shop_sheet(ws, inventory: list[dict], day: date, avg_use: dict[str, float] | None = None) -> None:
    """Their layout exactly: A1 'Soft Drinks', row 2 headers (Avrg. Qtty, Tuesday, Thursday, On Hand,
    Replacement, Delta), item rows with Delta = On Hand - Replacement and Value = price x On Hand.
    Filled from the camera ledger: On Hand = live stock, Replacement = par, Tuesday/Thursday = what to
    order on the next delivery day to get back to Replacement (on-order already counted)."""
    import math
    inv = {r["item"]: r for r in inventory}
    rows = [(k, label) for k, label in SHOP_SHEET_ITEMS if k in inv]
    if not rows:
        return
    deliver_tue = day.weekday() in (4, 5, 6, 0)          # Fri-Mon: next delivery Tuesday; Tue-Thu: Thursday
    f = lambda **kw: Font(name=SHOP_FONT, size=12, **kw)
    center = Alignment(horizontal="center")
    ws["A1"] = "Soft Drinks"
    ws["A1"].font = f(bold=True)
    for col, h in (("B", "Avrg. Qtty"), ("C", "Tuesday"), ("D", "Thursday"), ("G", "On Hand"),
                   ("H", "Replacement"), ("I", "Delta")):
        ws[f"{col}2"] = h
        ws[f"{col}2"].font = f()
        ws[f"{col}2"].alignment = center
    targets = [1] + list(range(3, 3 + len(rows) - 1))   # their sheet: first item on row 1, headers on row 2
    for r, (key, name) in zip(targets, rows):
        row = inv[key]
        short = max(0.0, row["par"] - row["on_hand"] - row.get("on_order", 0))
        order = math.ceil(short)
        vals = {"B": round((avg_use or {}).get(key, 0.0), 1), "C": order if deliver_tue else 0,
                "D": 0 if deliver_tue else order, "E": name, "G": row["on_hand"], "H": row["par"],
                "I": f"=G{r}-H{r}", "J": row["unit_cost"], "K": f"=J{r}*G{r}"}
        for col, v in vals.items():
            c = ws[f"{col}{r}"]
            c.value = v
            c.font = f()
            c.alignment = Alignment(horizontal="right") if col == "K" else center
        ws[f"E{r}"].fill = SHOP_ITEM_FILL
        ws[f"K{r}"].number_format = "[$$]#,##0.00"
        ws[f"J{r}"].number_format = "[$$]#,##0.00"
    ws.column_dimensions["E"].width = 19.8
    ws.conditional_formatting.add(f"I1:I{targets[-1]}", FormulaRule(formula=["I1<0"], font=Font(color="9B1C1C", bold=True)))


def _col(key: str) -> str:
    return get_column_letter([c[0] for c in COLUMNS].index(key) + 1)


def build_workbook(inventory: list[dict], day: date, grabs: list[dict] | None = None,
                   orders: list[dict] | None = None, note: str | None = None,
                   avg_use: dict[str, float] | None = None) -> Workbook:
    wb = Workbook()
    first = wb.active
    if any(r["item"] in dict(SHOP_SHEET_ITEMS) for r in inventory):
        first.title = "Hoja 1"            # the shop's own sheet, first, exactly as they keep it
        shop_sheet(first, inventory, day, avg_use)
        ws = wb.create_sheet("Inventory")
    else:
        ws = first
        ws.title = "Inventory"
    ncols = len(COLUMNS)
    last = get_column_letter(ncols)

    ws.merge_cells(f"A1:{last}1")
    ws["A1"] = f"{SHOP_NAME} — {day.strftime('%A, %b %-d, %Y')}"
    ws["A1"].font = Font(size=14, bold=True)
    ws.merge_cells(f"A2:{last}2")
    ws["A2"] = note or f"Filled automatically from the camera + register at {datetime.now().strftime('%-I:%M %p')}"
    ws["A2"].font = Font(size=9, italic=True, color="6B7280")

    hdr = 4
    for i, (_, header, width, _) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=hdr, column=i, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = GRID
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = ws.cell(row=hdr + 1, column=1)

    par, on_hand, qty, cost, onord = _col("par"), _col("on_hand"), _col("order_qty"), _col("unit_cost"), _col("on_order")
    first = hdr + 1
    rows = sorted(inventory, key=lambda r: r["item"])
    for r, row in enumerate(rows, start=first):
        values = {
            "item": row["item"].replace("_", " ").title(),
            "unit": row["unit"],
            "par": row["par"],
            "on_hand": row["on_hand"],
            "used_today": row["used_today"],
            "on_order": row.get("on_order", 0),
            "unit_cost": row["unit_cost"],
            "order_qty": f"=IF({on_hand}{r}+{onord}{r}<{par}{r},ROUNDUP(2*{par}{r}-{on_hand}{r}-{onord}{r},0),0)",
            "order_cost": f"={qty}{r}*{cost}{r}",
            "status": f'=IF({on_hand}{r}+{onord}{r}<{par}{r},"REORDER",IF({on_hand}{r}<{par}{r},"ON ORDER","OK"))',
        }
        for c, (key, _, _, fmt) in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=r, column=c, value=values[key])
            cell.border = GRID
            if fmt:
                cell.number_format = fmt
            if key != "item":
                cell.alignment = Alignment(horizontal="center")
    end = first + len(rows) - 1

    if rows:
        # Below par rows go red, and stay correct if the owner edits On Hand.
        ws.conditional_formatting.add(
            f"A{first}:{last}{end}",
            FormulaRule(formula=[f"${on_hand}{first}<${par}{first}"], fill=LOW_FILL, font=Font(bold=True, color="9B1C1C")),
        )
        total = end + 2
        oc = _col("order_cost")
        ws[f"A{total}"] = "Total to reorder"
        ws[f"A{total}"].font = Font(bold=True)
        ws[f"{oc}{total}"] = f"=SUM({oc}{first}:{oc}{end})"
        ws[f"{oc}{total}"].number_format = '"$"#,##0.00'
        ws[f"{oc}{total}"].font = Font(bold=True)

    if orders is not None:
        _orders(wb.create_sheet("Orders"), orders)
    if grabs:
        _camera_log(wb.create_sheet("Camera log"), grabs)
    ws.sheet_view.zoomScale = 120
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    return wb


def _camera_log(ws, grabs: list[dict]) -> None:
    headers = ["Time", "Bin", "Action", "Item", "Amount", "Confidence", "Status", "Grab id"]
    widths = [10, 12, 10, 12, 16, 11, 11, 34]
    for i, (h, w) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=1, column=i, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        ws.column_dimensions[get_column_letter(i)].width = w
    for r, g in enumerate(sorted(grabs, key=lambda g: g["t_start"]), start=2):
        t = datetime.fromisoformat(str(g["t_start"]).replace("Z", "+00:00"))
        vals = [t.strftime("%-I:%M %p"), g["zone"], (g.get("action") or "?").replace("_", " "), g.get("item") or "?",
                (g.get("qty_label") or "?").replace("_", " "), g.get("confidence"), g["status"], g["grab_id"]]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            if c == 6:
                cell.number_format = "0%"
            if g["status"] == "flagged":
                cell.fill = LOW_FILL
    ws.freeze_panes = "A2"


def _orders(ws, orders: list[dict]) -> None:
    """Every reorder, newest first: who approved it is the owner, by text (status approved)."""
    headers = ["Placed", "Item", "Qty", "Unit", "Unit Cost", "Cost", "Supplier", "Status"]
    widths = [18, 16, 8, 8, 10, 11, 16, 11]
    for i, (h, w) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=1, column=i, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(i)].width = w
    for r, o in enumerate(orders, start=2):
        t = datetime.fromisoformat(str(o["ts"]).replace("Z", "+00:00"))
        vals = [t.strftime("%a %b %-d, %-I:%M %p"), o["item"].replace("_", " ").title(), o["qty"], o["unit"],
                o["unit_cost"], f"=C{r}*E{r}", o.get("supplier") or "", o["status"]]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = GRID
            if c in (5, 6):
                cell.number_format = '"$"#,##0.00'
            if o["status"] == "rejected":
                cell.font = Font(color="9CA3AF", strike=True)
    if orders:
        t = len(orders) + 3
        ws[f"A{t}"] = "Total approved"
        ws[f"A{t}"].font = Font(bold=True)
        ws[f"F{t}"] = f'=SUMIF(H2:H{len(orders) + 1},"approved",F2:F{len(orders) + 1})'
        ws[f"F{t}"].number_format = '"$"#,##0.00'
        ws[f"F{t}"].font = Font(bold=True)
    ws.freeze_panes = "A2"


def workbook_bytes(inventory: list[dict], day: date, grabs: list[dict] | None = None,
                   orders: list[dict] | None = None, note: str | None = None,
                   avg_use: dict[str, float] | None = None) -> bytes:
    buf = io.BytesIO()
    build_workbook(inventory, day, grabs, orders, note, avg_use).save(buf)
    return buf.getvalue()
