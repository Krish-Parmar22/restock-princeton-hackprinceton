// Client for the agent API's POST /inbound (owned by app/, contract in docs/arnav-shah.md).
import type { AgentReply, InboundPayload } from "./relay.js";

export function agentClient(apiUrl: string, timeoutMs = 90_000) {
  return async function callAgent(payload: InboundPayload): Promise<AgentReply> {
    const res = await fetch(`${apiUrl.replace(/\/$/, "")}/inbound`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!res.ok) throw new Error(`agent API ${res.status}: ${(await res.text()).slice(0, 200)}`);
    const body = (await res.json()) as Partial<AgentReply>;
    return {
      reply: typeof body.reply === "string" ? body.reply : null,
      images: Array.isArray(body.images) ? body.images.filter((p) => typeof p === "string") : [],
    };
  };
}
