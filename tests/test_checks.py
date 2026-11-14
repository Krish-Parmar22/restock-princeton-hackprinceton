"""d1 grab checks, the grabs watcher, and the Liquid request/response mapping.
No network and no database: the ledger's record_grab is mocked."""
from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.config import settings
from app.contracts import GrabPackage
from app.d1.base import D1Question, FakeD1
from app.d1.liquid import (API_URL, MAX_IMAGES, LiquidD1, LiquidD1Error, build_request, parse_response)
from app.grabs import watcher as watcher_mod
from app.grabs.checks import build_questions, check_grab
from app.grabs.watcher import GrabWatcher

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import make_fake_grabs  # noqa: E402

ITEMS = ["cheese", "dough", "pepperoni", "sauce"]
ROW_KEYS = {"grab_id", "zone", "camera_id", "source", "t_start", "t_end", "item", "action",
            "qty_label", "confidence", "status", "frames"}


@pytest.fixture()
def grabs(tmp_path) -> list[Path]:
    return make_fake_grabs.make(tmp_path / "grabs", "2026-11-14")


@pytest.fixture()
def pkg(grabs) -> GrabPackage:
    return GrabPackage.load(grabs[0])  # pepperoni, took


class RecordingD1(FakeD1):
    """FakeD1 that also remembers what it was asked."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []

    def ask(self, questions, state, images=None):
        self.calls.append((questions, state, images))
        return super().ask(questions, state, images)


# ---------- check_grab decision rules ----------

def test_consistent_answers_are_logged(pkg):
    row, answers = check_grab(pkg, ITEMS, FakeD1(p=0.95))
    assert set(row) == ROW_KEYS
    assert row["status"] == "logged"
    assert (row["action"], row["item"], row["qty_label"]) == ("took", "pepperoni", "pinch")
    assert row["confidence"] == 0.95
    assert row["grab_id"] == pkg.meta.grab_id and row["frames"] == pkg.frames_json()
    assert {a["question_id"] for a in answers} == {"hand_food", "item", "bin_change", "action", "qty"}
    for a in answers:
        assert set(a) == {"question_id", "answer", "probs"}
        assert a["probs"][a["answer"]] == 0.95


def test_bin_change_drives_action_and_conflict_flags(pkg):
    row, _ = check_grab(pkg, ITEMS, FakeD1(p=0.95, overrides={"bin_change": ("fuller", 0.95)}))
    assert row["action"] == "put_back"  # from bin_change, not from the 'action' answer ('took')
    assert row["status"] == "flagged"


def test_bin_change_agrees_put_back_logged(pkg):
    d1 = FakeD1(p=0.95, overrides={"bin_change": ("fuller", 0.9), "action": ("put_back", 0.85)})
    row, _ = check_grab(pkg, ITEMS, d1)
    assert (row["action"], row["status"], row["confidence"]) == ("put_back", "logged", 0.85)


def test_touched_has_no_qty_and_ignores_hand_food(pkg):
    d1 = FakeD1(p=0.95, overrides={"bin_change": ("same", 0.9), "action": ("touched", 0.9),
                                   "hand_food": ("no", 0.9), "qty": ("pinch", 0.3)})
    row, _ = check_grab(pkg, ITEMS, d1)
    assert (row["action"], row["qty_label"], row["status"]) == ("touched", None, "logged")


def test_low_confidence_is_flagged(pkg):
    low = settings.autolog_min_prob - 0.05
    row, _ = check_grab(pkg, ITEMS, FakeD1(p=0.95, overrides={"qty": ("handful", low)}))
    assert row["status"] == "flagged"
    assert row["confidence"] == pytest.approx(low)
    assert row["qty_label"] == "handful"


def test_item_none_is_flagged(pkg):
    row, _ = check_grab(pkg, ITEMS, FakeD1(p=0.95, overrides={"item": ("none", 0.95)}))
    assert row["item"] is None
    assert row["status"] == "flagged"


def test_hand_food_no_is_not_a_veto(pkg):
    row, _ = check_grab(pkg, ITEMS, FakeD1(p=0.95, overrides={"hand_food": ("no", 0.95)}))
    assert row["status"] == "logged"  # clear bin change wins; hand_food is context only


def test_images_are_labelled_by_position(tmp_path):
    folder = make_fake_grabs.make(tmp_path, "2026-11-14")[0]
    for j in range(5, 13):  # 12 during frames: more than d1's 8-image limit allows
        (folder / f"during_{j:02d}.jpg").write_bytes((folder / "during_04.jpg").read_bytes())
    p = GrabPackage.load(folder)
    d1 = RecordingD1()
    check_grab(p, ITEMS, d1)
    (bin_q, bin_state, bin_imgs), (grab_q, grab_state, grab_imgs) = d1.calls
    assert [q.id for q in bin_q] == ["bin_change"]
    assert bin_imgs == [p.before, p.after]
    assert "Image 1 is BEFORE" in bin_state and "Image 2 is AFTER" in bin_state
    assert len(grab_imgs) == MAX_IMAGES
    assert grab_imgs[0] == p.before and grab_imgs[-1] == p.after
    assert grab_imgs[1] == p.during[0] and grab_imgs[-2] == p.during[-1]
    assert "Image 1 is BEFORE" in grab_state and "Images 2 to 7 are DURING" in grab_state
    assert "Image 8 is AFTER" in grab_state
    item_q = next(q for q in grab_q if q.id == "item")
    assert item_q.options == ("pepperoni", "cheese", "dough", "sauce", "none")


# ---------- watcher ----------

def test_watcher_processes_each_package_once(grabs, monkeypatch):
    calls = []
    monkeypatch.setattr("app.ledger.api.record_grab", lambda row, ans: calls.append(row["grab_id"]))
    d = grabs[0].parent

    w1 = GrabWatcher(d, client=FakeD1(), items_fn=lambda: ITEMS, settle_s=0)
    assert len(w1.poll_once()) == len(grabs)
    assert w1.poll_once() == []  # same process, nothing new
    # a restarted watcher reads .processed.json and does nothing
    w2 = GrabWatcher(d, client=FakeD1(), items_fn=lambda: ITEMS, settle_s=0)
    assert w2.poll_once() == []
    assert sorted(calls) == sorted(g.name for g in grabs)
    assert set(json.loads((d / ".processed.json").read_text())) >= {g.name for g in grabs}

    # a new package arriving later is picked up
    more = make_fake_grabs.make(d.parent / "more", "2026-11-15")[0]
    more.rename(d / more.name)
    assert w2.poll_once() == [more.name]


def test_watcher_skips_incomplete_and_retries_failures(grabs, monkeypatch):
    d = grabs[0].parent
    (grabs[1] / "after.jpg").unlink()  # still copying in
    fail = {"on": True}

    def record(row, ans):
        if fail["on"]:
            raise RuntimeError("db down")

    monkeypatch.setattr("app.ledger.api.record_grab", record)
    w = GrabWatcher(d, client=FakeD1(), items_fn=lambda: ITEMS, settle_s=0, backoff_base_s=0)
    assert w.poll_once() == []  # every record failed: nothing marked processed
    assert not (d / ".processed.json").exists()
    assert json.loads((d / ".failed.json").read_text())  # failures are visible
    for _ in range(5):          # keeps retrying: never a permanent skip
        assert w.poll_once() == []
    fail["on"] = False
    done = w.poll_once()
    assert grabs[1].name not in done and len(done) == len(grabs) - 1
    assert json.loads((d / ".failed.json").read_text()) == {}


def test_watcher_backs_off_after_failure(grabs, monkeypatch):
    monkeypatch.setattr("app.ledger.api.record_grab", lambda row, ans: (_ for _ in ()).throw(RuntimeError("x")))
    w = GrabWatcher(grabs[0].parent, client=FakeD1(), items_fn=lambda: ITEMS, settle_s=0, backoff_base_s=60)
    w.poll_once()
    assert w.pending() == []    # all in backoff window


def test_watcher_rejects_half_written_image(grabs, monkeypatch):
    monkeypatch.setattr("app.ledger.api.record_grab", lambda row, ans: None)
    (grabs[0] / "after.jpg").write_bytes((grabs[0] / "after.jpg").read_bytes()[:200])  # truncated JPEG
    w = GrabWatcher(grabs[0].parent, client=FakeD1(), items_fn=lambda: ITEMS, settle_s=0)
    assert grabs[0].name not in w.poll_once()


def test_watcher_quarantines_wrong_shaped_state(grabs, monkeypatch):
    d = grabs[0].parent
    (d / ".processed.json").write_text(json.dumps({grabs[0].name: True}))  # object, not list
    monkeypatch.setattr("app.ledger.api.record_grab", lambda row, ans: None)
    w = GrabWatcher(d, client=FakeD1(), items_fn=lambda: ITEMS, settle_s=0)
    assert grabs[0].name in w.poll_once()  # not silently skipped
    assert list(d.glob(".processed.corrupt-*.json"))


def test_watcher_fails_closed_without_item_list(grabs, monkeypatch):
    calls = []

    def no_db():
        raise OSError("connection refused")

    monkeypatch.setattr("app.ledger.api.record_grab", lambda row, ans: calls.append(row))
    w = GrabWatcher(grabs[0].parent, client=FakeD1(), items_fn=no_db, settle_s=0)
    assert w.poll_once() == [] and calls == []          # never guesses items from zone names
    w.items_fn = lambda: ITEMS
    assert len(w.poll_once()) == len(grabs)             # recovers once the DB is back


def test_watcher_waits_for_folder_to_settle(grabs, monkeypatch):
    monkeypatch.setattr("app.ledger.api.record_grab", lambda row, ans: None)
    w = GrabWatcher(grabs[0].parent, client=FakeD1(), items_fn=lambda: ITEMS, settle_s=60)
    assert w.poll_once() == []  # files were just written


# ---------- Liquid request building / response parsing ----------

Q_NOUL = D1Question(id="hand_food", type="noul", instructions="Is a hand holding food?")
Q_CHOICE = D1Question(id="bin_change", type="choice", instructions="Emptier, fuller or same?",
                      options=("emptier", "fuller", "same"))

# Shape copied from docs.liquid.ai/api/reference/decision-models/make-decisions (200 example)
DOC_RESPONSE = {
    "model": "d1.1",
    "answers": {
        "hand_food": {"type": "noul", "noul": 0.95},
        "bin_change": {"type": "choice", "choice": "emptier",
                       "probabilities": {"emptier": 0.67, "fuller": 0.31, "same": 0.02}, "confidence": 0.5},
    },
    "usage": {"input_tokens": 912, "output_tokens": 0},
}


def test_build_request_matches_documented_body(pkg):
    body = build_request([Q_NOUL, Q_CHOICE], "state text", [pkg.before, pkg.after], model="d1.1")
    assert body["model"] == "d1.1" and body["state"] == "state text"
    assert body["questions"] == {
        "hand_food": {"type": "noul", "instructions": "Is a hand holding food?"},
        "bin_change": {"type": "choice", "instructions": "Emptier, fuller or same?",
                       "criteria": {"emptier": None, "fuller": None, "same": None}},
    }
    assert len(body["images"]) == 2
    head, b64 = body["images"][0].split(",", 1)
    assert head == "data:image/jpeg;base64"
    assert base64.b64decode(b64) == pkg.before.read_bytes()  # small JPEG sent untouched


def test_build_request_dict_state_and_downscale(tmp_path):
    big = tmp_path / "big.png"
    Image.new("RGB", (3000, 2000), (10, 20, 30)).save(big)
    body = build_request([Q_NOUL], {"zone": "cheese"}, [big], max_side=768)
    assert json.loads(body["state"]) == {"zone": "cheese"}
    head, b64 = body["images"][0].split(",", 1)
    assert head == "data:image/jpeg;base64"
    assert max(Image.open(io.BytesIO(base64.b64decode(b64))).size) == 768


def test_build_request_enforces_documented_limits(pkg):
    with pytest.raises(LiquidD1Error, match="at most 8 images"):
        build_request([Q_NOUL], "s", [pkg.before] * 9)
    with pytest.raises(LiquidD1Error, match="2-255 options"):
        build_request([D1Question(id="x", type="choice", instructions="?", options=("only",))], "s")


def test_parse_response_documented_shape():
    out = parse_response([Q_NOUL, Q_CHOICE], DOC_RESPONSE)
    assert out["hand_food"].answer == "yes" and out["hand_food"].probs == {"yes": 0.95, "no": pytest.approx(0.05)}
    assert out["hand_food"].confidence == 0.95
    b = out["bin_change"]
    assert b.answer == "emptier" and b.probs == {"emptier": 0.67, "fuller": 0.31, "same": 0.02}
    assert b.confidence == 0.67  # P(chosen), not the API's spread-based 'confidence' (0.5)


def test_parse_response_noul_no_and_errors():
    out = parse_response([Q_NOUL], {"answers": {"hand_food": {"type": "noul", "noul": 0.2}}})
    assert out["hand_food"].answer == "no" and out["hand_food"].confidence == pytest.approx(0.8)
    with pytest.raises(LiquidD1Error, match="missing an answer"):
        parse_response([Q_NOUL, Q_CHOICE], {"answers": {"hand_food": {"noul": 0.9}}})
    with pytest.raises(LiquidD1Error, match="no 'answers'"):
        parse_response([Q_NOUL], {"error": "x"})


def test_liquid_ask_end_to_end_with_mock_transport(pkg):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=DOC_RESPONSE)

    d1 = LiquidD1(api_key="liquid_test", transport=httpx.MockTransport(handler))
    out = d1.ask([Q_NOUL, Q_CHOICE], "state", [pkg.before])
    assert seen["url"] == API_URL and seen["auth"] == "Bearer liquid_test"
    assert set(seen["body"]["questions"]) == {"hand_food", "bin_change"}
    assert out["bin_change"].answer == "emptier"
    assert d1.last_response["usage"]["input_tokens"] == 912


def test_liquid_errors_are_actionable():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "Invalid API key", "type": "auth"}})

    d1 = LiquidD1(api_key="liquid_bad", transport=httpx.MockTransport(handler))
    with pytest.raises(LiquidD1Error, match="HTTP 401.*LIQUID_API_KEY"):
        d1.ask([Q_NOUL], "s")
    with pytest.raises(LiquidD1Error, match="LIQUID_API_KEY is empty"):
        LiquidD1(api_key="")


def test_check_grab_through_liquid_client(pkg):
    """check_grab -> LiquidD1 -> mocked HTTP: the two requests carry the right questions."""
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        answers = {}
        for qid, q in body["questions"].items():
            if q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.97}
            else:
                opts = list(q["criteria"])
                pick = {"bin_change": "emptier", "action": "took", "qty": "handful"}.get(qid, opts[0])
                probs = {o: (0.92 if o == pick else 0.08 / (len(opts) - 1)) for o in opts}
                answers[qid] = {"type": "choice", "choice": pick, "probabilities": probs, "confidence": 0.8}
        return httpx.Response(200, json={"model": "d1.1", "answers": answers, "usage": {}})

    d1 = LiquidD1(api_key="liquid_test", transport=httpx.MockTransport(handler))
    row, answers = check_grab(pkg, ITEMS, d1)
    assert [set(b["questions"]) for b in bodies] == [{"bin_change"}, {"hand_food", "item", "action", "qty"}]
    assert [len(b["images"]) for b in bodies] == [2, 2 + len(pkg.during)]
    assert (row["action"], row["item"], row["qty_label"], row["status"]) == ("took", "pepperoni", "handful", "logged")
    assert row["confidence"] == 0.92


def test_watcher_module_uses_ledger_api(monkeypatch):
    got = []
    monkeypatch.setattr("app.ledger.api.record_grab", lambda row, ans: got.append(row))
    watcher_mod._record({"grab_id": "g"}, [])
    assert got == [{"grab_id": "g"}]
