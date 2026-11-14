// Platform-agnostic bridge logic: inbound relay to the agent API, and outbound /send.
// Everything that touches Photon or the network is injected, so tests run offline.
import type { ChatStore } from "./store.js";

/** POST :8000/inbound body (docs/arnav-shah.md). */
export interface InboundPayload {
  chat_id: string;
  sender: string;
  text: string;
  ts: string;
  images: string[];
}

/** POST :8000/inbound reply. `reply: null` means send nothing. */
export interface AgentReply {
  reply: string | null;
  images?: string[];
}

/** POST :8787/send body. */
export interface SendRequest {
  chat_id: string;
  text: string;
  images?: string[];
}

export type SendResult = { ok: true; deduped?: boolean } | { ok: false; error: string };

/** One inbound message, already unpacked from whatever the platform delivered. */
export interface InboundEvent {
  messageId: string;
  chatId: string;
  platform: string;
  sender: string;
  text: string;
  images: string[];
  ts: Date;
}

export interface Outbox {
  /** Send text and/or local image paths into a chat. */
  send(chatId: string, platform: string, text: string | null, images: string[]): Promise<void>;
  typing?(chatId: string, platform: string, on: boolean): Promise<void>;
}

export interface RelayDeps {
  store: ChatStore;
  outbox: Outbox;
  callAgent(payload: InboundPayload): Promise<AgentReply>;
  log?(msg: string, extra?: Record<string, unknown>): void;
  /** Send the holding message if the agent hasn't answered after this long. */
  holdingAfterMs?: number;
  holdingText?: string;
  /** Drop an outbound text identical to one sent to the same chat within this window (no spam). */
  dedupeWindowMs?: number;
  now?(): number;
}

export const HOLDING_TEXT = "One sec, checking…";
export const FALLBACK_TEXT = "Sorry, I couldn't reach the inventory system just now. Try again in a minute.";
/** Split an agent reply on blank lines into separate texts; null or blank gives []. */
export function splitReply(reply: string | null): string[] {
  if (!reply) return [];
  return reply
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter(Boolean);
}

export class Relay {
  private readonly seenIds = new Set<string>();
  private readonly chains = new Map<string, Promise<void>>();
  private readonly recentSends = new Map<string, number>();

  constructor(private readonly d: RelayDeps) {}

  private log(msg: string, extra?: Record<string, unknown>) {
    this.d.log?.(msg, extra);
  }

  private now() {
    return this.d.now?.() ?? Date.now();
  }

  /**
   * Queue one inbound message. Messages in the same chat are handled in order;
   * different chats run in parallel. Duplicate deliveries (same id) are ignored.
   */
  inbound(ev: InboundEvent): Promise<void> {
    if (this.seenIds.has(ev.messageId)) return Promise.resolve();
    this.seenIds.add(ev.messageId);
    if (this.seenIds.size > 5000) this.seenIds.delete(this.seenIds.values().next().value!);

    const prev = this.chains.get(ev.chatId) ?? Promise.resolve();
    const next = prev.then(() => this.handle(ev)).catch((e) => this.log("inbound failed", { err: String(e) }));
    this.chains.set(ev.chatId, next);
    void next.finally(() => {
      if (this.chains.get(ev.chatId) === next) this.chains.delete(ev.chatId);
    });
    return next;
  }

  private async handle(ev: InboundEvent): Promise<void> {
    const { store, outbox } = this.d;
    store.seen(ev.chatId, ev.platform, ev.sender, ev.ts);
    if (!ev.text.trim() && ev.images.length === 0) return; // reactions, typing, empty content

    this.log("inbound", { chat: ev.chatId, text: ev.text, images: ev.images.length });
    await outbox.typing?.(ev.chatId, ev.platform, true).catch(() => {});

    let holdingSent = false;
    const holdingAfter = this.d.holdingAfterMs ?? 4000;
    const timer = setTimeout(() => {
      holdingSent = true;
      outbox
        .send(ev.chatId, ev.platform, this.d.holdingText ?? HOLDING_TEXT, [])
        .catch((e) => this.log("holding send failed", { err: String(e) }));
    }, holdingAfter);

    let reply: AgentReply;
    try {
      reply = await this.d.callAgent({
        chat_id: ev.chatId,
        sender: ev.sender,
        text: ev.text,
        ts: ev.ts.toISOString(),
        images: ev.images,
      });
    } catch (e) {
      this.log("agent call failed", { err: String(e) });
      reply = { reply: FALLBACK_TEXT };
    } finally {
      clearTimeout(timer);
      await outbox.typing?.(ev.chatId, ev.platform, false).catch(() => {});
    }

    const images = reply.images ?? [];
    if (reply.reply === null && images.length === 0) {
      this.log("agent chose silence", { chat: ev.chatId, holdingSent });
      return;
    }
    // Several agent messages from one run arrive joined by a blank line: send them as separate texts,
    // with the images (grab frames as proof) after the last one.
    const parts = splitReply(reply.reply);
    if (parts.length === 0) {
      await outbox.send(ev.chatId, ev.platform, null, images);
      return;
    }
    for (const [i, part] of parts.entries()) {
      await outbox.send(ev.chatId, ev.platform, part, i === parts.length - 1 ? images : []);
      this.remember(ev.chatId, part);
    }
  }

  /** The agent texts the owner first (POST /send). */
  async send(req: SendRequest): Promise<SendResult> {
    if (!req?.chat_id || typeof req.text !== "string" || !req.text.trim()) {
      return { ok: false, error: "chat_id and non-empty text are required" };
    }
    const chat = this.d.store.get(req.chat_id);
    if (!chat) {
      return {
        ok: false,
        error: "unknown chat_id: the owner hasn't texted the line yet (Photon only delivers to people who texted first)",
      };
    }
    if (this.isDuplicate(req.chat_id, req.text)) {
      this.log("dropped duplicate alert", { chat: req.chat_id });
      return { ok: true, deduped: true };
    }
    try {
      await this.d.outbox.send(req.chat_id, chat.platform, req.text, req.images ?? []);
    } catch (e) {
      return { ok: false, error: `photon send failed: ${String(e)}` };
    }
    this.remember(req.chat_id, req.text);
    return { ok: true };
  }

  private key(chatId: string, text: string) {
    return `${chatId}\u0000${text.trim().toLowerCase()}`;
  }

  private remember(chatId: string, text: string | null) {
    if (text) this.recentSends.set(this.key(chatId, text), this.now());
  }

  private isDuplicate(chatId: string, text: string): boolean {
    const window = this.d.dedupeWindowMs ?? 10 * 60_000;
    const at = this.recentSends.get(this.key(chatId, text));
    return at !== undefined && this.now() - at < window;
  }

  /** Resolves when every queued inbound message has been handled (for tests and shutdown). */
  async idle(): Promise<void> {
    while (this.chains.size) await Promise.all([...this.chains.values()]);
  }
}
