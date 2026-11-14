"""d1 through OpenRouter's System One / Decisions API (one OpenRouter key for everything).

Verified from OpenRouter docs (Nov 13 2026):
- https://openrouter.ai/liquid/d1: model id `liquid/d1`, input text+image, output decisions,
  "the same /v1/systemone schema as other OpenRouter Decisions models", $0.02/M input.
  The page says d1 is "Going away November 20, 2026" (after the hackathon).
- https://openrouter.ai/docs/guides/community/typesafe-sdk: POST https://openrouter.ai/api/v1/systemone,
  `Authorization: Bearer <OpenRouter key>`.
- https://openrouter.ai/docs/client-sdks/python/sdks/systemone/README.md: images go in `assets`
  ({key: {data_base64, format, type}}). For non-TypeSafe providers each whole-string reference is
  resolved to an image_url part, and is accepted only as a TOP-LEVEL `state` item. So `state`
  becomes a list: [text, "img_0", "img_1", ...].
Unverified until a real key is used: the exact `type` value for an asset (we send "image").
"""
from __future__ import annotations

import os
import time

from app.d1.liquid import LiquidD1, LiquidD1Error, build_request

OPENROUTER_URL = "https://openrouter.ai/api/v1/systemone"
RETRY_STATUS = {429, 500, 502, 503, 504, 524, 529}
OPENROUTER_D1_MODEL = os.getenv("OPENROUTER_D1_MODEL", "liquid/d1")


def to_openrouter(body: dict) -> dict:
    """Convert a Liquid-style body (top-level `images` data URLs) to OpenRouter's `assets` form."""
    body = dict(body)
    urls = body.pop("images", None)
    if not urls:
        return body
    assets, refs = {}, []
    for i, url in enumerate(urls):
        head, _, data = url.partition(",")
        fmt = head.split("/")[-1].split(";")[0].lower()  # data:image/jpeg;base64 -> jpeg
        if fmt not in ("jpeg", "png"):
            raise LiquidD1Error(f"OpenRouter assets accept PNG or JPEG only, got {fmt!r}")
        key = f"img_{i}"
        assets[key] = {"data_base64": data, "format": fmt, "type": "image"}
        refs.append(key)
    text = body.get("state")
    body["state"] = ([text] if text else []) + refs
    body["assets"] = assets
    return body


class OpenRouterD1(LiquidD1):
    def __init__(self, api_key: str, model: str = OPENROUTER_D1_MODEL, **kw):
        if not api_key:
            raise LiquidD1Error("OPENROUTER_API_KEY is empty: create one at openrouter.ai/settings/keys")
        super().__init__(api_key, model=model, url=OPENROUTER_URL, **kw)

    def ask(self, questions, state, images=None):
        from app.d1.liquid import _error_message, parse_response

        body = to_openrouter(build_request(questions, state, images, model=self.model, max_side=self.max_side))
        resp = None
        for wait in (0, 1.5, 4.0):  # retry gateway/overload errors: seen live (504) on Nov 13
            if wait:
                time.sleep(wait)
            try:
                resp = self.http.post(self.url, json=body)
            except Exception as e:  # network blip / timeout
                if wait == 4.0:
                    raise LiquidD1Error(f"OpenRouter d1 unreachable: {e}") from e
                continue
            if resp.status_code not in RETRY_STATUS:
                break
        if resp.status_code != 200:
            raise LiquidD1Error(_error_message(resp).replace("Liquid d1", "OpenRouter d1"))
        data = resp.json()
        self.last_response = data
        return parse_response(questions, data)
