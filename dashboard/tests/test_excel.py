from datetime import date

from openpyxl import load_workbook

from demo import DemoApi
from excel import COLUMNS, workbook_bytes
from export_excel import main as export_main

DAY = date(2026, 11, 14)


def _sheet(tmp_path, grabs=None):
    api = DemoApi()
    path = tmp_path / "s.xlsx"
    path.write_bytes(workbook_bytes(api.inventory(), DAY, grabs))
    return load_workbook(path)


def test_inventory_sheet_layout_and_formulas(tmp_path):
    wb = _sheet(tmp_path)
    ws = wb["Inventory"]
    assert "Saturday, Nov 14, 2026" in ws["A1"].value
    assert [c.value for c in ws[4]] == [h for _, h, _, _ in COLUMNS]
    rows = {ws.cell(r, 1).value: r for r in range(5, 9)}
    assert set(rows) == {"Cheese", "Dough", "Pepperoni", "Sauce"}
    r = rows["Pepperoni"]
    assert ws.cell(r, 4).value == 4.25 and ws.cell(r, 3).value == 10
    assert ws.cell(r, 5).value == 0                       # On Order
    assert ws.cell(r, 7).value == f"=IF(D{r}+E{r}<C{r},ROUNDUP(2*C{r}-D{r}-E{r},0),0)"
    assert ws.cell(r, 10).value == f'=IF(D{r}+E{r}<C{r},"REORDER",IF(D{r}<C{r},"ON ORDER","OK"))'
    assert ws["A10"].value == "Total to reorder" and ws["I10"].value == "=SUM(I5:I8)"
    assert ws.conditional_formatting, "below-par rows are highlighted"
    assert "Camera log" not in wb.sheetnames


def test_camera_log_sheet(tmp_path):
    wb = _sheet(tmp_path, DemoApi().grabs(DAY))
    log = wb["Camera log"]
    assert log["A1"].value == "Time" and log.max_row == 8
    assert log["B2"].value == "pepperoni"


def test_export_cli_demo(tmp_path, capsys):
    out = tmp_path / "sheet.xlsx"
    assert export_main(["--date", "2026-11-14", "--out", str(out), "--demo"]) == 0
    assert load_workbook(out)["Inventory"]["A5"].value == "Cheese"
    assert "demo data" in capsys.readouterr().out


def test_export_cli_api_down(tmp_path, capsys):
    assert export_main(["--out", str(tmp_path / "x.xlsx"), "--api", "http://127.0.0.1:9"]) == 1
    assert "--demo" in capsys.readouterr().err


def test_first_tab_is_casa_piadas_own_sheet(tmp_path):
    """The restaurant's sheet layout (Hoja 1): they can adopt it with no change."""
    from datetime import date as _d

    from excel import build_workbook
    inv = [{"item": "coke", "unit": "each", "par": 20, "on_hand": 31, "on_order": 0, "used_today": 0, "unit_cost": 1.0, "below_par": False},
           {"item": "sprite", "unit": "each", "par": 10, "on_hand": 5, "on_order": 0, "used_today": 0, "unit_cost": 1.0, "below_par": True},
           {"item": "cheese", "unit": "lb", "par": 15, "on_hand": 20, "on_order": 0, "used_today": 0, "unit_cost": 3.8, "below_par": False}]
    wb = build_workbook(inv, _d(2026, 11, 15), avg_use={"sprite": 3.0})     # a Sunday: next delivery Tuesday
    ws = wb.worksheets[0]
    assert ws.title == "Hoja 1" and ws["A1"].value == "Soft Drinks"
    assert [ws[c].value for c in ("B2", "C2", "D2", "G2", "H2", "I2")] == ["Avrg. Qtty", "Tuesday", "Thursday", "On Hand", "Replacement", "Delta"]
    assert (ws["E1"].value, ws["G1"].value, ws["H1"].value, ws["I1"].value, ws["K1"].value) == ("Coca Cola", 31, 20, "=G1-H1", "=J1*G1")
    assert (ws["E3"].value, ws["G3"].value, ws["H3"].value, ws["C3"].value, ws["D3"].value, ws["B3"].value) == ("Sprite", 5, 10, 5, 0, 3.0)
    assert ws["E1"].font.name == "Times New Roman" and ws["E1"].fill.fgColor.rgb.endswith("FAE2D5")
    cells = {c.value: c.coordinate for row in ws.iter_rows() for c in row if c.value is not None}
    assert "Kitchen" in cells and "Cheese (lb)" in cells, "the rest of the shop is on the same sheet, own section"
    r = int(cells["Cheese (lb)"][1:])
    assert ws[f"G{r}"].value == 20 and ws[f"H{r}"].value == 15 and ws[f"I{r}"].value == f"=G{r}-H{r}"
    assert wb.sheetnames[1] == "Inventory"
