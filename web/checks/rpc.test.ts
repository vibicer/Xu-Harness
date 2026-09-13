// Runnable check for the WebSocket JSON-RPC client:
//
//   node --test checks/rpc.test.ts
//
// Regression this exists for: once a frame was sent, `call()` settled only on a
// reply or `onclose`. A wedged brain method left the promise pending forever and
// leaked its `pending` map entry — a UI action (reload plugins / compress /
// rename) never completed and never errored. Every call now has a deadline, a
// synchronous `ws.send` throw rejects with a typed error, and each call settles
// exactly once with its timer cleared.
//
// The store half of the same fix (a failed `session.send` must roll back its
// optimistic row) is pinned by source text below: `store.svelte.ts` is a runes
// module and cannot be imported into this runner.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { BrainClient, RpcError } from "../src/lib/rpc.ts";

class FakeSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 3;
  readyState = 1;
  sent: { id: number; method: string }[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  url: string;
  constructor(url: string) {
    this.url = url;
  }
  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }
  close(): void {
    this.readyState = 3;
    this.onclose?.();
  }
}

/** A socket whose `send` throws synchronously (closing / closed socket). */
class ThrowingSocket extends FakeSocket {
  send(): void {
    throw new DOMException("socket is closing", "InvalidStateError");
  }
}

async function withSocket<T>(Ctor: typeof FakeSocket, fn: () => Promise<T>): Promise<T> {
  const real = globalThis.WebSocket;
  globalThis.WebSocket = Ctor as unknown as typeof WebSocket;
  try {
    return await fn();
  } finally {
    globalThis.WebSocket = real;
  }
}

function makeClient(): { client: BrainClient; ws: FakeSocket } {
  const client = new BrainClient("ws://test");
  client.connect();
  return { client, ws: (client as unknown as { ws: FakeSocket }).ws };
}

const pendingSize = (c: BrainClient): number =>
  (c as unknown as { pending: Map<number, unknown> }).pending.size;

const isRpc = (code: number) => (e: unknown) => e instanceof RpcError && e.code === code;

// 1. No reply before the deadline → typed timeout, entry dropped.
await withSocket(FakeSocket, async () => {
  const { client } = makeClient();
  const p = client.call("wedged.method", {}, 20);
  await assert.rejects(p, isRpc(-32001));
  assert.equal(pendingSize(client), 0, "the pending entry must be deleted on timeout");
});

// 2. A reply settles the call and clears the deadline timer.
await withSocket(FakeSocket, async () => {
  let cleared = 0;
  const realClear = globalThis.clearTimeout;
  globalThis.clearTimeout = ((h: Parameters<typeof clearTimeout>[0]) => {
    cleared++;
    return realClear(h);
  }) as typeof clearTimeout;
  try {
    const { client, ws } = makeClient();
    const p = client.call<{ ok: boolean }>("ping", {}, 10_000);
    const id = ws.sent[0].id;
    ws.onmessage!({ data: JSON.stringify({ jsonrpc: "2.0", id, result: { ok: true } }) });
    assert.deepEqual(await p, { ok: true });
    assert.equal(pendingSize(client), 0);
    assert.ok(cleared >= 1, "the deadline timer must be cleared when the call settles");
  } finally {
    globalThis.clearTimeout = realClear;
  }
});

// 3. A synchronous `send` throw → typed RpcError, entry dropped (no leak, no
//    raw DOMException escaping).
await withSocket(ThrowingSocket, async () => {
  const { client } = makeClient();
  await assert.rejects(client.call("x", {}, 10_000), isRpc(-32000));
  assert.equal(pendingSize(client), 0, "a send throw must not leak the pending entry");
});

// 4. A dropped connection rejects outstanding calls typed and clears the map.
await withSocket(FakeSocket, async () => {
  const { client } = makeClient();
  const p = client.call("slow", {}, 10_000);
  client.close(); // closedByUs → no reconnect is scheduled
  await assert.rejects(p, isRpc(-32000));
  assert.equal(pendingSize(client), 0, "onclose must clear the pending map");
});

// 5. Calling while not connected rejects typed without touching the socket.
{
  const client = new BrainClient("ws://test");
  await assert.rejects(client.call("x"), isRpc(-32000));
}

// 6. Store half (source text): a failed send rolls back its optimistic row and
//    rethrows; saveConfig assigns a fresh object so config consumers repaint.
{
  const store = readFileSync(new URL("../src/lib/store.svelte.ts", import.meta.url), "utf8");
  assert.match(
    store,
    /const optimistic = tab\.messages\[tab\.messages\.length - 1\]/,
    "send() must capture the stored optimistic row ($state proxies break identity)",
  );
  assert.match(
    store,
    /tab\.messages = tab\.messages\.filter\(\(m\) => m !== optimistic\)/,
    "a failed send must remove its optimistic row",
  );
  assert.match(
    store,
    /this\.config = \{ \.\.\.this\.config, \.\.\.patch \}/,
    "saveConfig must assign a fresh config object so readers re-run",
  );
}

console.log("rpc: 6 groups passed");
