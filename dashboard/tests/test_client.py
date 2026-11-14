import json
from datetime import date

import httpx
import pytest

from client import Api, ApiError, connect


def handler(req: httpx.Request) -> httpx.Response:
    p, q = req.url.path, dict(req.url.params)
    if p == "/health":
        return httpx.Response(200, json={"ok": True})
    if p == "/api/variance":
        return httpx.Response(200, json=[{"q": q}])
    if p == "/api/grabs":
        return httpx.Response(200, json=[{"q": q}])
    if p == "/api/messages":
        return httpx.Response(200, json=[{"q": q}])
    if p.endswith("/confirm"):
        if "missing" in p:
            return httpx.Response(404, json={"detail": "no such grab"})
        return httpx.Response(200, json={"body": json.loads(req.content), "path": p})
    return httpx.Response(500, text="boom")


@pytest.fixture
def api():
    return Api("http://test", transport=httpx.MockTransport(handler))


def test_query_params_match_contract(api):
    assert api.variance(date(2026, 11, 8), date(2026, 11, 14))[0]["q"] == {"from": "2026-11-08", "to": "2026-11-14"}
    assert api.grabs(date(2026, 11, 14), "flagged")[0]["q"] == {"date": "2026-11-14", "status": "flagged"}
    assert api.messages(10)[0]["q"] == {"limit": "10"}


def test_confirm(api):
    r = api.confirm("2026-11-14T12-14-03_pepperoni", "took", "pepperoni", "handful")
    assert r["body"] == {"action": "took", "item": "pepperoni", "qty_label": "handful"}
    assert r["path"] == "/api/grabs/2026-11-14T12-14-03_pepperoni/confirm"
    with pytest.raises(ApiError, match="404"):
        api.confirm("missing", "took", "pepperoni", "handful")


def test_errors_are_readable(api):
    with pytest.raises(ApiError, match="500"):
        api.inventory()
    with pytest.raises(ApiError, match="can't reach"):
        Api("http://127.0.0.1:9").inventory()


def test_connect_falls_back_to_demo_only_when_allowed():
    assert connect("http://127.0.0.1:9").demo is True
    assert connect("http://127.0.0.1:9", demo=False).demo is False
