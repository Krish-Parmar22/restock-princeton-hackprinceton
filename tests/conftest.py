"""Test-wide settings. Unit tests never send traces to LangSmith or call paid APIs by default."""
import os

os.environ["LANGSMITH_TRACING"] = "false"     # set before app.config loads .env (dotenv won't override)
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["WATCHER_IN_SERVER"] = "false"     # tests drive the watcher directly
os.environ["PLAN_TIME"] = ""                  # no daily reorder-plan thread in tests


import psycopg  # noqa: E402
import pytest  # noqa: E402

_SAFE_DB = "postgresql://localhost:5432/pizza_test_default"


@pytest.fixture(autouse=True, scope="session")
def _never_touch_the_live_database():
    """Every test defaults to a throwaway database. A test that forgot its own DB fixture used to
    write into the live 'pizza' database (a fake chat 'c' showed up on the owner's dashboard)."""
    from app.db.conn import apply_schema
    from app.ledger import impl
    try:
        with psycopg.connect("postgresql://localhost:5432/postgres", autocommit=True) as c:
            c.execute("DROP DATABASE IF EXISTS pizza_test_default WITH (FORCE)")
            c.execute("CREATE DATABASE pizza_test_default")
        apply_schema(_SAFE_DB)
        impl.use_database(_SAFE_DB)
    except Exception:
        pass   # no Postgres: DB tests skip/fail on their own
    yield
    impl.use_database(None)
