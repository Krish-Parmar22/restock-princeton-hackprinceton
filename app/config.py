"""Settings from environment (.env). Every module reads config from here."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "postgresql://localhost:5432/pizza")
    grabs_dir: Path = Path(os.getenv("GRABS_DIR", str(ROOT / "grabs"))).resolve()
    d1_backend: str = os.getenv("D1_BACKEND", "fake")  # 'openrouter' | 'liquid' | 'fake'
    liquid_api_key: str = os.getenv("LIQUID_API_KEY", "")
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    agent_model: str = os.getenv("AGENT_MODEL", "")
    fallback_model: str = os.getenv("FALLBACK_MODEL", "anthropic:claude-sonnet-5-5")
    yolo_enabled: bool = os.getenv("YOLO_ENABLED", "true").lower() in ("1", "true", "yes")
    upload_token: str = os.getenv("UPLOAD_TOKEN", "")
    watcher_in_server: bool = os.getenv("WATCHER_IN_SERVER", "true").lower() in ("1", "true", "yes")
    bridge_url: str = os.getenv("BRIDGE_URL", "http://localhost:8787")
    # d1 decision thresholds (see docs/architecture.md)
    autolog_min_prob: float = float(os.getenv("AUTOLOG_MIN_PROB", "0.75"))
    guard_send_min: float = float(os.getenv("GUARD_SEND_MIN", "0.85"))
    guard_soften_min: float = float(os.getenv("GUARD_SOFTEN_MIN", "0.65"))
    # How d1 is asked: "auto" (per box: counted items like cans get exact counts, bulk food gets
    # pinch/handful), or force "pizza" / "drinks" for every box.
    scene: str = os.getenv("SCENE", "auto")
    # Where the live Excel sheet is written after every change (the owner opens this file).
    sheet_path: Path = Path(os.getenv("SHEET_PATH", str(ROOT / "exports" / "Inventory.xlsx"))).resolve()
    # Daily reorder-plan text at this shop-local time (America/New_York), "HH:MM". "" = off.
    plan_time: str = os.getenv("PLAN_TIME", "")


settings = Settings()
