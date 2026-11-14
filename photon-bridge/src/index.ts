// Photon iMessage bridge: owner's phone <-> Photon (spectrum-ts) <-> agent API on :8000.
// Contract: docs/arnav-shah.md. Run: npm start (iMessage) or npm run dev (terminal provider, offline).
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { extname, join, resolve } from "node:path";
import { attachment, type Message, type Space, Spectrum } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";
import { terminal } from "spectrum-ts/providers/terminal";
import { agentClient } from "./agent.js";
import { type InboundEvent, type Outbox, Relay } from "./relay.js";
import { bridgeServer } from "./server.js";
import { ChatStore } from "./store.js";

const env = process.env;
const PROVIDER = env.PHOTON_PROVIDER ?? "imessage"; // imessage | terminal
const API_URL = env.API_URL ?? "http://localhost:8000";
const PORT = Number(env.BRIDGE_PORT ?? 8787);
const DATA_DIR = resolve(env.BRIDGE_DATA_DIR ?? join(import.meta.dirname, "..", "data"));
const INBOX_DIR = join(DATA_DIR, "inbox");

// The terminal provider owns stdout, so logs go to stderr.
const log = (msg: string, extra?: Record<string, unknown>) =>
  console.error(`[bridge ${new Date().toISOString()}] ${msg}${extra ? ` ${JSON.stringify(extra)}` : ""}`);

async function main() {
  const providers = PROVIDER === "terminal" ? [terminal.config()] : [imessage.config()];
  const creds =
    env.SPECTRUM_PROJECT_ID && env.SPECTRUM_PROJECT_SECRET
      ? { projectId: env.SPECTRUM_PROJECT_ID, projectSecret: env.SPECTRUM_PROJECT_SECRET }
      : {};
  if (PROVIDER !== "terminal" && !("projectId" in creds)) {
    throw new Error("Set SPECTRUM_PROJECT_ID and SPECTRUM_PROJECT_SECRET (Photon project Settings), or PHOTON_PROVIDER=terminal");
  }
  const app = await Spectrum({ ...creds, providers, options: { logLevel: "warn" } } as Parameters<typeof Spectrum>[0]);

  // Spaces seen this run; after a restart, look them up by id from the chat store.
  const spaces = new Map<string, Space>();
  async function spaceFor(chatId: string, platform: string): Promise<Space> {
    const cached = spaces.get(chatId);
    if (cached) return cached;
    const provider = platform === "terminal" ? terminal : imessage;
    const space = await (provider as any)(app).space.get(chatId);
    spaces.set(chatId, space);
    return space;
  }

  const outbox: Outbox = {
    async send(chatId, platform, text, images) {
      const space = await spaceFor(chatId, platform);
      if (text) await space.send(text);
      for (const path of images) {
        if (!existsSync(path)) {
          log("image missing, skipped", { path });
          continue;
        }
        await space.send(attachment(path));
      }
    },
    async typing(chatId, platform, on) {
      const space = await spaceFor(chatId, platform);
      await (on ? space.startTyping() : space.stopTyping());
    },
  };

  const store = new ChatStore(join(DATA_DIR, "chats.json"));
  const relay = new Relay({ store, outbox, callAgent: agentClient(API_URL), log });

  bridgeServer(relay, store, () => ({ provider: PROVIDER, api: API_URL, chats: store.all().length })).listen(PORT, () =>
    log(`listening on :${PORT}`, { provider: PROVIDER, api: API_URL }),
  );

  const shutdown = async () => {
    log("shutting down");
    await app.stop().catch(() => {});
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  for await (const [space, message] of app.messages) {
    if (message.direction !== "inbound") continue;
    spaces.set(space.id, space);
    unpack(message)
      .then((ev) => relay.inbound(ev))
      .catch((e) => log("could not read inbound message", { err: String(e) }));
  }
}

/** Flatten a Photon message (text, photo, album, reply) into text + saved image paths. */
async function unpack(message: Message): Promise<InboundEvent> {
  const texts: string[] = [];
  const images: string[] = [];

  async function walk(m: Message): Promise<void> {
    const c = m.content as any;
    switch (c?.type) {
      case "text":
      case "markdown":
        if (typeof c.text === "string") texts.push(c.text);
        else if (typeof c.source === "string") texts.push(c.source);
        break;
      case "attachment":
        if (String(c.mimeType).startsWith("image/")) images.push(await saveImage(m.id, c));
        break;
      case "group":
        for (const item of c.items as Message[]) await walk(item);
        break;
      case "reply":
        if (c.content) await walk({ ...m, content: c.content } as Message);
        break;
      default:
        break; // reactions, voice notes, contacts: not used by the agent
    }
  }
  await walk(message);

  return {
    messageId: message.id,
    chatId: message.space.id,
    platform: message.platform,
    sender: (message.sender as any)?.address ?? message.sender?.id ?? "unknown",
    text: texts.join("\n").trim(),
    images,
    ts: message.timestamp ?? new Date(),
  };
}

async function saveImage(messageId: string, a: { name?: string; mimeType: string; read: () => Promise<Buffer> }) {
  mkdirSync(INBOX_DIR, { recursive: true });
  const ext = extname(a.name ?? "") || `.${a.mimeType.split("/")[1]?.replace("jpeg", "jpg") ?? "img"}`;
  const path = join(INBOX_DIR, `${messageId.replace(/[^\w-]/g, "_")}${ext}`);
  writeFileSync(path, await a.read());
  return path;
}

main().catch((e) => {
  log("fatal", { err: String(e?.stack ?? e) });
  process.exit(1);
});
