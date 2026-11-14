"""One real request to Liquid's d1, to prove the key, endpoint and payload work.

Needs LIQUID_API_KEY in .env or the environment (console.liquid.ai > Dashboard > API Keys).
Costs a fraction of a cent: two requests over one fake grab package (~6 small images).

Usage:
  uv run python scripts/d1_smoke.py                       # fake grab, default model (d1.1)
  uv run python scripts/d1_smoke.py --model d1-ultra
  uv run python scripts/d1_smoke.py --folder grabs/2026-11-14T12-14-03_pepperoni
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.config import settings  # noqa: E402
from app.contracts import GrabPackage  # noqa: E402
from app.d1.base import D1Question  # noqa: E402
from app.d1.liquid import DEFAULT_MODEL, LiquidD1, LiquidD1Error  # noqa: E402
from app.grabs.checks import build_questions, decide  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--folder", type=Path, help="a grab package folder (default: generate a fake one)")
    ap.add_argument("--items", default="pepperoni,cheese,dough,sauce")
    a = ap.parse_args()

    if not settings.liquid_api_key:
        print("LIQUID_API_KEY is not set. Add it to .env (console.liquid.ai > Dashboard > API Keys).")
        return 2
    d1 = LiquidD1(settings.liquid_api_key, model=a.model)

    try:
        # 1. Which models can this key call, and do they take images?
        models = d1.list_models()
        print("models:", [(m.get("name"), m.get("input_modalities")) for m in models])
        if models and not any(m.get("name") == a.model for m in models):
            print(f"WARNING: {a.model!r} is not in this key's model list; pass --model with one of the names above")

        # 2. Text-only sanity check.
        q = D1Question(id="is_food", type="noul", instructions="Is the state describing a food ingredient?")
        print("text noul:", d1.ask([q], "A tub of sliced pepperoni on a pizza make-line.")["is_food"])

        # 3. A full grab package, same two requests check_grab makes.
        folder = a.folder
        if folder is None:
            import make_fake_grabs

            folder = make_fake_grabs.make(Path(tempfile.mkdtemp()), "2026-11-14")[0]
        pkg = GrabPackage.load(folder)
        answers = {}
        for req in build_questions(pkg, a.items.split(",")):
            answers.update(d1.ask(req["questions"], req["state"], req["images"]))
            print(f"request: {len(req['images'])} images, usage {d1.last_response.get('usage')}")
    except LiquidD1Error as e:
        print(f"FAILED: {e}")
        return 1

    for qid, ans in answers.items():
        print(f"  {qid:10s} {ans.answer:16s} p={ans.confidence:.3f}  {json.dumps({k: round(v, 3) for k, v in ans.probs.items()})}")
    d = decide(answers, settings.autolog_min_prob)
    print("decision:", d)
    truth = json.loads((folder / "meta.json").read_text()).get("_truth")
    if truth:
        print("ground truth:", truth, "-> action", "MATCH" if truth["action"] == d["action"] else "MISMATCH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
