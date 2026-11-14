# photon-bridge

The owner's iMessage ⇄ **Photon 2.0** (`api.photon.codes/v1`, `src/photon2.ts`) ⇄ agent API on `:8000`. A `pho_prj_…` project ID selects Photon 2.0 automatically; `npm run dev` still uses the offline terminal provider. Contract: `docs/arnav-shah.md`.

```
owner's phone ──iMessage──▶ Photon ──▶ bridge ──POST /inbound──▶ app/ (agent)
owner's phone ◀─────────── Photon ◀── bridge ◀──POST :8787/send── app/ (alerts)
```

## Run
```bash
cd photon-bridge
npm install
npm run dev          # terminal provider: chat in this terminal, no Photon account needed
cp .env.example .env # then fill PHOTON_PROJECT_ID (pho_prj_…) / PHOTON_API_KEY
npm start            # live on the Photon iMessage line
npm test             # 16 offline tests
```
Needs Node 22+. The agent API must be up on `API_URL` (`uv run uvicorn app.api.server:app --port 8000`).

## What it does
- **Inbound:** each owner text (and any photos, saved to `data/inbox/` and passed as `images`) is relayed to `POST /inbound`. The reply is sent back:
  - A reply with blank lines between parts is sent as separate texts. `images` follow the last text as iMessage photos.
  - `reply: null` with no images: nothing is sent.
  - Approval answers ("yes" / "no") are relayed verbatim. The agent resumes the order.
- **Slow agent:** after 4s with no answer, it sends "One sec, checking…" (with a typing indicator meanwhile), then the real answer.
- **Agent down:** the owner gets a short "couldn't reach the inventory system" text, not silence.
- **Order + dedupe:** messages in one chat are answered in order. Photon redeliveries (same message id) are handled once.
- **Outbound `POST :8787/send`** `{chat_id, text, images}`:
  - Returns `{"ok": true}`.
  - Returns `{"ok": false, "error": ...}` for a chat that never texted the line (a Photon rule).
  - An identical alert to the same chat within 10 minutes is dropped (`{"ok": true, "deduped": true}`) so the owner isn't spammed.
- **Chat memory:** every chat that texted is kept in `data/chats.json` (survives restarts). `GET /chats` lists them. `GET /health` is a liveness check.

`chat_id` is the Photon space id, which is stable per owner and is the agent's memory thread.

## Photon setup checklist
1. In app.beta.photon.codes: copy the **Project ID** (`pho_prj_…`) from Settings and create an API key (project:read + project:write). Put them in `.env`. Under **iMessage**, add the owner's phone number to get an assigned line.
2. `npm start`, then text the line from your phone. Check that the agent's answer comes back.
3. Every demo/judge phone must text the line once before `/send` can reach it.
