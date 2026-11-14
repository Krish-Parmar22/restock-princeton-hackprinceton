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
    assert ws.cell(r, 6).value == f"=ROUNDUP(MAX(0,C{r}-D{r}),0)"
    assert ws.cell(r, 9).value == f'=IF(D{r}<C{r},"REORDER","OK")'
    assert ws["A10"].value == "Total to reorder" and ws["H10"].value == "=SUM(H5:H8)"
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
