
from streamlit.testing.v1 import AppTest


def test_dashboard_renders_and_confirms_in_demo_mode(monkeypatch):
    monkeypatch.setenv("DASHBOARD_DEMO", "1")
    at = AppTest.from_file("../app.py", default_timeout=30).run()
    assert not at.exception, at.exception
    assert any("Demo data" in w.value for w in at.warning)
    subs = [s.value for s in at.subheader]
    assert subs[0] == "Stock" and subs[1].startswith("Needs your OK (3)")
    assert "Where the food went" in subs and any(s.startswith("What the agent said") for s in subs)
    # confirm the first flagged grab
    submit = next(b for b in at.button if "Confirm" in str(b.label))
    submit.click().run()
    assert not at.exception, at.exception
    assert at.subheader[1].value.startswith("Needs your OK (2)")
