// Photon 2.0 transport, offline: a fake P2Api stands in for api.photon.codes.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { flatten, isOwnerMessage, type P2Api, type P2Message, Photon2Transport } from "../src/photon2.js";
import type { InboundEvent } from "../src/relay.js";

const T0 = Date.parse("2026-11-14T15:00:00Z");

function msg(id: string, secs: number, content: P2Message["content"], over: Partial<P2Message> = {}): P2Message {
  return {
    id,
    platform: "imessage",
    status: "received",
    createdAt: new Date(T0 + secs * 1000).toISOString(),
    sentBy: { id: "usr_owner", kind: "human", handle: "+15550001111" },
    receivedBy: { id: "usr_agent", kind: "agent", handle: "+15550002222" },
    space: { id: "spc_1", kind: "direct" },
    content,
    ...over,
  };
}

function fakeApi(inbox: P2Message[]) {
  const sent: { spaceId: string; senderId: string; content: any; key: string }[] = [];
  const queries: string[] = [];
  let fail: { status: number } | null = null;
  const api: P2Api = {
    async listMessages(since) {
      queries.push(since);
      if (fail) throw Object.assign(new Error("boom"), fail);
      return { items: inbox.filter((m) => m.createdAt >= since) };
    },
    async sendMessage(spaceId, senderId, content, key) {
      sent.push({ spaceId, senderId, content, key });
    },
    async download() {
      return { bytes: Buffer.from("jpegbytes"), contentType: "image/jpeg" };
    },
    async upload() {
      return "pho_att_1";
    },
  };
  return { api, sent, queries, setFail: (f: { status: number } | null) => (fail = f) };
}

const quiet = () => {};

test("owner text becomes one inbound event; replays and our own sends are ignored", async () => {
  const dir = mkdtempSync(join(tmpdir(), "p2-"));
  const inbox = [
    msg("m1", 1, { type: "text", format: "plain", text: "how much pepperoni do we have?" }),
    msg("m2", 2, { type: "text", text: "ours" }, { sentBy: { id: "usr_agent", kind: "agent" }, receivedBy: { id: "usr_owner", kind: "human" } }),
    msg("m3", 3, { type: "message.read" } as any, { status: "read" }),
  ];
  const { api } = fakeApi(inbox);
  const t = new Photon2Transport({ api, dataDir: dir, log: quiet, now: () => T0 });
  const got: InboundEvent[] = [];
  await t.poll((e) => got.push(e));
  await t.poll((e) => got.push(e)); // overlap window re-reads m1: must not deliver twice
  assert.equal(got.length, 1);
  assert.equal(got[0].chatId, "spc_1");
  assert.equal(got[0].text, "how much pepperoni do we have?");
  assert.equal(got[0].sender, "+15550001111");
  assert.ok(t.knows("spc_1"));
});

test("reply goes into the same space as the agent user that received the text", async () => {
  const dir = mkdtempSync(join(tmpdir(), "p2-"));
  const { api, sent } = fakeApi([msg("m1", 1, { type: "text", text: "hi" })]);
  const t = new Photon2Transport({ api, dataDir: dir, log: quiet, now: () => T0 });
  await t.poll(() => {});
  await t.outbox.send("spc_1", "imessage", "118.6 lb of cheese", []);
  assert.deepEqual(sent.map((s) => [s.spaceId, s.senderId, s.content]), [
    ["spc_1", "usr_agent", { type: "text", format: "plain", text: "118.6 lb of cheese" }],
  ]);
});

test("images are uploaded and sent as attachments after the text", async () => {
  const dir = mkdtempSync(join(tmpdir(), "p2-"));
  const img = join(dir, "frame.jpg");
  writeFileSync(img, "x");
  const { api, sent } = fakeApi([msg("m1", 1, { type: "text", text: "hi" })]);
  const t = new Photon2Transport({ api, dataDir: dir, log: quiet, now: () => T0 });
  await t.poll(() => {});
  await t.outbox.send("spc_1", "imessage", "here's the grab", [img, join(dir, "missing.jpg")]);
  assert.deepEqual(sent.map((s) => s.content.type), ["text", "attachment"]);
  assert.equal(sent[1].content.attachmentId, "pho_att_1");
});

test("sending to a chat that never texted the line is refused", async () => {
  const dir = mkdtempSync(join(tmpdir(), "p2-"));
  const t = new Photon2Transport({ api: fakeApi([]).api, dataDir: dir, log: quiet, now: () => T0 });
  await assert.rejects(t.outbox.send("spc_unknown", "imessage", "hi", []), /never texted/);
});

test("the space map survives a restart, so agent-first alerts still work", async () => {
  const dir = mkdtempSync(join(tmpdir(), "p2-"));
  const a = fakeApi([msg("m1", 1, { type: "text", text: "hi" })]);
  await new Photon2Transport({ api: a.api, dataDir: dir, log: quiet, now: () => T0 }).poll(() => {});
  const b = fakeApi([msg("m1", 1, { type: "text", text: "hi" })]);
  const t2 = new Photon2Transport({ api: b.api, dataDir: dir, log: quiet, now: () => T0 + 60_000 });
  const got: InboundEvent[] = [];
  await t2.poll((e) => got.push(e));
  assert.equal(got.length, 0, "already-handled text is not replayed after restart");
  await t2.outbox.send("spc_1", "imessage", "pepperoni is below par", []);
  assert.equal(b.sent[0].senderId, "usr_agent");
  assert.ok(JSON.parse(readFileSync(join(dir, "photon2.json"), "utf8")).spaces.spc_1);
});

test("after long downtime, at most 10 minutes of old texts are picked up", async () => {
  const dir = mkdtempSync(join(tmpdir(), "p2-"));
  writeFileSync(join(dir, "photon2.json"), JSON.stringify({ since: new Date(T0 - 3 * 3600_000).toISOString(), seen: [], spaces: {} }));
  const { api, queries } = fakeApi([]);
  await new Photon2Transport({ api, dataDir: dir, log: quiet, now: () => T0 }).poll(() => {});
  assert.ok(Date.parse(queries[0]) >= T0 - 10 * 60_000 - 10_000, queries[0]);
});

test("photo from the owner is downloaded and passed as an image path", async () => {
  const dir = mkdtempSync(join(tmpdir(), "p2-"));
  const { api } = fakeApi([
    msg("m1", 1, { type: "group", parts: [{ type: "text", text: "is this bin low?" }, { type: "attachment", attachmentId: "pho_att_9" }] }),
  ]);
  const t = new Photon2Transport({ api, dataDir: dir, log: quiet, now: () => T0 });
  const got: InboundEvent[] = [];
  await t.poll((e) => got.push(e));
  assert.equal(got[0].text, "is this bin low?");
  assert.equal(got[0].images.length, 1);
  assert.equal(readFileSync(got[0].images[0], "utf8"), "jpegbytes");
});

test("429 backs off instead of hammering the API", async () => {
  const dir = mkdtempSync(join(tmpdir(), "p2-"));
  let now = T0;
  const f = fakeApi([]);
  const t = new Photon2Transport({ api: f.api, dataDir: dir, log: quiet, now: () => now });
  f.setFail({ status: 429 });
  await t.poll(() => {});
  f.setFail(null);
  now += 1_000;
  await t.poll(() => {});
  assert.equal(f.queries.length, 1, "no request during backoff");
  now += 60_000;
  await t.poll(() => {});
  assert.equal(f.queries.length, 2);
});

test("helpers: owner filter and content flattening", () => {
  assert.equal(isOwnerMessage(msg("a", 0, { type: "text", text: "x" })), true);
  assert.equal(isOwnerMessage(msg("b", 0, { type: "text", text: "x" }, { receivedBy: null })), false);
  assert.deepEqual(flatten({ type: "group", parts: [{ type: "text", text: "a" }, { type: "space.typing.update" } as any] }), { texts: ["a"], attachments: [] });
});
