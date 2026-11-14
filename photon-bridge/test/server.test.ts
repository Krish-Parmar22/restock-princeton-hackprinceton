import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { Relay } from "../src/relay.js";
import { bridgeServer } from "../src/server.js";
import { ChatStore } from "../src/store.js";

test("HTTP /send, /chats, /health and the persisted chat store", async () => {
  const dir = mkdtempSync(join(tmpdir(), "bridge-"));
  const store = new ChatStore(join(dir, "chats.json"));
  const sent: string[] = [];
  const relay = new Relay({
    store,
    outbox: { send: async (_c, _p, text) => void sent.push(text ?? "") },
    callAgent: async () => ({ reply: "ok" }),
  });
  const server = bridgeServer(relay, store).listen(0);
  await new Promise((r) => server.once("listening", r));
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const post = (body: string) =>
    fetch(`${base}/send`, { method: "POST", headers: { "content-type": "application/json" }, body }).then(async (r) => [r.status, await r.json()]);

  try {
    assert.deepEqual((await post(JSON.stringify({ chat_id: "c1", text: "hi" })))[1].ok, false);
    await relay.inbound({ messageId: "x", chatId: "c1", platform: "imessage", sender: "+1", text: "hello", images: [], ts: new Date() });
    assert.deepEqual(await post(JSON.stringify({ chat_id: "c1", text: "Pepperoni low", images: [] })), [200, { ok: true }]);
    assert.equal((await post("{not json"))[0], 400);
    assert.equal((await (await fetch(`${base}/chats`)).json())[0].chat_id, "c1");
    assert.equal((await (await fetch(`${base}/health`)).json()).ok, true);
    assert.equal(JSON.parse(readFileSync(join(dir, "chats.json"), "utf8"))[0].sender, "+1");
    assert.ok(new ChatStore(join(dir, "chats.json")).get("c1"), "store survives a restart");
    assert.deepEqual(sent, ["ok", "Pepperoni low"]);
  } finally {
    server.close();
  }
});
