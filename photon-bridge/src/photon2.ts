// Photon 2.0 transport (https://photon.codes/docs/beta): api.photon.codes/v1 with a project API key.
// The prize requires Photon 2.0. Project IDs start with pho_prj_, and auth is `Authorization: Bearer <key>`.
//
// Inbound: poll listMessages every POLL_MS (the docs' own pattern). A Socket.IO event stream, when it
//   connects, triggers an immediate poll so replies feel instant. Polling alone is still correct.
// Outbound: sendMessage into the chat's space, as the agent user that received the owner's text.
//   Images are uploaded first (multipart/related, verified live) and sent as attachment messages.
//
// chat_id = the Photon space id (stable per owner). The space -> agent-user map is persisted, so
// /send (agent-initiated alerts) still works after a restart.
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { basename, dirname, extname, join } from "node:path";
import type { InboundEvent, Outbox } from "./relay.js";

export const API_BASE = "https://api.photon.codes";
const POLL_MS = 2_000;
const OVERLAP_MS = 10_000; // re-query a little history each poll: listMessages is eventually consistent
const MAX_SEEN = 2_000;

/** The slice of a Photon 2.0 message the bridge reads (validated by the client against the contract). */
export interface P2Message {
  id: string;
  platform: string;
  status?: string;
  createdAt: string;
  sentBy: { id: string; kind: string; handle?: string };
  receivedBy?: { id: string; kind: string; handle?: string } | null;
  space: { id: string; kind?: string };
  content: P2Content;
}
export type P2Content =
  | { type: "text"; format?: string; text: string }
  | { type: "attachment"; attachmentId: string; metadata?: unknown }
  | { type: "group"; parts: P2Content[] }
  | { type: string; [k: string]: unknown };

/** What the transport needs from Photon. Real impl below; tests inject a fake. */
export interface P2Api {
  listMessages(sinceIso: string, pageToken?: string): Promise<{ items: P2Message[]; nextPageToken?: string }>;
  sendMessage(spaceId: string, senderId: string, content: unknown, idempotencyKey: string): Promise<void>;
  download(attachmentId: string): Promise<{ bytes: Buffer; contentType: string }>;
  upload(bytes: Buffer, contentType: string, filename: string): Promise<string>; // -> attachment id
}

export function realApi(project: string, key: string): P2Api {
  const auth = { Authorization: `Bearer ${key}` };
  const url = (p: string) => `${API_BASE}/v1/projects/${encodeURIComponent(project)}${p}`;
  async function check(r: Response, what: string) {
    if (r.ok) return;
    const body = (await r.text().catch(() => "")).slice(0, 300);
    const err = new Error(`photon ${what} HTTP ${r.status}: ${body}`) as Error & { status: number; retryAfter?: number };
    err.status = r.status;
    const ra = Number(r.headers.get("retry-after"));
    if (Number.isFinite(ra) && ra > 0) err.retryAfter = ra * 1000;
    throw err;
  }
  return {
    async listMessages(sinceIso, pageToken) {
      const q = new URLSearchParams({ createdSince: sinceIso, direction: "received", order: "asc", pageSize: "100" });
      if (pageToken) q.set("pageToken", pageToken);
      const r = await fetch(`${url("/messages")}?${q}`, { headers: auth });
      await check(r, "listMessages");
      return (await r.json()) as { items: P2Message[]; nextPageToken?: string };
    },
    async sendMessage(spaceId, senderId, content, idempotencyKey) {
      const r = await fetch(url("/messages"), {
        method: "POST",
        headers: { ...auth, "content-type": "application/json", "idempotency-key": idempotencyKey },
        body: JSON.stringify({ spaceId, senderId, content }),
      });
      await check(r, "sendMessage");
    },
    async download(attachmentId) {
      const r = await fetch(url(`/attachments/${encodeURIComponent(attachmentId)}/content`), { headers: auth });
      await check(r, "downloadAttachment");
      return { bytes: Buffer.from(await r.arrayBuffer()), contentType: r.headers.get("content-type") ?? "application/octet-stream" };
    },
    async upload(bytes, contentType, filename) {
      // Two parts: JSON {contentType, sizeBytes, filename}, then the bytes (multipart/related).
      const b = `photon${randomUUID().replace(/-/g, "")}`;
      const meta = JSON.stringify({ contentType, sizeBytes: bytes.length, filename });
      const body = Buffer.concat([
        Buffer.from(`--${b}\r\nContent-Type: application/json\r\n\r\n${meta}\r\n--${b}\r\nContent-Type: ${contentType}\r\n\r\n`),
        bytes,
        Buffer.from(`\r\n--${b}--\r\n`),
      ]);
      const r = await fetch(url("/attachments"), {
        method: "POST",
        headers: { ...auth, "content-type": `multipart/related; boundary=${b}`, "content-length": String(body.length), "idempotency-key": randomUUID() },
        body,
      });
      await check(r, "uploadAttachment");
      return ((await r.json()) as { id: string }).id;
    },
  };
}

/** A human texting our agent line. Our own sends, receipts, and typing updates are ignored. */
export function isOwnerMessage(m: P2Message): boolean {
  if (m.status && m.status !== "received") return false;
  if (m.sentBy?.kind !== "human" || m.receivedBy?.kind !== "agent") return false;
  if (m.sentBy.id === m.receivedBy.id) return false;
  return true;
}

/** Text parts joined, attachment ids collected (groups flattened). */
export function flatten(c: P2Content): { texts: string[]; attachments: string[] } {
  const texts: string[] = [];
  const attachments: string[] = [];
  const walk = (x: P2Content) => {
    if (x.type === "text" && typeof (x as { text?: unknown }).text === "string") texts.push((x as { text: string }).text);
    else if (x.type === "attachment" && typeof (x as { attachmentId?: unknown }).attachmentId === "string")
      attachments.push((x as { attachmentId: string }).attachmentId);
    else if (x.type === "group" && Array.isArray((x as { parts?: unknown }).parts)) for (const p of (x as { parts: P2Content[] }).parts) walk(p);
  };
  walk(c);
  return { texts, attachments };
}

const IMAGE_TYPES: Record<string, string> = { ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic" };

interface State {
  since: string;
  seen: string[];
  spaces: Record<string, { agentId: string; platform: string }>;
}

export interface Photon2Options {
  api: P2Api;
  dataDir: string;
  log: (msg: string, extra?: Record<string, unknown>) => void;
  pollMs?: number;
  now?: () => number;
}

export class Photon2Transport {
  private state: State;
  private readonly statePath: string;
  private readonly inboxDir: string;
  private timer: NodeJS.Timeout | null = null;
  private polling: Promise<void> | null = null;
  private again = false;
  private backoffUntil = 0;
  private stopped = false;
  private warnedTyping = false;
  readonly outbox: Outbox;

  constructor(private readonly o: Photon2Options) {
    this.statePath = join(o.dataDir, "photon2.json");
    this.inboxDir = join(o.dataDir, "inbox");
    const now = (o.now ?? Date.now)();
    let loaded: Partial<State> = {};
    try {
      loaded = JSON.parse(readFileSync(this.statePath, "utf8"));
    } catch {
      // first run
    }
    // Resume where we left off, but never replay more than 10 minutes of old texts after downtime.
    const floor = new Date(now - 10 * 60_000).toISOString();
    const since = loaded.since && loaded.since > floor ? loaded.since : new Date(now - 5_000).toISOString();
    this.state = { since, seen: loaded.seen ?? [], spaces: loaded.spaces ?? {} };
    this.outbox = {
      send: (chatId, _platform, text, images) => this.send(chatId, text, images),
      typing: (chatId, _platform, on) => this.typing(chatId, on),
    };
  }

  /** True once this chat has texted the line (the only chats Photon lets us message). */
  knows(chatId: string): boolean {
    return chatId in this.state.spaces;
  }

  start(onInbound: (ev: InboundEvent) => void): void {
    const tick = () => {
      if (this.stopped) return;
      this.poll(onInbound).finally(() => {
        if (!this.stopped) this.timer = setTimeout(tick, this.o.pollMs ?? POLL_MS);
      });
    };
    tick();
  }

  /** Poll now (coalesced: a poll already running just runs once more when it finishes). */
  poll(onInbound: (ev: InboundEvent) => void): Promise<void> {
    if (this.polling) {
      this.again = true;
      return this.polling;
    }
    this.polling = (async () => {
      do {
        this.again = false;
        await this.pollOnce(onInbound);
      } while (this.again && !this.stopped);
    })().finally(() => {
      this.polling = null;
    });
    return this.polling;
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
  }

  private async pollOnce(onInbound: (ev: InboundEvent) => void): Promise<void> {
    const now = (this.o.now ?? Date.now)();
    if (now < this.backoffUntil) return;
    const sinceMs = Date.parse(this.state.since) - OVERLAP_MS;
    const sinceIso = new Date(sinceMs).toISOString();
    const fresh: P2Message[] = [];
    try {
      let token: string | undefined;
      do {
        const page = await this.o.api.listMessages(sinceIso, token);
        for (const m of page.items) if (!this.state.seen.includes(m.id)) fresh.push(m);
        token = page.nextPageToken || undefined;
      } while (token);
    } catch (e) {
      const err = e as { status?: number; retryAfter?: number };
      const wait = err.status === 429 ? (err.retryAfter ?? 30_000) : 5_000;
      this.backoffUntil = now + wait;
      this.o.log("photon poll failed", { err: String(e), backoffMs: wait });
      return;
    }
    fresh.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
    for (const m of fresh) {
      this.markSeen(m);
      if (!isOwnerMessage(m)) continue;
      this.state.spaces[m.space.id] = { agentId: m.receivedBy!.id, platform: m.platform };
      this.save();
      try {
        onInbound(await this.toEvent(m));
      } catch (e) {
        this.o.log("could not read inbound message", { id: m.id, err: String(e) });
      }
    }
  }

  private markSeen(m: P2Message) {
    this.state.seen.push(m.id);
    if (this.state.seen.length > MAX_SEEN) this.state.seen = this.state.seen.slice(-MAX_SEEN);
    if (m.createdAt > this.state.since) this.state.since = m.createdAt;
    this.save();
  }

  private async toEvent(m: P2Message): Promise<InboundEvent> {
    const { texts, attachments } = flatten(m.content);
    const images: string[] = [];
    for (const id of attachments) {
      try {
        const { bytes, contentType } = await this.o.api.download(id);
        if (!contentType.startsWith("image/")) continue;
        mkdirSync(this.inboxDir, { recursive: true });
        const ext = Object.entries(IMAGE_TYPES).find(([, t]) => t === contentType)?.[0] ?? ".img";
        const path = join(this.inboxDir, `${m.id.replace(/[^\w-]/g, "_")}_${images.length}${ext}`);
        writeFileSync(path, bytes);
        images.push(path);
      } catch (e) {
        this.o.log("attachment download failed", { id, err: String(e) });
      }
    }
    return {
      messageId: m.id,
      chatId: m.space.id,
      platform: m.platform,
      sender: m.sentBy.handle ?? m.sentBy.id,
      text: texts.join("\n").trim(),
      images,
      ts: new Date(m.createdAt),
    };
  }

  private target(chatId: string) {
    const t = this.state.spaces[chatId];
    if (!t) throw new Error(`chat ${chatId} has never texted the Photon line`);
    return t;
  }

  private async send(chatId: string, text: string | null, images: string[]): Promise<void> {
    const { agentId } = this.target(chatId);
    if (text) await this.o.api.sendMessage(chatId, agentId, { type: "text", format: "plain", text }, randomUUID());
    for (const path of images) {
      const ct = IMAGE_TYPES[extname(path).toLowerCase()];
      if (!ct || !existsSync(path)) {
        this.o.log("image skipped", { path });
        continue;
      }
      try {
        const id = await this.o.api.upload(readFileSync(path), ct, basename(path));
        await this.o.api.sendMessage(chatId, agentId, { type: "attachment", attachmentId: id }, randomUUID());
      } catch (e) {
        this.o.log("image send failed", { path, err: String(e) });
      }
    }
  }

  private async typing(chatId: string, on: boolean): Promise<void> {
    try {
      const { agentId } = this.target(chatId);
      await this.o.api.sendMessage(chatId, agentId, { type: "space.typing.update", typing: on }, randomUUID());
    } catch (e) {
      // Beta iMessage may not support typing; never let it break a reply.
      if (!this.warnedTyping) this.o.log("typing indicator unavailable", { err: String(e) });
      this.warnedTyping = true;
    }
  }

  private save() {
    mkdirSync(dirname(this.statePath), { recursive: true });
    const tmp = `${this.statePath}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.state));
    renameSync(tmp, this.statePath);
  }
}

/** Optional: Photon's Socket.IO event stream, used only as a "poll now" signal. */
export async function eventWake(project: string, key: string, onEvent: () => void, log: Photon2Options["log"]) {
  try {
    const { io } = await import("socket.io-client");
    const socket = io(API_BASE, {
      path: `/v1/projects/${project}/events/ws`,
      addTrailingSlash: false,
      transports: ["websocket"],
      extraHeaders: { Authorization: `Bearer ${key}` },
      autoConnect: false,
    });
    socket.on("connect", () => {
      socket.timeout(10_000).emit("subscribe", { startSeq: 0 }, (err: unknown, res: { ok?: boolean; error?: unknown }) => {
        if (err || !res?.ok) log("event stream subscribe failed (polling continues)", { err: String(err ?? JSON.stringify(res?.error)) });
        else log("event stream subscribed");
      });
    });
    socket.on("delivery", () => onEvent());
    socket.on("connect_error", (e: Error) => log("event stream unavailable (polling continues)", { err: e.message }));
    socket.connect();
    return () => socket.disconnect();
  } catch (e) {
    log("event stream disabled (polling continues)", { err: String(e) });
    return () => {};
  }
}
