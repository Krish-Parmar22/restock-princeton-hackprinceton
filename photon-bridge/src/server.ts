// HTTP side of the bridge on :8787. The agent calls POST /send to text the owner first.
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { Relay, SendRequest } from "./relay.js";
import type { ChatStore } from "./store.js";

async function readJson(req: IncomingMessage, limit = 1_000_000): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > limit) throw new Error("body too large");
    chunks.push(chunk as Buffer);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

export function bridgeServer(relay: Relay, store: ChatStore, info: () => Record<string, unknown> = () => ({})): Server {
  return createServer(async (req, res) => {
    const reply = (status: number, body: unknown) => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(body));
    };
    try {
      const path = new URL(req.url ?? "/", "http://localhost").pathname;
      if (req.method === "GET" && path === "/health") return reply(200, { ok: true, ...info() });
      if (req.method === "GET" && path === "/chats") return reply(200, store.all());
      if (req.method === "POST" && path === "/send") {
        let body: SendRequest;
        try {
          body = (await readJson(req)) as SendRequest;
        } catch (e) {
          return reply(400, { ok: false, error: `bad JSON: ${String(e)}` });
        }
        // Always 200 with {ok:false} on refusals, per the contract; the agent reads `ok`.
        return reply(200, await relay.send(body));
      }
      reply(404, { ok: false, error: "not found" });
    } catch (e) {
      reply(500, { ok: false, error: String(e) });
    }
  });
}
