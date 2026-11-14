import assert from "node:assert/strict";
import { test } from "node:test";
import { type AgentReply, FALLBACK_TEXT, HOLDING_TEXT, type InboundEvent, type Outbox, Relay } from "../src/relay.js";
import { ChatStore } from "../src/store.js";

type Sent = { chatId: string; text: string | null; images: string[] };

function setup(agent: (text: string) => Promise<AgentReply>, opts: { holdingAfterMs?: number } = {}) {
  const sent: Sent[] = [];
  const outbox: Outbox = {
    async send(chatId, _p, text, images) {
      sent.push({ chatId, text, images });
    },
  };
  const store = new ChatStore(null);
  const payloads: unknown[] = [];
  const relay = new Relay({
    store,
    outbox,
    callAgent: async (p) => {
      payloads.push(p);
      return agent(p.text);
    },
    holdingAfterMs: opts.holdingAfterMs ?? 1000,
  });
  return { relay, sent, store, payloads };
}

let n = 0;
const ev = (text: string, extra: Partial<InboundEvent> = {}): InboundEvent => ({
  messageId: `m${++n}`,
  chatId: "chat-1",
  platform: "imessage",
  sender: "+15550001111",
  text,
  images: [],
  ts: new Date("2026-11-14T13:02:11Z"),
  ...extra,
});
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

test("relays the agent's reply and images", async () => {
  const { relay, sent, payloads } = setup(async () => ({ reply: "About 6 lb.", images: ["/tmp/f.jpg"] }));
  await relay.inbound(ev("how much cheese is left?"));
  assert.deepEqual(sent, [{ chatId: "chat-1", text: "About 6 lb.", images: ["/tmp/f.jpg"] }]);
  assert.deepEqual(payloads[0], {
    chat_id: "chat-1",
    sender: "+15550001111",
    text: "how much cheese is left?",
    ts: "2026-11-14T13:02:11.000Z",
    images: [],
  });
});

test("null reply sends nothing", async () => {
  const { relay, sent } = setup(async () => ({ reply: null }));
  await relay.inbound(ev("ok thanks"));
  assert.equal(sent.length, 0);
});

test("slow agent gets a holding message first, then the answer", async () => {
  const { relay, sent } = setup(
    async () => {
      await sleep(60);
      return { reply: "Done." };
    },
    { holdingAfterMs: 10 },
  );
  await relay.inbound(ev("check variance"));
  assert.deepEqual(
    sent.map((s) => s.text),
    [HOLDING_TEXT, "Done."],
  );
});

test("fast agent: no holding message", async () => {
  const { relay, sent } = setup(async () => ({ reply: "Hi" }), { holdingAfterMs: 50 });
  await relay.inbound(ev("hi"));
  await sleep(70);
  assert.deepEqual(sent.map((s) => s.text), ["Hi"]);
});

test("agent down: owner gets a fallback, not silence", async () => {
  const { relay, sent } = setup(async () => {
    throw new Error("ECONNREFUSED");
  });
  await relay.inbound(ev("how much dough?"));
  assert.deepEqual(sent.map((s) => s.text), [FALLBACK_TEXT]);
});

test("duplicate deliveries of the same message are handled once", async () => {
  const { relay, sent } = setup(async () => ({ reply: "x" }));
  const e = ev("hi");
  await Promise.all([relay.inbound(e), relay.inbound(e)]);
  assert.equal(sent.length, 1);
});

test("messages in one chat are answered in order", async () => {
  const { relay, sent } = setup(async (t) => {
    await sleep(t === "first" ? 40 : 1);
    return { reply: t };
  });
  await Promise.all([relay.inbound(ev("first")), relay.inbound(ev("second"))]);
  assert.deepEqual(sent.map((s) => s.text), ["first", "second"]);
});

test("photos are forwarded as image paths", async () => {
  const { relay, payloads } = setup(async () => ({ reply: "Got the photo." }));
  await relay.inbound(ev("", { images: ["/abs/inbox/m.jpg"] }));
  assert.deepEqual((payloads[0] as any).images, ["/abs/inbox/m.jpg"]);
});

test("empty messages (reactions etc.) are not sent to the agent", async () => {
  const { relay, payloads, store } = setup(async () => ({ reply: "x" }));
  await relay.inbound(ev("   "));
  assert.equal(payloads.length, 0);
  assert.ok(store.get("chat-1"), "chat is still remembered");
});

test("/send refuses chats that never texted the line", async () => {
  const { relay } = setup(async () => ({ reply: null }));
  const r = await relay.send({ chat_id: "stranger", text: "Cheese is low" });
  assert.equal(r.ok, false);
  assert.match((r as any).error, /hasn't texted/);
});

test("/send works after the owner texted, and drops repeat alerts", async () => {
  const { relay, sent } = setup(async () => ({ reply: null }));
  await relay.inbound(ev("hi"));
  assert.deepEqual(await relay.send({ chat_id: "chat-1", text: "Cheese is below par. Reorder 20 lb?", images: [] }), { ok: true });
  assert.deepEqual(await relay.send({ chat_id: "chat-1", text: "Cheese is below par. Reorder 20 lb?" }), { ok: true, deduped: true });
  assert.equal(sent.filter((s) => s.text?.startsWith("Cheese")).length, 1);
});

test("/send validates its body", async () => {
  const { relay } = setup(async () => ({ reply: null }));
  assert.equal((await relay.send({ chat_id: "chat-1", text: "" })).ok, false);
});

test("multi-message replies are split on blank lines; images go with the last text", async () => {
  const { relay, sent } = setup(async () => ({
    reply: "Pepperoni is at 4.25 lb, below your par of 10.\n\nApprove this order: 20 pepperoni? Reply yes or no.",
    images: ["/abs/before.jpg"],
  }));
  await relay.inbound(ev("how's pepperoni?"));
  assert.deepEqual(sent, [
    { chatId: "chat-1", text: "Pepperoni is at 4.25 lb, below your par of 10.", images: [] },
    { chatId: "chat-1", text: "Approve this order: 20 pepperoni? Reply yes or no.", images: ["/abs/before.jpg"] },
  ]);
});

test("approval answers are relayed verbatim", async () => {
  const { relay, payloads } = setup(async () => ({ reply: "Ordered 20 lb pepperoni." }));
  await relay.inbound(ev("yes"));
  assert.equal((payloads[0] as any).text, "yes");
});

test("images with no text still get sent", async () => {
  const { relay, sent } = setup(async () => ({ reply: null, images: ["/abs/a.jpg"] }));
  await relay.inbound(ev("show me"));
  assert.deepEqual(sent, [{ chatId: "chat-1", text: null, images: ["/abs/a.jpg"] }]);
});
