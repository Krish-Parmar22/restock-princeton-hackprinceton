# Princeton Hacks: the restaurant inventory that keeps itself

A Raspberry Pi + webcam watches a pizza shop's ingredient bins. Every grab becomes a fact-checked inventory event. A LangChain **deepagents** agent texts the owner over iMessage (Photon): what's running low, where food (and money) went, and reorders on a "yes".

```
Pi camera ──grab package──▶ /api/grabs/upload ──▶ watcher ──▶ d1 (what happened?) + YOLO-World (hand in the bin?)
                                                                 │
                                                       Postgres ledger (all math in code)
                                                                 │
owner's iPhone ◀──iMessage── Photon bridge ◀── deepagents agent + D1 guard (fact-checks every text)
dashboard + Excel ◀── /api/*
```

## Run it (one laptop)
```bash
scripts/db_up.sh && uv run python scripts/seed_demo.py --reset          # Postgres + demo week
uv run uvicorn app.api.server:app --host 0.0.0.0 --port 8000           # API + grab watcher + alerts
cd dashboard && API_URL=http://localhost:8000 uv run streamlit run app.py     # dashboard
cd photon-bridge && npm start                                           # iMessage line (needs .env)
cd hardware && python main.py --api-url http://<laptop-ip>:8000 --upload-token <UPLOAD_TOKEN>   # Pi
```
Keys go in `.env` (see `.env.example`): `OPENROUTER_API_KEY` (agent model + d1), `AGENT_MODEL=openrouter:openai/gpt-oss-120b`, `D1_BACKEND=openrouter`, `LANGSMITH_API_KEY`. Photon: `photon-bridge/.env`.

## Tests
```bash
uv run pytest -q                              # 79 unit/integration tests, no paid APIs
RUN_E2E=1 uv run pytest tests/e2e -q -s       # real models end to end -> docs/e2e-report.md
(cd hardware && uv run --project .. python -m pytest -q tests)   # 39 Pi tests
(cd dashboard && uv run pytest -q)            # 9
(cd photon-bridge && npm test)                # 16
```

## Docs
- `docs/STATUS.md`: what's done, what's left
- `docs/architecture.md`, `docs/handoff.md` (Pi ↔ laptop)
- `docs/arnav-shah.md` (Photon/dashboard contract)
- `docs/struggles.md`: what broke and how we fixed it
- `docs/e2e-report.md`: the latest real-model run
- `docs/guard-calibration.md`, `docs/reviews/`
