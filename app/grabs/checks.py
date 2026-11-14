"""Stage 3: turn one grab package into a ledger row using d1 (see docs/architecture.md).

Two d1 requests per grab, because every question in a request sees every image:
  1. bin_change sees only [BEFORE, AFTER], so the primary signal is a clean two-image comparison.
  2. hand_food, item, action and qty see [BEFORE, DURING..., AFTER] in time order.
The state text of each request says which image position is which.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PIL import Image

from app.config import settings

if TYPE_CHECKING:
    from app.grabs.yolo import HandCheck

from app.contracts import GrabPackage
from app.d1.base import D1Answer, D1Client, D1Question

log = logging.getLogger("grabs.checks")

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


SCENES = {
    "pizza": dict(
        place="ingredient bin on a pizza make-line", camera="fixed kitchen camera", bin="bin",
        thing="ingredient", holding="food, such as a piece or handful of an ingredient",
        qty="'pinch': a few pieces or a small pinch. 'handful': about a full handful. "
            "'whole_container': the whole bin or container was lifted out or emptied.",
        qty_options=("pinch", "handful", "whole_container")),
    "drinks": dict(
        place="drinks fridge or shelf at a restaurant", camera="fixed camera", bin="shelf",
        thing="drink", holding="a drink, such as a can or bottle",
        qty="Count the cans or bottles. '1', '2' or '3': exactly that many were moved. "
            "'more': more than three, a whole pack, or you can't tell the exact count.",
        qty_options=("1", "2", "3", "more")),   # exact counts; 'more' has no fixed qty -> owner reviews
}


def scene_for(zone: str) -> str:
    """'drinks' (exact counts) for items counted in units, 'pizza' (pinch/handful) for bulk food.
    SCENE=drinks|pizza forces one wording for every box; SCENE=auto (default) decides per box, so a
    cheese bowl and a Sprite shelf can be watched at the same time."""
    if settings.scene in SCENES:
        return settings.scene
    try:
        from app.ledger.impl import _conn
        with _conn() as c:
            r = c.execute("SELECT qty_per_label FROM items WHERE name = %s", (zone,)).fetchone()
        return "drinks" if r and "1" in (r["qty_per_label"] or {}) else "pizza"
    except Exception:
        return "pizza"


def build_questions(pkg: GrabPackage, items: list[str]) -> tuple[dict, dict]:
    """Return the two requests as dicts {questions, state, images}."""
    w = SCENES[scene_for(pkg.meta.zone)]
    zone = pkg.meta.zone
    during = _sample(pkg.during, MAX_DURING)
    n_during = len(during)
    last = n_during + 2
    during_span = "image 2" if n_during == 1 else f"images 2 to {n_during + 1}"
    item_list = ", ".join(o for o in _item_options(items, zone) if o != NONE_ITEM)

    bin_req = {
        "state": (
            f"Two photos of the same {w['place']}, from a {w['camera']}. "
            f"The camera zone is labelled '{zone}'. "
            f"Image 1 is BEFORE: taken just before a hand reached into the {w['bin']}. "
            f"Image 2 is AFTER: taken just after the hand left."
        ),
        "images": [pkg.before, pkg.after],
        "questions": [
            D1Question(
                id="bin_change", type="choice", options=("emptier", "fuller", "same"),
                instructions=(
                    f"Compare the {w['bin']} in image 2 (AFTER) with the {w['bin']} in image 1 (BEFORE). "
                    f"Answer 'emptier' if image 2 shows less {w['thing']} in the {w['bin']} than image 1, "
                    f"'fuller' if image 2 shows more {w['thing']} than image 1, "
                    f"or 'same' if the amount of {w['thing']} looks unchanged."
                ),
            ),
        ],
    }

    grab_req = {
        "state": (
            f"Photos of one grab at a {w['place']}, from a {w['camera']}, "
            f"in time order. Image 1 is BEFORE the grab (no hand). {during_span.capitalize()} are DURING "
            f"the grab (a hand is in or over the {w['bin']}). Image {last} is AFTER the grab (hand gone). "
            f"The camera zone is labelled '{zone}'; the label is a hint and may be wrong, so judge from "
            f"the images. The shop's {w['thing']}s are: {item_list}."
        ),
        "images": [pkg.before, *during, pkg.after],
        "questions": [
            D1Question(
                id="hand_food", type="noul",
                instructions=(
                    f"Look at the DURING images ({during_span}). Is a hand visibly holding "
                    f"{w['holding']}, in any of them?"
                ),
            ),
            D1Question(
                id="item", type="choice", options=_item_options(items, zone),
                instructions=(
                    f"Which {w['thing']} did the hand handle during the grab ({during_span})? "
                    f"The camera zone label '{zone}' is only context; answer from what the images show. "
                    f"Answer 'none' if no {w['thing']} was handled or none of the options fits."
                ),
            ),
            D1Question(
                id="action", type="choice", options=("took", "put_back", "touched"),
                instructions=(
                    f"What did the hand do to the {w['bin']}, comparing image 1 (BEFORE), the DURING images "
                    f"({during_span}) and image {last} (AFTER)? 'took': removed {w['thing']} from the {w['bin']}. "
                    f"'put_back': returned {w['thing']} to the {w['bin']}. 'touched': reached in or touched the "
                    f"{w['thing']} without removing or adding any."
                ),
            ),
            D1Question(
                id="qty", type="choice", options=w["qty_options"],
                instructions=(
                    f"How much {w['thing']} did the hand move during the grab ({during_span})? "
                    f"{w['qty']}"
                ),
            ),
        ],
    }
    return bin_req, grab_req


def decide(answers: dict[str, D1Answer], min_prob: float, zone: str | None = None) -> dict:
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
    elif zone and item != zone:  # the box says one ingredient, d1 saw another: owner decides
        reasons.append(f"zone is '{zone}' but d1 saw '{item}'")

    qty_label = None
    if action in ("took", "put_back"):
        qty_label = answers["qty"].answer
        deciding["qty"] = answers["qty"]
        # hand_food is recorded for context but is NOT a veto: the before/after bin change is the
        # primary signal, and a utensil or an occluded hand shouldn't flag a clear bin change.

    # Thresholds tuned on real d1 runs (Nov 13, docs/struggles.md): the before/after bin change and
    # the ingredient are the facts that move inventory, so they must be confident. The action
    # answer only has to AGREE with the bin change (checked above), and the amount is inherently
    # fuzzy, so it needs QTY_MIN_PROB, not min_prob.
    need = {"bin_change": min_prob, "item": min_prob}
    if "qty" in deciding:
        need["qty"] = QTY_MIN_PROB
    confidence = round(min(deciding[q].confidence for q in need), 4)
    low = [f"{q}={deciding[q].confidence:.2f}<{t:.2f}" for q, t in need.items() if deciding[q].confidence < t]
    if low:
        reasons.append(f"low confidence: {', '.join(low)}")

    return {
        "action": action, "item": item, "qty_label": qty_label, "confidence": confidence,
        "status": "flagged" if reasons else "logged", "reasons": reasons,
    }


QTY_MIN_PROB = 0.5
CROP_MARGIN = 0.35  # extra context around the bin on each side (fraction of the bin's size)


def crop_to_zone(pkg: GrabPackage, out_dir: Path) -> GrabPackage:
    """Copy of `pkg` whose frames are cropped to the bin's zone (+ margin), so d1 compares the bin
    itself instead of a ~100 px patch of a busy full frame. No zone_polygon: unchanged."""
    poly = pkg.meta.zone_polygon
    if not poly:
        return pkg
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    mx, my = (x1 - x0) * CROP_MARGIN, (y1 - y0) * CROP_MARGIN

    def crop(path: Path) -> Path:
        with Image.open(path) as im:
            w, h = im.size
            box = (max(0, int(x0 - mx)), max(0, int(y0 - my)), min(w, int(x1 + mx)), min(h, int(y1 + my)))
            if box[2] - box[0] < 16 or box[3] - box[1] < 16:
                return path  # polygon is off-frame or degenerate: send the full frame
            out = out_dir / path.name
            im.crop(box).convert("RGB").save(out, quality=92)
            return out

    return pkg.model_copy(update={"before": crop(pkg.before), "during": [crop(p) for p in pkg.during],
                                  "after": crop(pkg.after)})


def check_grab(pkg: GrabPackage, items: list[str], client: D1Client,
               hand_check: Callable[[GrabPackage], "HandCheck"] | None = None) -> tuple[dict, list[dict]]:
    """Ask d1 about one grab package, then (optionally) double-check with YOLO-World that a hand
    was really inside the bin's zone. Returns (row, d1_answers) for app.ledger.api.record_grab."""
    answers: dict[str, D1Answer] = {}
    with tempfile.TemporaryDirectory(prefix="d1crop-") as td:
        d1_pkg = crop_to_zone(pkg, Path(td))  # d1 sees the bin up close; YOLO + the ledger keep originals
        for req in build_questions(d1_pkg, items):
            answers.update(client.ask(req["questions"], req["state"], req["images"]))

    d = decide(answers, settings.autolog_min_prob, zone=pkg.meta.zone)
    yolo = None
    if hand_check is not None:
        try:
            yolo = hand_check(pkg)
        except Exception as e:  # YOLO is a double-check: if it breaks, d1's answer stands, but say so
            log.warning("YOLO check failed for %s: %s", pkg.meta.grab_id, e)
        if yolo is not None and d["action"] in ("took", "put_back") and not yolo.hand_seen:
            d["reasons"].append(f"d1 says {d['action']} but YOLO saw no hand in the {pkg.meta.zone} zone")
            d["status"] = "flagged"
    m = pkg.meta
    row = {
        "grab_id": m.grab_id, "zone": m.zone, "camera_id": m.camera_id, "source": m.source,
        "t_start": m.t_start, "t_end": m.t_end,
        "item": d["item"], "action": d["action"], "qty_label": d["qty_label"],
        "confidence": d["confidence"], "status": d["status"],
        "frames": pkg.frames_json(),
    }
    d1_answers = [{"question_id": qid, "answer": a.answer, "probs": a.probs} for qid, a in answers.items()]
    if yolo is not None:  # stored next to d1's answers so the dashboard/trace can show both votes
        d1_answers.append({"question_id": "yolo_hand_in_zone", "answer": "yes" if yolo.hand_seen else "no",
                           "probs": {"best_conf": yolo.best_conf, "frames_with_hand": yolo.frames_with_hand_in_zone,
                                     "frames_checked": yolo.frames_checked}})
    return row, d1_answers
