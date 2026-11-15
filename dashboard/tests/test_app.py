
from streamlit.testing.v1 import AppTest


def test_dashboard_renders_and_confirms_in_demo_mode(monkeypatch):
    monkeypatch.setenv("DASHBOARD_DEMO", "1")
    at = AppTest.from_file("../app.py", default_timeout=30).run()
    assert not at.exception, at.exception
    subs = [s.value for s in at.subheader]
    ok = lambda: next(x.value for x in at.subheader if x.value.startswith("Needs your OK"))
    assert subs[0] == "Stock" and "Reorder" in subs and ok().startswith("Needs your OK (3)")
    assert "Where the food went" in subs
    # one tap on a flagged grab: "nothing taken"
    submit = next(b for b in at.button if "Nothing taken" in str(b.label))
    submit.click().run()
    assert not at.exception, at.exception
    assert ok().startswith("Needs your OK (2)")
    assert any("Text me this plan" in str(b.label) for b in at.button)
    assert any("morning debrief" in str(b.label) for b in at.button)



def test_password_gate_hides_everything_until_entered(monkeypatch):
    monkeypatch.setenv("DASHBOARD_DEMO", "1")
    monkeypatch.setenv("DASHBOARD_VIEW_KEY", "s3cret")
    at = AppTest.from_file("../app.py", default_timeout=30).run()
    assert not at.exception and not at.subheader, "nothing shown before the password"
    at.text_input[0].input("wrong").run()
    assert at.error and not at.subheader
    at.text_input[0].input("s3cret").run()
    assert any(s.value == "Stock" for s in at.subheader)
