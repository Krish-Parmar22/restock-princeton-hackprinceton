# The agent (LangChain deepagents)

One `create_deep_agent(...)` call in `build.py`:

| Piece | How |
|---|---|
| Planning | `TodoListMiddleware` (opt-in since deepagents 0.7) |
| d1 fact-check | `D1GuardMiddleware` (`guard.py`) wraps `send_text`: claims → d1 yes/no each → send / soften / block, max 2 blocks then a safe fallback. Every decision is recorded in `messages` |
| Fallback model | `ModelFallbackMiddleware` → `FALLBACK_MODEL` (skipped if its key is missing) |
| Subagents | `analyst` (how yesterday went, variance + clips), `clip-reviewer` (re-checks flagged grabs), `purchasing` (forecast vs stock and par), read-only `general-purpose` |
| Owner approval | `interrupt_on={"place_order": True, "place_orders": True}`; the owner's "yes"/"no" text resumes it (`runtime.py`) |
| Memory | `/memories/` and `/reports/` → Postgres store (owner corrections, morning briefings) |
| Persistence | Postgres checkpointer: paused approvals survive restarts. Thread = Photon `chat_id` |

Tools (`tools.py`): stock, variance, grabs, `d1_look`, `send_text`, orders, deliveries, counts, par, reorder plan, the sheet, usage trend, demand forecast, flagged grabs, briefing facts. All numbers come from the ledger.

## Run with a real model
`.env`:
```
AGENT_MODEL=openrouter:openai/gpt-oss-120b
OPENROUTER_API_KEY=...                      # also used for d1
FALLBACK_MODEL=anthropic:claude-sonnet-5-5  # optional; needs ANTHROPIC_API_KEY
D1_BACKEND=openrouter                       # or liquid / fake
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=princeton-hacks
```
Then:
```bash
scripts/db_up.sh && uv run python scripts/seed_demo.py --reset
uv run python scripts/chat_cli.py                         # talk to it in the terminal
uv run uvicorn app.api.server:app --port 8000             # what the Photon bridge calls
```
Without `AGENT_MODEL`, `/inbound` echoes, so the bridge always works.

## Tests
`uv run pytest tests/test_agent.py -q`: the real deepagents graph, driven by a scripted fake model. No keys needed.
