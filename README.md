# Princeton Hacks: the restaurant inventory that keeps itself

Small restaurants still count stock by hand. The restaurant we interviewed enters inventory into Excel manually, "probably 4 hours a day," 5 to 7 days a week (`docs/discovery/2026-11-13-pizza-shop-interview.md`). They also told us: "we're a small business, we can do it manually for now." So it has to be *less* work than a clipboard, not another app. We put **a $30 webcam on the shelf and an AI agent in the owner's iMessage**.

**Where we tested it:** a family Italian restaurant, live, on Nov 14. We started with **the drinks out front** (Coke, Diet Coke, Sprite, water, limonada fresca), because the back rooms and kitchen couldn't be filmed. Cans are also the honest place to start: **one can = one unit**, so every count is exact and checkable. The same pipeline already handles kitchen bins (pepperoni, cheese, dough), with tests; that's the next deployment.

**What it does:**
1. **Sees a grab.** The camera notices a hand in a shelf zone and cuts a before/during/after clip. Liquid **d1** (vision) says what was taken and how many: 1, 2, 3, or "more", which goes to the owner. **YOLO-World** checks that a hand was actually there. Confident grabs update stock. Anything uncertain is flagged for a one-tap confirm, never guessed.
2. **Texts the owner (Photon 2.0 iMessage).** "Sprite is at 2, par 6." A **LangChain deepagents** agent answers questions, proposes a reorder plan (every item, cost, total), and waits for "yes". Approved means **on order**: nothing is sent to a supplier, and stock rises only when the owner says the delivery came.
3. **Keeps the owner's Excel sheet current.** "We counted 18 cokes" or "make water par 20" updates the real `.xlsx` and texts back a picture of it.
4. **Earns trust.** Every outgoing text is fact-checked by **d1 against the ledger**, and numbers are checked in code. Bank info is locked behind the owner's **authenticator-app code**. The code is checked in code and never shown to the AI; 3 wrong tries locks it; it re-locks after 10 minutes; amounts can't leak from a locked chat by any path.

```
webcam ──grab clip──▶ /api/grabs/upload ──▶ watcher ──▶ d1 (what + how many?) + YOLO-World (hand there?)
                                                            │
                                                  Postgres ledger (all math in code)
                                                            │
owner's iPhone ◀── iMessage ── Photon 2.0 bridge ◀── deepagents agent (+ d1 guard, approvals, code gate)
dashboard + live Excel sheet ◀── /api/*            board/LED (Arduino UNO Q) ◀── grab result
```

## Run it (one laptop)
The drinks test runs with `SCENE=drinks` (d1 is asked about cans and bottles); the default is the pizza make-line.
```bash
scripts/db_up.sh && uv run python scripts/seed_demo.py --reset          # Postgres + demo week
uv run uvicorn app.api.server:app --host 0.0.0.0 --port 8000           # API + grab watcher + alerts
cd dashboard && API_URL=http://localhost:8000 uv run streamlit run app.py     # dashboard
cd photon-bridge && npm start                                           # iMessage line (needs .env)
cd hardware && python main.py --camera 0 --source webcam --stream-port 8090 --api-url http://127.0.0.1:8000   # camera + live view/box editor
uv run python scripts/setup_authenticator.py                           # once: link the owner's authenticator app
```
Keys go in `.env` (see `.env.example`): `OPENROUTER_API_KEY` (agent model + d1), `AGENT_MODEL=openrouter:openai/gpt-oss-120b`, `D1_BACKEND=openrouter`, `LANGSMITH_API_KEY`. Photon: `photon-bridge/.env`.

## Tests
```bash
uv run pytest -q                              # 125 unit/integration tests, no paid APIs
RUN_E2E=1 uv run pytest tests/e2e -q -s       # real models end to end -> docs/e2e-report.md
(cd hardware && uv run --project .. python -m pytest -q tests)   # 39 Pi tests
(cd dashboard && uv run pytest -q)            # 10
(cd photon-bridge && npm test)                # 25
```

## Docs
- `docs/hackathon/README.md`: each sponsor's criteria, and what we show for it
- `docs/STATUS.md`: what's done, what's left
- `docs/architecture.md`, `docs/handoff.md` (Pi ↔ laptop)
- `docs/arnav-shah.md` (Photon/dashboard contract)
- `docs/struggles.md`: what broke and how we fixed it
- `docs/e2e-report.md`: the latest real-model run
- `docs/guard-calibration.md`, `docs/reviews/`
