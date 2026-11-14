
from streamlit.testing.v1 import AppTest


def test_dashboard_renders_and_confirms_in_demo_mode(monkeypatch):
    monkeypatch.setenv("DASHBOARD_DEMO", "1")
    at = AppTest.from_file("../app.py", default_timeout=30).run()
    assert not at.exception, at.exception
    assert any("Demo data" in w.value for w in at.warning)
    subs = [s.value for s in at.subheader]
    ok = lambda: next(x.value for x in at.subheader if x.value.startswith("Needs your OK"))
    assert subs[0] == "Stock" and "Reorder" in subs and ok().startswith("Needs your OK (3)")
    assert "Where the food went" in subs and any(s.startswith("What the agent said") for s in subs)
    # confirm the first flagged grab
    submit = next(b for b in at.button if "Confirm" in str(b.label))
    submit.click().run()
    assert not at.exception, at.exception
    assert ok().startswith("Needs your OK (2)")
    assert any("Text me this plan" in str(b.label) for b in at.button)
