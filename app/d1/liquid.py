"""Liquid AI d1 backend for the D1Client protocol (app/d1/base.py).

Everything marked VERIFIED is taken from Liquid's docs (checked 2026-11-13):
  - https://docs.liquid.ai/api/reference/decision-models/make-decisions  (OpenAPI for the endpoint)
  - https://docs.liquid.ai/lfm/models/d1                                 (image inputs, limits)
  - https://www.liquid.ai/blog/d1-decision-model                         (original Python example)
Anything ASSUMED is marked so here and in docs/d1-notes.md.

Request (VERIFIED):
  POST https://api.liquid.ai/decisions/v1/systemone
  Authorization: Bearer $LIQUID_API_KEY
  {"model": "d1.1", "state": "<text>", "images": ["data:image/jpeg;base64,..."],
   "questions": {"<id>": {"type": "noul", "instructions": "..."},
                 "<id>": {"type": "choice", "instructions": "...", "criteria": {"opt": null, ...}}}}
Response (VERIFIED):
  {"model": "...", "answers": {"<id>": {"type": "noul", "noul": 0.95},
                               "<id>": {"type": "choice", "choice": "a",
                                        "probabilities": {"a": 0.9, "b": 0.1}, "confidence": 0.7}},
   "usage": {"input_tokens": 912, "output_tokens": 0}}
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import time
from pathlib import Path

import httpx
from PIL import Image

from app.d1.base import D1Answer, D1Question

# ---- VERIFIED constants (docs URLs above) ----
API_URL = "https://api.liquid.ai/decisions/v1/systemone"
MODELS_URL = "https://api.liquid.ai/decisions/v1/models"
# 'd1.1' is the documented default hosted model and accepts images; 'd1-ultra' is the larger one.
# 'd1' (the Oct 2026 launch id in the blog) is still referenced by the OpenAPI spec.
DEFAULT_MODEL = os.getenv("LIQUID_D1_MODEL", "d1.1")
MAX_IMAGES = 8                  # "Up to 8 images" per request
MAX_BODY_BYTES = 4_500_000      # "4.5 MB request body limit", including base64 image data
MAX_PATCHES = 10_000            # 32x32 patches across all images of a request
MAX_CHOICE_OPTIONS = 255        # Choice criteria: 2-255 options
MAX_QUESTIONS = 128

# ---- Our own choices (not API facts) ----
MAX_SIDE = 768                  # downscale frames so 8 of them fit the patch and body limits
JPEG_QUALITY = 85
_ACCEPTED = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class LiquidD1Error(RuntimeError):
    pass


def _image_data_url(path: Path, max_side: int = MAX_SIDE) -> tuple[str, int]:
    """Return (data URL, 32x32 patch count). Re-encodes as JPEG only if the image is too large
    or not a documented format; otherwise sends the original bytes."""
    raw = Path(path).read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        fmt, (w, h) = im.format, im.size
        if fmt in _ACCEPTED and max(w, h) <= max_side:
            mime = _ACCEPTED[fmt]
        else:
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            w, h = im.size
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=JPEG_QUALITY)
            raw, mime = buf.getvalue(), "image/jpeg"
    patches = math.ceil(w / 32) * math.ceil(h / 32)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}", patches


def build_request(
    questions: list[D1Question],
    state: str | dict | None,
    images: list[Path] | None = None,
    model: str = DEFAULT_MODEL,
    max_side: int = MAX_SIDE,
) -> dict:
    """Build the JSON body for POST /decisions/v1/systemone. Raises LiquidD1Error on any
    documented limit we would break, so we fail before spending a request."""
    if not questions:
        raise LiquidD1Error("d1 request needs at least one question")
    if len(questions) > MAX_QUESTIONS:
        raise LiquidD1Error(f"d1 allows at most {MAX_QUESTIONS} questions per request, got {len(questions)}")
    qs: dict[str, dict] = {}
    for q in questions:
        if q.id in qs:
            raise LiquidD1Error(f"duplicate question id {q.id!r}")
        if q.type == "noul":
            qs[q.id] = {"type": "noul", "instructions": q.instructions}
        elif q.type == "choice":
            if not 2 <= len(q.options) <= MAX_CHOICE_OPTIONS:
                raise LiquidD1Error(f"choice question {q.id!r} needs 2-{MAX_CHOICE_OPTIONS} options, got {len(q.options)}")
            # criteria maps option name -> description or null (VERIFIED). We put option meanings
            # in the instructions text, because D1Question has no per-option descriptions.
            qs[q.id] = {"type": "choice", "instructions": q.instructions,
                        "criteria": {o: None for o in q.options}}
        else:
            raise LiquidD1Error(f"unsupported question type {q.type!r} for {q.id!r}")

    body: dict = {"model": model, "questions": qs}
    if state is not None:
        # The API takes plain text or structured JSON; the documented examples send JSON as a
        # string (json.dumps), so we do the same.
        body["state"] = state if isinstance(state, str) else json.dumps(state, default=str)
    if images:
        if len(images) > MAX_IMAGES:
            raise LiquidD1Error(f"d1 accepts at most {MAX_IMAGES} images per request, got {len(images)}")
        urls, patches = zip(*(_image_data_url(Path(p), max_side) for p in images))
        if sum(patches) > MAX_PATCHES:
            raise LiquidD1Error(f"images cover {sum(patches)} 32x32 patches, over d1's {MAX_PATCHES}; lower max_side")
        body["images"] = list(urls)
    size = len(json.dumps(body))
    if size > MAX_BODY_BYTES:
        raise LiquidD1Error(f"request body is {size} bytes, over d1's {MAX_BODY_BYTES}; lower max_side or send fewer images")
    return body


def parse_response(questions: list[D1Question], data: dict) -> dict[str, D1Answer]:
    """Turn a System One response into {question.id: D1Answer}.

    D1Answer.confidence is the probability of the chosen answer (our contract). That is NOT the
    API's own Choice `confidence`, which measures how peaked the distribution is (0 = flat,
    1 = all mass on one option), so we recompute it from `probabilities`."""
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise LiquidD1Error(f"d1 response has no 'answers' object: {str(data)[:300]}")
    out: dict[str, D1Answer] = {}
    for q in questions:
        a = answers.get(q.id)
        if not isinstance(a, dict):
            raise LiquidD1Error(f"d1 response is missing an answer for question {q.id!r}")
        if q.type == "noul":
            if "noul" not in a:
                raise LiquidD1Error(f"noul answer for {q.id!r} has no 'noul' field: {a}")
            p = float(a["noul"])
            probs = {"yes": p, "no": 1.0 - p}
            ans = "yes" if p >= 0.5 else "no"
        else:
            raw = a.get("probabilities")
            if not isinstance(raw, dict) or not raw:
                raise LiquidD1Error(f"choice answer for {q.id!r} has no 'probabilities': {a}")
            probs = {o: float(raw.get(o, 0.0)) for o in q.options}
            ans = a.get("choice") or max(probs, key=probs.get)
            if ans not in probs:
                raise LiquidD1Error(f"d1 chose {ans!r} for {q.id!r}, which is not one of {list(q.options)}")
        out[q.id] = D1Answer(answer=ans, probs=probs, confidence=probs[ans])
    return out


def _error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else (err or resp.text)
    except Exception:
        msg = resp.text
    hint = {
        401: "check LIQUID_API_KEY (keys start with 'liquid_'; create one at console.liquid.ai > Dashboard > API Keys)",
        403: "the key's organization may not have access to this model; check GET /decisions/v1/models",
        404: f"check API_URL in app/d1/liquid.py (currently {API_URL}) against docs.liquid.ai/api/reference/decision-models/make-decisions",
        413: "request too large: lower MAX_SIDE or send fewer frames (limit 4.5 MB)",
        422: "validation error: check model id (LIQUID_D1_MODEL, default d1.1) accepts images, <=8 images, <=10,000 patches, choice has 2-255 options",
        429: "rate limited: see Retry-After",
    }.get(resp.status_code, "see docs/d1-notes.md")
    return f"Liquid d1 HTTP {resp.status_code}: {str(msg)[:500]} ({hint})"


class LiquidD1:
    """D1Client backed by Liquid's hosted decision models."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        url: str = API_URL,
        timeout: float = 30.0,
        max_side: int = MAX_SIDE,
        transport: httpx.BaseTransport | None = None,
    ):
        if not api_key:
            raise LiquidD1Error("LIQUID_API_KEY is empty: set it in .env (console.liquid.ai > Dashboard > API Keys) or use D1_BACKEND=fake")
        self.model, self.url, self.max_side = model, url, max_side
        self.http = httpx.Client(
            timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        self.last_response: dict | None = None  # raw JSON of the last call, incl. usage

    def ask(self, questions, state, images=None):
        body = build_request(questions, state, images, model=self.model, max_side=self.max_side)
        resp = self.http.post(self.url, json=body)
        if resp.status_code == 429:  # one polite retry if the wait is short
            wait = int(resp.headers.get("Retry-After", "0") or 0)
            if 0 < wait <= 10:
                time.sleep(wait)
                resp = self.http.post(self.url, json=body)
        if resp.status_code != 200:
            raise LiquidD1Error(_error_message(resp))
        data = resp.json()
        self.last_response = data
        return parse_response(questions, data)

    def list_models(self) -> list[dict]:
        """GET /decisions/v1/models: [{name, description, release_date, input_modalities}]."""
        resp = self.http.get(MODELS_URL)
        if resp.status_code != 200:
            raise LiquidD1Error(_error_message(resp))
        return resp.json().get("models", [])
