"""Test-wide settings. Unit tests never send traces to LangSmith or call paid APIs by default."""
import os

os.environ["LANGSMITH_TRACING"] = "false"     # set before app.config loads .env (dotenv won't override)
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["WATCHER_IN_SERVER"] = "false"     # tests drive the watcher directly
os.environ["PLAN_TIME"] = ""                  # no daily reorder-plan thread in tests
