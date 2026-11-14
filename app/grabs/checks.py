"""Stage 3: turn one grab package into a ledger row using d1 (see docs/architecture.md).

Two d1 requests per grab, because every question in a request sees every image:
  1. bin_change sees only [BEFORE, AFTER], so the primary signal is a clean two-image comparison.
  2. hand_food, item, action and qty see [BEFORE, DURING..., AFTER] in time order.
The state text of each request says which image position is which.
"""
from __future__ import annotations

from app.config import settings
from app.contracts import GrabPackage
from app.d1.base import D1Answer, D1Client, D1Question

MAX_DURING = 6  # d1 takes <= 8 images per request: BEFORE + up to 6 DURING + AFTER
BIN_TO_ACTION = {"emptier": "took", "fuller": "put_back", "same": "touched"}
NONE_ITEM = "none"


def _sample(frames: list, k: int) -> list:
    """Up to k frames, evenly spaced, always keeping the first and last."""
    if len(frames) <= k:
        return list(frames)
    if k == 1:
        return [frames[len(frames) // 2]]
    n = len(frames) - 1
    return [frames[round(i * n / (k - 1))] for i in range(k)]


def _item_options(items: list[str], zone: str) -> tuple[str, ...]:
    opts = list(dict.fromkeys(i for i in items if i and i != NONE_ITEM))
    if zone in opts:  # zone first: harmless for d1, and makes FakeD1's default answer realistic
        opts.remove(zone)
        opts.insert(0, zone)
    return (*opts, NONE_ITEM)


def build_questions(pkg: GrabPackage, items: list[str]) -> tuple[dict, dict]:
    """Return the two requests as dicts {questions, state, images}."""
    zone = pkg.meta.zone
    during = _sample(pkg.during, MAX_DURING)
    n_during = len(during)
    last = n_during + 2
    during_span = "image 2" if n_during == 1 else f"images 2 to {n_during + 1}"
    item_list = ", ".join(o for o in _item_options(items, zone) if o != NONE_ITEM)

    bin_req = {
        "state": (
            f"Two photos of the same ingredient bin on a pizza make-line, from a fixed kitchen camera. "
            f"The camera zone is labelled '{zone}'. "
            f"Image 1 is BEFORE: taken just before a hand reached into the bin. "
            f"Image 2 is AFTER: taken just after the hand left."
        ),
        "images": [pkg.before, pkg.after],
        "questions": [
            D1Question(
                id="bin_change", type="choice", options=("emptier", "fuller", "same"),
                instructions=(
                    "Compare the bin in image 2 (AFTER) with the bin in image 1 (BEFORE). "
                    "Answer 'emptier' if image 2 shows less ingredient in the bin than image 1, "
                    "'fuller' if image 2 shows more ingredient than image 1, "
                    "or 'same' if the amount of ingredient looks unchanged."
                ),
            ),
        ],
    }

    grab_req = {
        "state": (
            f"Photos of one grab at an ingredient bin on a pizza make-line, from a fixed kitchen camera, "
            f"in time order. Image 1 is BEFORE the grab (no hand). {during_span.capitalize()} are DURING "
            f"the grab (a hand is in or over the bin). Image {last} is AFTER the grab (hand gone). "
            f"The camera zone is labelled '{zone}'; the label is a hint and may be wrong, so judge from "
            f"the images. The shop's ingredients are: {item_list}."
        ),
        "images": [pkg.before, *during, pkg.after],
        "questions": [
            D1Question(
                id="hand_food", type="noul",
                instructions=(
                    f"Look at the DURING images ({during_span}). Is a hand visibly holding food, "
                    f"such as a piece or handful of an ingredient, in any of them?"
                ),
            ),
            D1Question(
                id="item", type="choice", options=_item_options(items, zone),
                instructions=(
                    f"Which ingredient did the hand handle during the grab ({during_span})? "
                    f"The camera zone label '{zone}' is only context; answer from what the images show. "
                    f"Answer 'none' if no ingredient was handled or none of the options fits."
                ),
            ),
            D1Question(
                id="action", type="choice", options=("took", "put_back", "touched"),
                instructions=(
                    f"What did the hand do to the bin, comparing image 1 (BEFORE), the DURING images "
                    f"({during_span}) and image {last} (AFTER)? 'took': removed ingredient from the bin. "
                    f"'put_back': returned ingredient to the bin. 'touched': reached in or touched the "
                    f"ingredient without removing or adding any."
                ),
            ),
            D1Question(
                id="qty", type="choice", options=("pinch", "handful", "whole_container"),
                instructions=(
                    f"How much ingredient did the hand move during the grab ({during_span})? "
                    f"'pinch': a few pieces or a small pinch. 'handful': about a full handful. "
                    f"'whole_container': the whole bin or container was lifted out or emptied."
                ),
            ),
        ],
    }
    return bin_req, grab_req


def decide(answers: dict[str, D1Answer], min_prob: float) -> dict:
    """Apply the decision rules to d1's answers. Returns action, item, qty_label, confidence,
    status and the reasons a grab was flagged (empty when logged)."""
    bin_ans = answers["bin_change"]
    action = BIN_TO_ACTION[bin_ans.answer]
    reasons: list[str] = []
    deciding = {"bin_change": bin_ans, "action": answers["action"], "item": answers["item"]}

    if answers["action"].answer != action:
        reasons.append(f"bin_change={bin_ans.answer} implies {action}, but action={answers['action'].answer}")

    item = answers["item"].answer
    if item == NONE_ITEM:
        item = None
        reasons.append("item=none")

    qty_label = None
    if action in ("took", "put_back"):
        qty_label = answers["qty"].answer
        deciding["qty"] = answers["qty"]
        # hand_food is recorded for context but is NOT a veto: the before/after bin change is the
        # primary signal, and a utensil or an occluded hand shouldn't flag a clear bin change.

    confidence = round(min(a.confidence for a in deciding.values()), 4)
    low = [f"{qid}={a.confidence:.2f}" for qid, a in deciding.items() if a.confidence < min_prob]
    if low:
        reasons.append(f"below {min_prob:.2f}: {', '.join(low)}")

    return {
        "action": action, "item": item, "qty_label": qty_label, "confidence": confidence,
        "status": "flagged" if reasons else "logged", "reasons": reasons,
    }


def check_grab(pkg: GrabPackage, items: list[str], client: D1Client) -> tuple[dict, list[dict]]:
    """Ask d1 about one grab package. Returns (row, d1_answers) for app.ledger.api.record_grab."""
    answers: dict[str, D1Answer] = {}
    for req in build_questions(pkg, items):
        answers.update(client.ask(req["questions"], req["state"], req["images"]))

    d = decide(answers, settings.autolog_min_prob)
    m = pkg.meta
    row = {
        "grab_id": m.grab_id, "zone": m.zone, "camera_id": m.camera_id, "source": m.source,
        "t_start": m.t_start, "t_end": m.t_end,
        "item": d["item"], "action": d["action"], "qty_label": d["qty_label"],
        "confidence": d["confidence"], "status": d["status"],
        "frames": pkg.frames_json(),
    }
    d1_answers = [{"question_id": qid, "answer": a.answer, "probs": a.probs} for qid, a in answers.items()]
    return row, d1_answers
