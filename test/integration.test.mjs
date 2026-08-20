// Cross-language integration test: the REAL pi extension (TypeScript) talks
// to the REAL weechat script (Python), via a real Unix socket. The python
// side runs in a subprocess driven by test/py_driver.py, which renders
// buffer lines as JSON on stdout.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const PY_DRIVER = new URL("./py_driver.py", import.meta.url).pathname;

function startWeechatSide(sockPath) {
  const child = spawn("python3", [PY_DRIVER], {
    env: { ...process.env, PI_WEECHAT_SOCK: sockPath },
    stdio: ["pipe", "pipe", "inherit"],
  });
  const lines = [];
  const waiters = [];
  let stdoutBuf = "";
  child.stdout.on("data", (d) => {
    stdoutBuf += d.toString();
    while (stdoutBuf.includes("\n")) {
      const line = stdoutBuf.slice(0, stdoutBuf.indexOf("\n"));
      stdoutBuf = stdoutBuf.slice(stdoutBuf.indexOf("\n") + 1);
      if (!line.trim()) continue;
      let obj;
      try {
        obj = JSON.parse(line);
      } catch {
        continue;
      }
      lines.push(obj);
      for (let i = waiters.length - 1; i >= 0; i--) {
        if (waiters[i].pred(obj)) {
          const w = waiters.splice(i, 1)[0];
          w.resolve(obj);
        }
      }
    }
  });
  return {
    child,
    lines,
    waitFor(pred, what, ms = 5000) {
      const hit = lines.find(pred);
      if (hit) return Promise.resolve(hit);
      return new Promise((resolve, reject) => {
        const timer = setTimeout(
          () => reject(new Error(`timeout waiting for ${what}`)),
          ms
        );
        waiters.push({ pred, resolve: (v) => { clearTimeout(timer); resolve(v); } });
      });
    },
    send(op) {
      child.stdin.write(JSON.stringify(op) + "\n");
    },
  };
}

function makePiMock() {
  const handlers = {};
  const sentUserMessages = [];
  return {
    sentUserMessages,
    api: {
      on: (name, fn) => {
        (handlers[name] ??= []).push(fn);
      },
      registerCommand: () => {},
      sendUserMessage: (text, opts) => {
        sentUserMessages.push({ text, ...(opts ?? {}) });
      },
      getSessionName: () => "itg-session",
    },
    fire: async (name, event = {}, ctx = MOCK_CTX) => {
      for (const fn of handlers[name] ?? []) await fn(event, ctx);
    },
  };
}

const MOCK_CTX = {
  cwd: "/tmp/itg",
  model: { provider: "prov", id: "model-itg" },
  isIdle: () => true,
  abort() {},
};

test("integration: real extension ↔ real weechat script", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-itg-"));
  const sockPath = path.join(dir, "bridge.sock");

  const wc = startWeechatSide(sockPath);
  t.after(async () => {
    try { wc.child.kill("SIGKILL"); } catch {}
    fs.rmSync(dir, { recursive: true, force: true });
  });

  await wc.waitFor((m) => m.type === "ready", "python driver ready", 10_000);
  assert.ok(fs.existsSync(sockPath), "weechat side created the socket");

  // load the REAL extension and start a session
  process.env.PI_WEECHAT_SOCK = sockPath;
  const mod = await import(
    new URL("../extensions/weechat-bridge.ts", import.meta.url).href
  );
  const mock = makePiMock();
  mod.default(mock.api);

  await mock.fire("session_start");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("pi connected"), "buffer: pi connected");

  // session info + status render in the buffer
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("/tmp/itg") && m.text.includes("prov/model-itg"),
    "buffer: session line"
  );

  // streaming assistant text → whole lines in the buffer
  await mock.fire("message_start", { message: { role: "assistant" } });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "line one from pi\nline two" },
  });
  await mock.fire("message_update", { assistantMessageEvent: { type: "text_end", contentIndex: 0 } });
  await mock.fire("message_end", { message: { role: "assistant" } });
  await wc.waitFor((m) => m.type === "print" && m.text.includes("line one from pi"), "buffer: line one");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("line two"), "buffer: line two (tail flush)");

  // tool execution renders
  await mock.fire("tool_execution_start", { toolCallId: "t1", toolName: "read_file", args: { path: "/etc/hosts" } });
  await mock.fire("tool_execution_end", {
    toolCallId: "t1", isError: false,
    result: { content: [{ type: "text", text: "127.0.0.1 localhost" }] },
  });
  await wc.waitFor((m) => m.type === "print" && m.text.includes("read_file"), "buffer: tool start");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("127.0.0.1 localhost"), "buffer: tool output");

  // buffer title tracks state
  await wc.waitFor((m) => m.type === "title" && /read_file/.test(m.text), "title: tool state");

  // user types in the weechat buffer → pi.sendUserMessage
  wc.send({ op: "input", text: "hello from weechat" });
  await waitForMock(
    () => mock.sentUserMessages.find((m) => m.text === "hello from weechat"),
    "user_input delivered"
  );

  // !s steer prefix maps to deliverAs: steer
  wc.send({ op: "input", text: "!s do it this way" });
  await waitForMock(
    () => mock.sentUserMessages.find((m) => m.text === "do it this way" && m.deliverAs === "steer"),
    "steer delivered"
  );

  // !q queue prefix maps to deliverAs: followUp
  wc.send({ op: "input", text: "!q and then that" });
  await waitForMock(
    () => mock.sentUserMessages.find((m) => m.text === "and then that" && m.deliverAs === "followUp"),
    "followUp delivered"
  );

  // typed lines echo back into the buffer (strip weechat color tags first)
  const strip = (s) => s.replace(/color:(?:white|default|blue|cyan|red|green|gray|reset)/g, "");
  await wc.waitFor(
    (m) => m.type === "print" && strip(m.text).includes("> hello from weechat"),
    "buffer: echo"
  );

  // thinking streams: hidden by default, toggleable with !think on
  await mock.fire("message_start", { message: { role: "assistant" } });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "thinking_delta", contentIndex: 0, delta: "hidden pondering\n" },
  });
  // liveliness marker on a later block: once it renders, the (absent)
  // thinking line above could not still be in flight
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "text_delta", contentIndex: 1, delta: "liveliness check\n" },
  });
  await mock.fire("message_end", { message: { role: "assistant" } });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("liveliness check"),
    "buffer: text after hidden thinking"
  );
  assert.ok(
    !wc.lines.some((m) => m.type === "print" && m.text.includes("hidden pondering")),
    "thinking must be hidden by default"
  );

  wc.send({ op: "input", text: "!think on" });
  await wc.waitFor(
    (m) => m.type === "print" && strip(m.text).includes("thinking: on"),
    "buffer: thinking enabled"
  );

  await mock.fire("message_start", { message: { role: "assistant" } });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "thinking_delta", contentIndex: 0, delta: "visible pondering\n" },
  });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "thinking_end", contentIndex: 0 },
  });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("visible pondering"),
    "buffer: thinking line rendered"
  );
});

function waitForMock(pred, what, ms = 5000) {
  return new Promise((resolve, reject) => {
    const iv = setInterval(() => {
      const v = pred();
      if (v) {
        clearInterval(iv);
        clearTimeout(timer);
        resolve(v);
      }
    }, 25);
    const timer = setTimeout(() => {
      clearInterval(iv);
      reject(new Error(`timeout waiting for ${what}`));
    }, ms);
  });
}
