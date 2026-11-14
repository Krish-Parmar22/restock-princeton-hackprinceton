"""Client for the agent API on :8000 (owned by app/, contract in docs/arnav-shah.md).

`Api` talks to the real service. `DemoApi` serves the same shapes from local demo data
(see demo.py) so the dashboard and Excel export work before the API or Postgres is up.
"""
from __future__ import annotations

import os
from datetime import date

import httpx

API_URL = os.getenv("API_URL", "http://localhost:8000")


class ApiError(RuntimeError):
    pass


class Api:
    demo = False

    def __init__(self, base_url: str = API_URL, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=base_url, timeout=15, transport=transport)

    def _get(self, path: str, **params):
        try:
            r = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as e:
            raise ApiError(f"can't reach the agent API at {self.http.base_url}: {e}") from e
        if r.status_code != 200:
            raise ApiError(f"GET {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def healthy(self) -> bool:
        try:
            return self.http.get("/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def inventory(self) -> list[dict]:
        return self._get("/api/inventory")

    def variance(self, start: date, end: date) -> list[dict]:
        return self._get("/api/variance", **{"from": start.isoformat(), "to": end.isoformat()})

    def grabs(self, day: date, status: str = "all") -> list[dict]:
        return self._get("/api/grabs", date=day.isoformat(), status=status)

    def messages(self, limit: int = 50) -> list[dict]:
        return self._get("/api/messages", limit=limit)

    def orders(self, limit: int = 30) -> list[dict]:
        return self._get("/api/orders", limit=limit)

    def reorder_plan(self) -> dict:
        return self._get("/api/reorder-plan")

    def send_briefing(self) -> dict:
        try:
            r = self.http.post("/api/briefing/send", timeout=20)
        except httpx.HTTPError as e:
            raise ApiError(f"can't reach the agent API: {e}") from e
        if r.status_code != 200:
            raise ApiError(f"send briefing -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def send_plan(self) -> list[dict]:
        """Ask the agent to text the owner today's reorder plan (they approve by replying yes)."""
        try:
            r = self.http.post("/api/reorder-plan/send", timeout=120)
        except httpx.HTTPError as e:
            raise ApiError(f"can't reach the agent API: {e}") from e
        if r.status_code != 200:
            raise ApiError(f"send plan -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def confirm(self, grab_id: str, action: str, item: str, qty_label: str) -> dict:
        try:
            r = self.http.post(f"/api/grabs/{grab_id}/confirm",
                               json={"action": action, "item": item, "qty_label": qty_label})
        except httpx.HTTPError as e:
            raise ApiError(f"can't reach the agent API: {e}") from e
        if r.status_code != 200:
            raise ApiError(f"confirm {grab_id} -> {r.status_code}: {r.text[:200]}")
        return r.json()


def connect(base_url: str = API_URL, demo: bool | None = None):
    """Real API if it answers /health, else demo data. DASHBOARD_DEMO=1 forces demo, =0 forbids it."""
    if demo is None:
        flag = os.getenv("DASHBOARD_DEMO", "")
        demo = True if flag == "1" else False if flag == "0" else None
    if demo:
        from demo import DemoApi
        return DemoApi()
    api = Api(base_url)
    if demo is None and not api.healthy():
        from demo import DemoApi
        return DemoApi()
    return api
