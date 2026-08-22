// Protocol + integration tests for the pi <-> weechat bridge.
// Run: node --experimental-strip-types --test test/   (or `npm test`)
import test from "node:test";
import assert from "node:assert/strict";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

import { encodeMessage, LineDecoder, PROTOCOL_VERSION, MAX_LINE_BYTES, parseEndpoint, makeHello } from "../lib/codec.mjs";

// ---------------------------------------------------------------- codec unit

test("LineDecoder decodes NDJSON lines split across chunk boundaries", () => {
  const got = [];
  const d = new LineDecoder((m) => got.push(m));
  d.feed(Buffer.from('{"type":"a","x":'));
  d.feed(Buffer.from('1}\n{"type":"b"}\n\n{"typ'));
  d.feed(Buffer.from('e":"c"}\n'));
  assert.deepEqual(got, [{ type: "a", x: 1 }, { type: "b" }, { type: "c" }]);
});

test("LineDecoder keeps going after a bad JSON line", () => {
  const got = [];
  const errors = [];
  const d = new LineDecoder((m) => got.push(m), { onError: (e) => errors.push(e) });
  d.feed("not json\n{\"type\":\"ok\"}\n");
  assert.deepEqual(got, [{ type: "ok" }]);
  assert.equal(errors.length, 1);
  assert.equal(errors[0].code, "bad_json");
});

test("LineDecoder rejects oversized lines without unbounded buffering", () => {
  const got = [];
  const errors = [];
  const max = 64;
  const d = new LineDecoder((m) => got.push(m), { maxLineBytes: max, onError: (e) => errors.push(e) });
  d.feed("x".repeat(max + 100)); // no newline yet → bounded trim + error
  d.feed("\n{\"type\":\"ok\"}\n");
  assert.deepEqual(got, [{ type: "ok" }]);
  assert.ok(errors.some((e) => e.code === "line_too_long"));
});

test("LineDecoder rejects messages without a string type", () => {
  const got = [];
  const errors = [];
  const d = new LineDecoder((m) => got.push(m), { onError: (e) => errors.push(e) });
  d.feed("[1,2,3]\n\"str\"\n{\"type\":42}\n");
  assert.equal(got.length, 0);
  assert.equal(errors.length, 3);
});

test("MAX_LINE_BYTES is the shared 1 MiB limit", () => {
  assert.equal(MAX_LINE_BYTES, 1024 * 1024);
  assert.equal(PROTOCOL_VERSION, 2);
});

// ------------------------------------------------------- parseEndpoint unit

test("parseEndpoint: tcp:// forms", () => {
  assert.deepEqual(parseEndpoint("tcp://10.0.0.2:52311"), { kind: "tcp", host: "10.0.0.2", port: 52311 });
  assert.deepEqual(parseEndpoint("tcp://tail-4a2b.ts.net:52311"), { kind: "tcp", host: "tail-4a2b.ts.net", port: 52311 });
  assert.deepEqual(parseEndpoint("tcp://[::1]:52311"), { kind: "tcp", host: "[::1]", port: 52311 });
  // missing port / bad port / missing host → clear errors
  assert.throws(() => parseEndpoint("tcp://host"), /host:port/);
  assert.throws(() => parseEndpoint("tcp://host:abc"), /numeric port/);
  assert.throws(() => parseEndpoint("tcp://host:99999"), /out of range/);
  assert.throws(() => parseEndpoint("tcp://:52311"), /host:port|host/);
  assert.throws(() => parseEndpoint("tcp://"), /host:port/);
});

test("parseEndpoint: unix:// and unix: forms", () => {
  assert.deepEqual(parseEndpoint("unix:///tmp/bridge.sock"), { kind: "unix", path: "/tmp/bridge.sock" });
  assert.deepEqual(parseEndpoint("unix:/tmp/bridge.sock"), { kind: "unix", path: "/tmp/bridge.sock" });
  assert.deepEqual(parseEndpoint("unix:relative/bridge.sock"), { kind: "unix", path: "relative/bridge.sock" });
  assert.throws(() => parseEndpoint("unix://"), /socket path/);
});

test("parseEndpoint: schemeless host:port → tcp, else unix path", () => {
  assert.deepEqual(parseEndpoint("192.168.1.20:52311"), { kind: "tcp", host: "192.168.1.20", port: 52311 });
  assert.deepEqual(parseEndpoint("weechat-box:52311"), { kind: "tcp", host: "weechat-box", port: 52311 });
  assert.deepEqual(parseEndpoint("/run/user/1000/pi-weechat.sock"), { kind: "unix", path: "/run/user/1000/pi-weechat.sock" });
  assert.deepEqual(parseEndpoint("C:\\Users\\simeng\\pi-weechat.sock"), { kind: "unix", path: "C:\\Users\\simeng\\pi-weechat.sock" }, "Windows-style path is a unix socket path, not a scheme");
});

test("parseEndpoint: unknown scheme throws (extension point)", () => {
  assert.throws(() => parseEndpoint("tls://host:1"), /unsupported endpoint scheme "tls"/);
  assert.throws(() => parseEndpoint("ws://host:1"), /unsupported endpoint scheme "ws"/);
  assert.throws(() => parseEndpoint(""), /empty endpoint/);
});

test("makeHello: proof only included when non-empty", () => {
  assert.deepEqual(makeHello("pi"), { type: "hello", protocol: 2, name: "pi" });
  assert.deepEqual(makeHello("pi", "ab12"), { type: "hello", protocol: 2, name: "pi", proof: "ab12" });
  assert.deepEqual(makeHello("pi", ""), { type: "hello", protocol: 2, name: "pi" });
});

// ------------------------------------------------------------ fake weechat

/** A minimal stand-in for the WeeChat script's socket server. */
async function startFakeWeechat(socketPath) {
  const received = [];
  const connections = new Set();
  let client = null;
  const decoder = new LineDecoder((m) => received.push(m));
  const server = net.createServer((c) => {
    client = c;
    connections.add(c);
    c.on("close", () => connections.delete(c));
    c.on("data", (chunk) => decoder.feed(chunk));
  });
  await new Promise((res, rej) => {
    server.once("error", rej);
    server.listen(socketPath, res);
  });
  return {
    received,
    send: (obj) => {
      if (!client) throw new Error("no client connected yet");
      client.write(encodeMessage(obj));
    },
    close: async () => {
      for (const c of connections) c.destroy();
      await new Promise((res) => server.close(res));
      try {
        fs.unlinkSync(socketPath);
      } catch {
        /* ignore */
      }
    },
  };
}

function waitFor(fn, what, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const iv = setInterval(() => {
      let v;
      try {
        v = fn();
      } catch {
        /* not yet */
      }
      if (v) return clearInterval(iv), resolve(v);
      if (Date.now() - t0 > timeoutMs) {
        clearInterval(iv);
        reject(new Error(`timeout waiting for ${what}`));
      }
    }, 10);
  });
}

// -------------------------------------------------------- extension loading

async function loadExtension(socketPath) {
  process.env.PI_WEECHAT_SOCK = socketPath;
  const mod = await import("../extensions/weechat-bridge.ts");
  const handlers = {};
  const commands = {};
  const sent = [];
  const ctx = {
    cwd: "/tmp/demo",
    model: { provider: "testprov", id: "test-model" },
    isIdle: () => true,
    abort() {},
    compact(opts) {
      opts?.onComplete?.();
    },
  };
  const pi = {
    on: (name, fn) => {
      (handlers[name] ??= []).push(fn);
    },
    registerCommand: (name, opts) => {
      commands[name] = opts;
    },
    sendUserMessage: (text, opts) => {
      sent.push({ text, opts });
      return Promise.resolve();
    },
    getSessionName: () => "test-session",
  };
  mod.default(pi);
  const fire = async (name, event = {}) => {
    for (const fn of handlers[name] ?? []) await fn(event, ctx);
  };
  return { handlers, commands, sent, ctx, fire };
}

// ------------------------------------------------------------ integration

test("end-to-end: handshake, output mirroring, input injection", async (t) => {
  const socketPath = path.join(
    fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-")),
    "bridge.sock"
  );
  const weechat = await startFakeWeechat(socketPath);
  const ext = await loadExtension(socketPath);

  // session_start → connect + hello + session_info + status
  await ext.fire("session_start", { reason: "startup" });

  const hello = await waitFor(() => weechat.received.find((m) => m.type === "hello"), "pi hello");
  assert.equal(hello.protocol, PROTOCOL_VERSION);
  // protocol 2 handshake gating: the hello goes out FIRST, and the pending
  // queue (session_info emitted at session_start) is flushed only after it
  assert.ok(
    weechat.received.findIndex((m) => m.type === "hello") <
      weechat.received.findIndex((m) => m.type === "session_info"),
    "hello precedes session_info on the wire (server-side handshake gating)"
  );
  await waitFor(
    () => weechat.received.find((m) => m.type === "session_info"),
    "session_info"
  );
  const info = weechat.received.find((m) => m.type === "session_info");
  assert.equal(info.cwd, "/tmp/demo");
  assert.equal(info.model, "testprov/test-model");
  await waitFor(
    () => weechat.received.find((m) => m.type === "status" && m.state === "idle"),
    "status idle"
  );

  // handshake back, then streaming text → whole-line output
  weechat.send({ type: "hello", protocol: PROTOCOL_VERSION, name: "weechat-pi-bridge" });
  await ext.fire("message_start", { message: { role: "assistant" } });
  await ext.fire("message_update", { assistantMessageEvent: { type: "text_start", contentIndex: 0 } });
  await ext.fire("message_update", { assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "Hello world\n" } });
  await ext.fire("message_update", { assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "second line" } });
  await ext.fire("message_update", { assistantMessageEvent: { type: "text_end", contentIndex: 0 } });
  await ext.fire("message_end", {
    message: { role: "assistant", content: [{ type: "text", text: "Hello world\nsecond line" }] },
  });

  await waitFor(
    () => weechat.received.filter((m) => m.type === "assistant_line").length >= 2,
    "two assistant lines"
  );
  const lines = weechat.received.filter((m) => m.type === "assistant_line");
  assert.deepEqual(
    lines.map((m) => m.text),
    ["Hello world", "second line"]
  );
  await waitFor(
    () => weechat.received.find((m) => m.type === "assistant_flush"),
    "assistant_flush"
  );

  // tool events
  await ext.fire("tool_execution_start", { toolCallId: "t1", toolName: "bash", args: { command: "ls" } });
  await waitFor(
    () => weechat.received.find((m) => m.type === "status" && m.state === "tool:bash"),
    "status tool:bash"
  );
  await waitFor(() => weechat.received.find((m) => m.type === "tool_start"), "tool_start");
  await ext.fire("tool_execution_end", {
    toolCallId: "t1",
    isError: false,
    result: { content: [{ type: "text", text: "file.txt" }] },
  });
  const toolEnd = await waitFor(() => weechat.received.find((m) => m.type === "tool_end"), "tool_end");
  assert.equal(toolEnd.toolName ?? null, null); // name not needed on end
  assert.equal(toolEnd.output, "file.txt");
  assert.equal(toolEnd.isError, false);

  // input injection both directions
  weechat.send({ type: "user_input", text: "hello from weechat" });
  await waitFor(() => ext.sent.length >= 1, "sendUserMessage");
  assert.equal(ext.sent[0].text, "hello from weechat");
  assert.equal(ext.sent[0].opts, undefined);

  weechat.send({ type: "user_input", text: "steer me", deliverAs: "steer" });
  await waitFor(() => ext.sent.length >= 2, "sendUserMessage steer");
  assert.deepEqual(ext.sent[1], { text: "steer me", opts: { deliverAs: "steer" } });

  // user prompts typed in the pi terminal are mirrored back
  await ext.fire("input", { text: "typed in pi", source: "interactive" });
  const echo = await waitFor(() => weechat.received.find((m) => m.type === "user_echo"), "user_echo");
  assert.equal(echo.text, "typed in pi");

  // commands: abort uses ctx.abort directly; others route via /weechat-ctl
  let aborted = false;
  ext.ctx.abort = () => {
    aborted = true;
  };
  weechat.send({ type: "command", name: "abort" });
  await waitFor(() => aborted, "ctx.abort called");

  weechat.send({ type: "command", name: "status" });
  await waitFor(() => ext.sent.find((s) => s.text === "/weechat-ctl status"), "/weechat-ctl status");

  // the routed command handler works and answers on the wire
  assert.ok(ext.commands["weechat-ctl"], "weechat-ctl registered");
  await ext.commands["weechat-ctl"].handler("status", {
    ...ext.ctx,
    newSession: async () => ({}),
  });
  const infoCount0 = weechat.received.filter((m) => m.type === "session_info").length;
  await waitFor(
    () => weechat.received.filter((m) => m.type === "session_info").length > infoCount0,
    "second session_info from command"
  );

  // protocol mismatch is reported
  const before = weechat.received.length;
  weechat.send({ type: "hello", protocol: 999, name: "x" });
  await waitFor(
    () => weechat.received.slice(before).find((m) => m.type === "error" && m.code === "protocol_mismatch"),
    "protocol mismatch error"
  );

  // session_shutdown tears down (no reconnect loop afterwards)
  await ext.fire("session_shutdown", {});
  t.after(() => weechat.close());
});

test("oversized tool output is truncated at whole-line boundary", async (t) => {
  const socketPath = path.join(
    fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-")),
    "bridge.sock"
  );
  const weechat = await startFakeWeechat(socketPath);
  const ext = await loadExtension(socketPath);
  await ext.fire("session_start", {});
  await waitFor(() => weechat.received.find((m) => m.type === "hello"), "pi hello");
  weechat.send({ type: "hello", protocol: PROTOCOL_VERSION, name: "weechat-pi-bridge" });

  const big = Array.from({ length: 3000 }, (_, i) => `line-${i} ${"x".repeat(20)}`).join("\n");
  await ext.fire("tool_execution_end", {
    toolCallId: "t9",
    isError: true,
    result: { content: [{ type: "text", text: big }] },
  });
  const out = (await waitFor(() => weechat.received.find((m) => m.type === "tool_end"), "tool_end"))
    .output;
  assert.ok(out.length < big.length);
  assert.ok(out.includes("more characters truncated"));
  assert.ok(!out.endsWith("\n…"), "truncation note on its own line");
  t.after(() => weechat.close());
});
