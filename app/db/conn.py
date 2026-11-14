"""Postgres connection helpers (psycopg 3)."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from app.config import settings

SCHEMA = Path(__file__).with_name("schema.sql")


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    """Yield a connection with dict rows; commits on success, rolls back on error."""
    with psycopg.connect(url or settings.database_url, row_factory=dict_row) as conn:
        yield conn


def apply_schema(url: str | None = None) -> None:
    with connect(url) as conn:
        conn.execute(SCHEMA.read_text())
