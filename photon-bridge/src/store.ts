// Remembers every chat that has texted the line, so the agent can text the owner first later.
// The shared Photon line can only message someone who has texted it, so an unknown chat_id is refused.
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export interface ChatRecord {
  chat_id: string;
  platform: string;
  sender: string;
  first_seen: string;
  last_inbound: string;
}

export class ChatStore {
  private chats = new Map<string, ChatRecord>();

  constructor(private readonly path: string | null) {
    if (!path) return;
    try {
      const rows = JSON.parse(readFileSync(path, "utf8")) as ChatRecord[];
      for (const r of rows) this.chats.set(r.chat_id, r);
    } catch {
      // first run: no file yet
    }
  }

  get(chatId: string): ChatRecord | undefined {
    return this.chats.get(chatId);
  }

  all(): ChatRecord[] {
    return [...this.chats.values()];
  }

  /** Record an inbound message. Returns true if this chat is new. */
  seen(chatId: string, platform: string, sender: string, ts: Date): boolean {
    const prev = this.chats.get(chatId);
    const iso = ts.toISOString();
    this.chats.set(chatId, {
      chat_id: chatId,
      platform,
      sender,
      first_seen: prev?.first_seen ?? iso,
      last_inbound: iso,
    });
    this.save();
    return !prev;
  }

  private save(): void {
    if (!this.path) return;
    mkdirSync(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.all(), null, 2));
    renameSync(tmp, this.path); // atomic, so a crash never leaves half a file
  }
}
