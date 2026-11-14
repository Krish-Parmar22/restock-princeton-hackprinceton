# The agent (LangChain deepagents)

One `create_deep_agent(...)` call in `build.py`:

| Piece | How |
|---|---|
| Planning | `TodoListMiddleware` (opt-in since deepagents 0.7) |
| d1 fact-check | `D1GuardMiddleware` (`guard.py`) wraps `send_text`: claims → d1 yes/no each → send / soften / block, max 2 blocks then a safe fallback. Every decision is recorded in `messages` |
| Fallback model | `ModelFallbackMiddleware` → `FALLBACK_MODEL` (skipped if its key is missing) |
| Subagents | `analyst` (variance + clips), `purchasing` (reorders vs par) |
| Owner approval | `interrupt_on={"place_order": True}`; the owner's "yes"/"no" text resumes it (`runtime.py`) |
| Memory | `/memories/` → Postgres store (par levels, owner corrections) |
| Persistence | Postgres checkpointer: paused approvals survive restarts. Thread = Photon `chat_id` |

Tools (`tools.py`): `get_stock`, `get_variance`, `list_grabs`, `d1_look`, `send_text`, `place_order`. All numbers come from the ledger.

## Run with a real model
`.env`:
```
AGENT_MODEL=groq:openai/gpt-oss-120b        # or ollama:qwen3:8b (needs `uv sync --extra ollama` + Ollama running)
GROQ_API_KEY=...                            # `uv sync --extra groq`
FALLBACK_MODEL=anthropic:claude-sonnet-5-5  # optional; needs ANTHROPIC_API_KEY
D1_BACKEND=liquid                           # or fake
LIQUID_API_KEY=...
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=princeton-hacks
```
Then:
```bash
scripts/db_up.sh && uv run python scripts/seed_demo.py --reset
uv run python scripts/chat_cli.py                         # talk to it in the terminal
uv run uvicorn app.api.server:app --port 8000             # what Arnav's bridge calls
```
Without `AGENT_MODEL`, `/inbound` echoes, so the bridge always works.

## Tests
`uv run pytest tests/test_agent.py -q`: the real deepagents graph, driven by a scripted fake model. No keys needed.
