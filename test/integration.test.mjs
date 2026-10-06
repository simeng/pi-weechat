// Cross-language integration test: the REAL pi extension (TypeScript) talks
// to the REAL weechat script (Python), via a real Unix socket. The python
// side runs in a subprocess driven by test/py_driver.py, which renders
// buffer lines as JSON on stdout.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as net from "node:net";
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
    waitFor(pred, what, ms = 5000, from = 0) {
      const hit = lines.slice(from).find(pred);
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
  const commands = {};
  const registeredTools = {};
  const sentUserMessages = [];
  const switchCalls = [];
  // Command context emulating pi's ExtensionCommandContext: /weechat-ctl
  // commands need waitForIdle/switchSession/newSession/compact.
  const commandCtx = {
    ...MOCK_CTX,
    waitForIdle: async () => {},
    newSession: async (o) => {
      o?.withSession?.(commandCtx);
      return { cancelled: false };
    },
    compact: (o) => o?.onComplete?.(),
    switchSession: async (file, o) => {
      switchCalls.push(file);
      o?.withSession?.(commandCtx);
      return { cancelled: false };
    },
  };
  return {
    sentUserMessages,
    switchCalls,
    registeredTools,
    api: {
      on: (name, fn) => {
        (handlers[name] ??= []).push(fn);
      },
      registerTool: (tool) => {
        registeredTools[tool.name] = tool;
      },
      registerCommand: (name, opts) => {
        commands[name] = opts;
      },
      sendUserMessage: (text, opts) => {
        sentUserMessages.push({ text, ...(opts ?? {}) });
        // Emulate pi's extension-command dispatch: with
        // expandPromptTemplates, "/weechat-ctl …" runs the registered handler
        // immediately (this is how !new/!model/… execute in real pi).
        if (
          typeof text === "string" &&
          text.startsWith("/weechat-ctl ") &&
          opts?.expandPromptTemplates
        ) {
          const raw = text.slice("/weechat-ctl ".length);
          void commands["weechat-ctl"]?.handler(raw, commandCtx);
        }
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
  // No ask_user among the tools → the bridge registers its built-in fallback
  // (exercises the registerTool path); real pi lists pi-ask-user's tool here.
  getAllTools: () => [],
  // session id for the multi-session reattach path (hello + session_info)
  sessionManager: { getSessionId: () => "itg-session-id" },
};

// The extension module is a singleton (registered once); all scenarios
// share one pi mock and re-fire session_start with different env.
const mock = makePiMock();
let extLoaded = false;
async function loadExt() {
  if (extLoaded) return;
  const mod = await import(
    new URL("../extensions/weechat-bridge.ts", import.meta.url).href
  );
  mod.default(mock.api);
  extLoaded = true;
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
  });
}

async function waitForFile(p, pred, what, ms = 10_000) {
  const t0 = Date.now();
  for (;;) {
    let data = "";
    try {
      data = fs.readFileSync(p, "utf8");
    } catch {
      /* not yet */
    }
    if (pred(data)) return data;
    if (Date.now() - t0 > ms) throw new Error(`timeout waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 100));
  }
}

test("integration: real extension ↔ real weechat script", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-itg-"));
  const sockPath = path.join(dir, "bridge.sock");

  const wc = startWeechatSide(sockPath);
  // Hermetic: never read the real ~/.pi/agent/pi-weechat.json (a configured
  // token would make the extension wait for a challenge the fake peer never
  // sends). Restored in t.after.
  const prevAgentDir = process.env.PI_CODING_AGENT_DIR;
  process.env.PI_CODING_AGENT_DIR = dir;
  t.after(async () => {
    try { wc.child.kill("SIGKILL"); } catch {}
    if (prevAgentDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevAgentDir;
    fs.rmSync(dir, { recursive: true, force: true });
  });

  await wc.waitFor((m) => m.type === "ready", "python driver ready", 10_000);
  assert.ok(fs.existsSync(sockPath), "weechat side created the socket");

  // load the REAL extension and start a session
  delete process.env.PI_WEECHAT_URL;
  delete process.env.PI_WEECHAT_TOKEN;
  process.env.PI_WEECHAT_SOCK = sockPath;
  await loadExt();

  await mock.fire("session_start");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("pi connected"), "buffer: pi connected");

  // session info + status render in the buffer
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("/tmp/itg") && m.text.includes("prov/model-itg"),
    "buffer: session line"
    );
    // reattach: the bridge drops the connection; the extension reconnects
    // with the same sessionId and reattaches to the SAME buffer
    const atDrop = wc.lines.length;
    wc.send({ op: "dropclient" });
    await wc.waitFor(
      (m) => m.type === "print" && m.text.includes("pi disconnected"),
      "buffer: pi disconnected",
      5000,
      atDrop
    );
    await wc.waitFor(
      (m) => m.type === "print" && m.text.includes("pi connected"),
      "buffer: pi reconnected (reattach by sessionId)",
      5000,
      atDrop
    );
  // a second, CONCURRENT client (raw NDJSON peer) gets its own buffer;
  // input/output of one session never reach the other
  const c2 = net.connect(sockPath);
  const c2msgs = [];
  let c2buf = "";
  c2.on("data", (d) => {
    c2buf += d.toString();
    while (c2buf.includes("\n")) {
      const l = c2buf.slice(0, c2buf.indexOf("\n"));
      c2buf = c2buf.slice(c2buf.indexOf("\n") + 1);
      if (l.trim()) c2msgs.push(JSON.parse(l));
    }
  });
  await new Promise((res, rej) => { c2.once("error", rej); c2.once("connect", res); });
  const atC2 = wc.lines.length;
  c2.write(JSON.stringify({ type: "hello", protocol: 3, name: "pi" }) + "\n");
  await waitForMock(() => c2msgs.some((m) => m.type === "hello"), "second client: hello accepted");
  c2.write(JSON.stringify({ type: "session_info", cwd: "/tmp/itg2" }) + "\n");
  const c2connected = await wc.waitFor(
    (m) => m.type === "print" && m.buffer && m.buffer !== "buffer" && m.text.includes("pi connected"),
    "second client: connected in its own buffer",
    5000,
    atC2
  );
  c2.write(JSON.stringify({ type: "assistant_line", msgId: "m-c2", text: "hello from client two" }) + "\n");
  c2.write(JSON.stringify({ type: "assistant_flush", msgId: "m-c2" }) + "\n");
  const c2line = await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("hello from client two"),
    "second client: assistant line",
    5000,
    atC2
  );
  assert.equal(c2line.buffer, c2connected.buffer, "output lands in the second client's buffer");
  // input in the FIRST buffer reaches the extension, not the raw client
  const c2msgsBefore = c2msgs.length;
  wc.send({ op: "input", text: "ping one", buffer: "buffer" });
  await waitForMock(() => mock.sentUserMessages.some((m) => m.text === "ping one"), "extension: first-buffer input");
  assert.equal(c2msgs.length, c2msgsBefore, "first-buffer input never reaches the second client");
  // input in the SECOND buffer reaches the raw client, not the extension
  const echoesBefore = mock.sentUserMessages.length;
  wc.send({ op: "input", text: "ping two", buffer: c2connected.buffer });
  await waitForMock(() => c2msgs.some((m) => m.type === "user_input" && m.text === "ping two"), "second client: user_input");
  assert.equal(mock.sentUserMessages.length, echoesBefore, "second-buffer input never reaches the extension");
  c2.end();
  await new Promise((res) => c2.once("close", res));

  // Echoing a prompt is only a mirror event; timers start with Pi's agent lifecycle,
  // not from a typed line or a WeeChat control command.
  const latestTitle = () => wc.lines.filter((m) => m.type === "title" && m.buffer === "buffer").at(-1)?.text ?? "";
  const titleBeforeEcho = latestTitle();
  await mock.fire("input", { source: "interactive", text: "typed in pi terminal" });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("typed in pi terminal"),
    "buffer: echoed pi-terminal prompt"
  );
  assert.equal(latestTitle(), titleBeforeEcho, "user_echo must not start the timer");

  // Pi's turnIndex is zero-based, resets at agent_start, and advances across
  // model turns in one run. Tool work is part of a turn; blocking UI time is not.
  await mock.fire("agent_start");
  await mock.fire("turn_start", { turnIndex: 0, timestamp: Date.now() });
  await wc.waitFor(
    (m) => m.type === "title" && m.text.includes("run ") && m.text.includes("turn 1"),
    "title: first model turn started"
  );
  await new Promise((resolve) => setTimeout(resolve, 1100));
  const liveTurnTitle = latestTitle();
  assert.match(liveTurnTitle, / · run \d+s · \d+s · turn 1\)/, liveTurnTitle);

  // Restart the WeeChat-side bridge while Pi remains mid-turn. The next
  // handshake must repaint the Pi-owned snapshot, not restart the clocks.
  const runBeforeReconnect = Number(liveTurnTitle.match(/ · run (\d+)s/)?.[1]);
  const oldChild = wc.child;
  const oldServerExit = new Promise((resolve) => oldChild.once("exit", resolve));
  wc.send({ op: "quit" });
  await oldServerExit;
  Object.assign(wc, startWeechatSide(sockPath));
  await wc.waitFor((m) => m.type === "ready", "replacement WeeChat driver ready");
  await wc.waitFor(
    (m) => m.type === "title" && m.text.includes("turn 1"),
    "title: active timing restored after reconnect"
  );
  const reconnectedTitle = latestTitle();
  const runAfterReconnect = Number(reconnectedTitle.match(/ · run (\d+)s/)?.[1]);
  assert.ok(runAfterReconnect >= runBeforeReconnect, reconnectedTitle);

  await mock.fire("ui_prompt_start", { kind: "select", reason: "ui_prompt" });
  await new Promise((resolve) => setTimeout(resolve, 100));
  const pausedTitle = latestTitle();
  await new Promise((resolve) => setTimeout(resolve, 1200));
  assert.equal(latestTitle(), pausedTitle, "UI prompt time must pause both clocks");
  const turnParts = (title) => {
    const match = title.match(/ · run (\d+)s · (\d+)s · turn 1\)/);
    return match ? [Number(match[1]), Number(match[2])] : null;
  };
  const pausedParts = turnParts(pausedTitle);
  assert.ok(pausedParts, pausedTitle);
  const linesBeforeResume = wc.lines.length;
  await mock.fire("ui_prompt_end", { kind: "select", reason: "ui_prompt" });
  await wc.waitFor(
    (m) => {
      if (m.type !== "title" || wc.lines.indexOf(m) < linesBeforeResume) return false;
      const parts = turnParts(m.text);
      return parts && parts[0] > pausedParts[0] && parts[1] > pausedParts[1];
    },
    "title: run and turn clocks resume after the prompt",
    4000,
  );
  const resumedTitle = latestTitle();
  assert.ok(turnParts(resumedTitle)[0] > pausedParts[0], resumedTitle);
  assert.ok(turnParts(resumedTitle)[1] > pausedParts[1], resumedTitle);

  const linesBeforeTurnEnd = wc.lines.length;
  await mock.fire("turn_end", { turnIndex: 0 });
  await new Promise((resolve) => setTimeout(resolve, 100));
  const completedParts = turnParts(latestTitle());
  assert.ok(completedParts, latestTitle());
  await wc.waitFor(
    (m) => {
      if (m.type !== "title" || wc.lines.indexOf(m) < linesBeforeTurnEnd) return false;
      const parts = turnParts(m.text);
      return parts && parts[0] > completedParts[0] && parts[1] === completedParts[1];
    },
    "title: run advances while completed turn stays frozen",
    4000,
  );
  const betweenTurnsTitle = latestTitle();
  const betweenParts = turnParts(betweenTurnsTitle);
  assert.ok(betweenParts && betweenParts[0] > completedParts[0], betweenTurnsTitle);
  assert.equal(betweenParts[1], completedParts[1], "completed turn clock remains frozen");
  const linesBeforeSecondRunTick = wc.lines.length;
  await wc.waitFor(
    (m) => {
      if (m.type !== "title" || wc.lines.indexOf(m) < linesBeforeSecondRunTick) return false;
      const parts = turnParts(m.text);
      return parts && parts[0] > betweenParts[0] && parts[1] === betweenParts[1];
    },
    "title: completed turn stays frozen across another run tick",
    4000,
  );
  const laterBetweenParts = turnParts(latestTitle());
  assert.ok(laterBetweenParts && laterBetweenParts[0] > betweenParts[0]);
  assert.equal(laterBetweenParts[1], completedParts[1]);

  await mock.fire("turn_start", { turnIndex: 1, timestamp: Date.now() });
  await wc.waitFor(
    (m) => m.type === "title" && m.text.includes("turn 2"),
    "title: second model turn started"
  );
  assert.match(latestTitle(), / · \d+s · turn 2\)/, latestTitle());
  await mock.fire("turn_end", { turnIndex: 1 });
  await mock.fire("agent_settled");
  await wc.waitFor(
    (m) => m.type === "title" && /\(idle · last run \d+s · 2 turns\)/.test(m.text),
    "title: settled run summary"
  );
  const settledTitle = latestTitle();
  await new Promise((resolve) => setTimeout(resolve, 1200));
  assert.equal(latestTitle(), settledTitle, "settled values remain frozen");

  // A settled/aborted run can end without a turn_end event; agent_settled
  // still freezes the live turn and the run clocks.
  await mock.fire("agent_start");
  await mock.fire("turn_start", { turnIndex: 0, timestamp: Date.now() });
  await mock.fire("agent_settled");
  await wc.waitFor(
    (m) => m.type === "title" && /\(idle · last run \d+s · 1 turn\)/.test(m.text),
    "title: aborted run settled without turn_end"
  );

  // streaming assistant text → whole lines in the buffer
  await mock.fire("message_start", { message: { role: "assistant" } });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "line one from pi\nline two" },
  });
  await mock.fire("message_update", { assistantMessageEvent: { type: "text_end", contentIndex: 0 } });
  await mock.fire("message_end", { message: { role: "assistant" } });
  await wc.waitFor((m) => m.type === "print" && m.tags === "notify_none,nick_pi,prefix_nick_chat_nick" && m.prefix.includes("pi") && m.text.includes("line one from pi"), "buffer: line one (pi nick prefix)");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("line two"), "buffer: line two (tail flush)");

  // tool execution renders
  await mock.fire("tool_execution_start", { toolCallId: "t1", toolName: "read_file", args: { path: "/etc/hosts" } });
  await mock.fire("tool_execution_end", {
    toolCallId: "t1", isError: false,
    result: { content: [{ type: "text", text: "127.0.0.1 localhost" }] },
  });
  await wc.waitFor((m) => m.type === "print" && (m.tags || "").includes("nick_read_file") && m.prefix.includes("read_file") && m.text.includes("/etc/hosts"), "buffer: tool start under the tool nick");
  await wc.waitFor((m) => m.type === "print" && (m.tags || "").includes("nick_read_file") && m.text.includes("127.0.0.1 localhost"), "buffer: tool output under the tool nick");
  assert.ok(!wc.lines.some((m) => m.type === "print" && (m.tags || "").includes("nick_read_file") && m.text.includes("read_file")),
    "auto nick mode keeps the tool name in the nick column, not in the body");

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

  // typed lines echo back into the buffer under the user's IRC nick
  // (prnt_date_tags tag prefix_nick_chat_nick_self, user nick before the TAB —
  // no legacy '> ' marker in the text)
  await wc.waitFor(
    (m) => m.type === "print"
      && m.tags === "self_msg,notify_none,no_highlight,prefix_nick_chat_nick_self"
      && m.prefix.includes("alice") && m.text === "hello from weechat",
    "buffer: echo (user nick)"
  );
  assert.ok(
    !wc.lines.some((m) => m.type === "print" && m.text.startsWith("> ")),
    "no legacy '> ' marker anywhere in the buffer"
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
    (m) => m.type === "print" && m.text.includes("thinking: on"),
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
    (m) => m.type === "print" && (m.tags || "").includes("nick_think") && m.prefix.includes("think") && m.text.includes("visible pondering"),
    "buffer: thinking line under the think nick"
  );

  // !nick pi → the legacy single `pi` nick, tool name back in the body
  wc.send({ op: "input", text: "!nick pi" });
  await wc.waitFor((m) => m.type === "print" && m.text.includes("nick mode: pi"), "buffer: !nick pi confirmed");
  await mock.fire("tool_execution_start", { toolCallId: "t9", toolName: "bash", args: { command: "uname -a" } });
  await wc.waitFor(
    (m) => m.type === "print" && m.tags === "notify_none,prefix_nick_chat_nick"
      && m.prefix.includes("pi") && m.text.includes("bash") && m.text.includes("uname -a"),
    "buffer: legacy pi nick with the tool name in the body"
  );
  wc.send({ op: "input", text: "!nick auto" });
  await wc.waitFor((m) => m.type === "print" && m.text.includes("nick mode: auto"), "buffer: back to auto nicks");

  // stop the extension cleanly before the child is killed
  await mock.fire("session_shutdown");
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("pi disconnected"),
    "buffer: disconnected after shutdown"
  );
});

test("integration: real extension ↔ real weechat script over TCP with token", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-itg-tcp-"));
  const sockPath = path.join(dir, "bridge.sock");
  const port = await getFreePort();
  const token = "itg-secret-" + Math.random().toString(16).slice(2);

  const wc = startWeechatSide(sockPath);
  t.after(async () => {
    try { wc.child.kill("SIGKILL"); } catch {}
    fs.rmSync(dir, { recursive: true, force: true });
  });

  await wc.waitFor((m) => m.type === "ready", "python driver ready", 10_000);

  // start the TCP listener via the live config path (token first, then the
  // listener — the listening line then notes "token required")
  wc.send({ op: "set", name: "token", value: token });
  wc.send({ op: "set", name: "tcp_listen", value: `127.0.0.1:${port}` });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes(`listening on tcp 127.0.0.1:${port}`) && m.text.includes("token required"),
    "tcp listening line"
  );

  // point the REAL extension at the TCP endpoint (env re-read on session_start)
  const dbgPath = path.join(dir, "bridge.debug.log");
  process.env.PI_WEECHAT_URL = `tcp://127.0.0.1:${port}`;
  process.env.PI_WEECHAT_TOKEN = token;
  process.env.PI_BRIDGE_DEBUG = dbgPath;
  await loadExt();
  await mock.fire("session_start");

  // challenge → hello+proof → connected (peer IP in the buffer line)
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("pi connected from 127.0.0.1"),
    "buffer: pi connected over tcp"
  );

  // session info renders
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("/tmp/itg") && m.text.includes("prov/model-itg"),
    "buffer: session line"
  );

  // streaming assistant text → whole lines
  await mock.fire("message_start", { message: { role: "assistant" } });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "tcp line one\ntcp line two" },
  });
  await mock.fire("message_update", { assistantMessageEvent: { type: "text_end", contentIndex: 0 } });
  await mock.fire("message_end", { message: { role: "assistant" } });
  await wc.waitFor((m) => m.type === "print" && m.tags === "notify_none,nick_pi,prefix_nick_chat_nick" && m.prefix.includes("pi") && m.text.includes("tcp line one"), "buffer: tcp line one (pi nick prefix)");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("tcp line two"), "buffer: tcp line two");

  // tool execution renders
  await mock.fire("tool_execution_start", { toolCallId: "t1", toolName: "bash", args: { command: "uname -a" } });
  await mock.fire("tool_execution_end", {
    toolCallId: "t1", isError: false,
    result: { content: [{ type: "text", text: "Linux tcp-box 6.1" }] },
  });
  await wc.waitFor((m) => m.type === "print" && (m.tags || "").includes("nick_bash") && m.prefix.includes("bash") && m.text.includes("uname -a"), "buffer: tool start under the tool nick");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Linux tcp-box 6.1"), "buffer: tool output");

  // user_input round trip over TCP
  wc.send({ op: "input", text: "hello over tcp" });
  await waitForMock(
    () => mock.sentUserMessages.find((m) => m.text === "hello over tcp"),
    "user_input delivered over tcp"
  );
  wc.send({ op: "input", text: "!s steer over tcp" });
  await waitForMock(
    () => mock.sentUserMessages.find((m) => m.text === "steer over tcp" && m.deliverAs === "steer"),
    "steer delivered over tcp"
  );
  wc.send({ op: "input", text: "!q queue over tcp" });
  await waitForMock(
    () => mock.sentUserMessages.find((m) => m.text === "queue over tcp" && m.deliverAs === "followUp"),
    "followUp delivered over tcp"
  );
  await wc.waitFor(
    (m) => m.type === "print"
      && m.tags === "self_msg,notify_none,no_highlight,prefix_nick_chat_nick_self"
      && m.prefix.includes("alice") && m.text === "hello over tcp",
    "buffer: echo over tcp (user nick)"
  );

  // ---------------- negative: wrong token ⇒ auth_failed + retries -------
  await mock.fire("session_shutdown");
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("pi disconnected"),
    "buffer: disconnected before negative phase"
  );

  process.env.PI_WEECHAT_TOKEN = "wrong-" + token;
  await mock.fire("session_start");
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("auth failed from"),
    "buffer: auth_failed line"
  );

  // the extension does not wedge: it keeps dialing with backoff, and the
  // loud auth note lands in the debug log
  const log = await waitForFile(
    dbgPath,
    (d) => d.includes("AUTH FAILED") && (d.match(/dialing /g) || []).length >= 3,
    "auth note + 3 dial attempts in the debug log"
  );
  // the shared secret itself is never logged (only one-way proofs are sent)
  assert.ok(!log.includes(token), "token must never appear in the debug log");
  assert.ok(!log.includes("wrong-" + token), "wrong token must never appear in the debug log");

  await mock.fire("session_shutdown");
});

// The config FILE drives endpoint + token (no PI_WEECHAT_URL / _TOKEN env),
// then the same file with a WRONG token loses to the correct env value —
// proving env vars take precedence over pi-weechat.json.
test("integration: config file pi-weechat.json + env precedence", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-itg-cfg-"));
  const sockPath = path.join(dir, "bridge.sock");
  const port = await getFreePort();
  const token = "cfg-secret-" + Math.random().toString(16).slice(2);
  const cfgPath = path.join(dir, "pi-weechat.json");

  const wc = startWeechatSide(sockPath);
  t.after(async () => {
    try { wc.child.kill("SIGKILL"); } catch {}
    fs.rmSync(dir, { recursive: true, force: true });
  });
  t.after(() => {
    delete process.env.PI_WEECHAT_URL;
    delete process.env.PI_WEECHAT_TOKEN;
    delete process.env.PI_CODING_AGENT_DIR;
  });

  await wc.waitFor((m) => m.type === "ready", "python driver ready", 10_000);

  // token + TCP listener on the weechat side (as in the plain TCP test)
  wc.send({ op: "set", name: "token", value: token });
  wc.send({ op: "set", name: "tcp_listen", value: `127.0.0.1:${port}` });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes(`listening on tcp 127.0.0.1:${port}`),
    "tcp listening line"
  );

  // --- phase 1: endpoint + token come from the CONFIG FILE only ---------
  delete process.env.PI_WEECHAT_URL;
  delete process.env.PI_WEECHAT_TOKEN;
  process.env.PI_CODING_AGENT_DIR = dir; // config path = <dir>/pi-weechat.json
  fs.writeFileSync(cfgPath, JSON.stringify({ url: `tcp://127.0.0.1:${port}`, token }));

  await loadExt();
  await mock.fire("session_start");
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("pi connected from 127.0.0.1"),
    "buffer: pi connected using config-file url+token"
  );

  // --- phase 2: env token WINS over a wrong file token -------------------
  await mock.fire("session_shutdown");
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("pi disconnected"),
    "buffer: disconnected before env-precedence phase"
  );

  fs.writeFileSync(cfgPath, JSON.stringify({ url: `tcp://127.0.0.1:${port}`, token: "wrong-file-token" }));
  process.env.PI_WEECHAT_TOKEN = token; // correct secret in the environment
  wc.lines.length = 0; // drop phase-1 lines so waits only match fresh output
  await mock.fire("session_start"); // refreshConfig re-reads file + env
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("pi connected from 127.0.0.1"),
    "buffer: pi connected with env token beating wrong file token"
  );
  assert.ok(
    !wc.lines.some((m) => m.type === "print" && m.text.includes("auth failed")),
    "no auth failure: the env token must win over the config file's"
  );

  await mock.fire("session_shutdown");
});

// End-to-end over the REAL wire: buffer typing → command → fuzzy prompt
// rendered in the buffer → !pick answering it → real session switch.
test("integration: !cd + !pick round trip", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-itg-cd-"));
  const sockPath = path.join(dir, "bridge.sock");
  const projAlpha = path.join(dir, "proj-alpha");
  fs.mkdirSync(projAlpha);
  fs.mkdirSync(path.join(dir, "zzz-unrelated"));

  const wc = startWeechatSide(sockPath);
  const prevHome = process.env.HOME;
  const prevAgentDir = process.env.PI_CODING_AGENT_DIR;
  t.after(async () => {
    try { wc.child.kill("SIGKILL"); } catch {}
    if (prevHome === undefined) delete process.env.HOME; else process.env.HOME = prevHome;
    if (prevAgentDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevAgentDir;
    fs.rmSync(dir, { recursive: true, force: true });
  });

  await wc.waitFor((m) => m.type === "ready", "python driver ready", 10_000);

  delete process.env.PI_WEECHAT_URL;
  delete process.env.PI_WEECHAT_TOKEN;
  process.env.PI_WEECHAT_SOCK = sockPath;
  // Hermetic: the fuzzy search scans $HOME, and SessionManager stores session
  // files under <PI_CODING_AGENT_DIR>/sessions/… — both pointed at the sandbox.
  process.env.HOME = dir;
  process.env.PI_CODING_AGENT_DIR = dir;

  await loadExt();
  await mock.fire("session_start");
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("pi connected"),
    "buffer: pi connected"
  );

  // --- exact path: switches immediately, no prompt on the wire ----------
  wc.send({ op: "input", text: `!cd ${projAlpha}` });
  await waitForMock(() => mock.switchCalls.length >= 1, "switchSession (exact)");
  const hdrFile = mock.switchCalls[0];
  assert.ok(fs.existsSync(hdrFile), "pre-written session file exists on disk");
  const header = JSON.parse(fs.readFileSync(hdrFile, "utf8"));
  assert.equal(header.cwd, projAlpha);
  // emulate pi re-emitting session_start for the resumed session (new cwd)
  await mock.fire("session_start", {}, { ...MOCK_CTX, cwd: projAlpha });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes(projAlpha),
    "buffer: session line with new cwd"
  );

  // --- typo'd path: fuzzy prompt renders in the buffer ------------------
  wc.lines.length = 0;
  wc.send({ op: "input", text: `!cd ${path.join(dir, "proj-alph")}` });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("Which directory?"),
    "buffer: ? prompt title"
  );
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes(projAlpha),
    "buffer: fuzzy match option"
  );
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("➕ create"),
    "buffer: create option"
  );
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("!pick cancel"),
    "buffer: pick hint line"
  );
  await wc.waitFor(
    (m) => m.type === "title" && m.text.includes("awaiting !pick"),
    "title: awaiting !pick"
  );

  // answering with !pick 1 completes the switch
  wc.send({ op: "input", text: "!pick 1" });
  await waitForMock(() => mock.switchCalls.length >= 2, "switchSession (picked)");
  assert.equal(
    JSON.parse(fs.readFileSync(mock.switchCalls[1], "utf8")).cwd,
    projAlpha,
    "picked option became the new cwd"
  );

  // --- no similar dirs: only the create option; !pick 1 mkdirs ----------
  wc.lines.length = 0;
  const brandNew = path.join(dir, "brand-new");
  wc.send({ op: "input", text: `!cd ${brandNew}` });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes(`➕ create ${brandNew} as new project`),
    "buffer: create option only"
  );
  wc.send({ op: "input", text: "!pick 1" });
  await waitForMock(() => mock.switchCalls.length >= 3, "switchSession (create)");
  assert.ok(fs.existsSync(brandNew), "create option mkdir'd the project dir");
  assert.equal(
    JSON.parse(fs.readFileSync(mock.switchCalls[2], "utf8")).cwd,
    brandNew
  );

  // --- !pick cancel: no switch, buffer notes it -------------------------
  wc.lines.length = 0;
  wc.send({ op: "input", text: `!cd ${path.join(dir, "proj-alph")}` });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("Which directory?"),
    "buffer: prompt (cancel case)"
  );
  wc.send({ op: "input", text: "!pick cancel" });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("(cd cancelled)"),
    "buffer: cd cancelled note"
  );
  assert.equal(mock.switchCalls.length, 3, "cancel must not switch sessions");

  await mock.fire("session_shutdown");
});

test("integration: assistant markdown renders through the real extension", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-itg-md-"));
  const sockPath = path.join(dir, "bridge.sock");
  const wc = startWeechatSide(sockPath);
  const prevAgentDir = process.env.PI_CODING_AGENT_DIR;
  process.env.PI_CODING_AGENT_DIR = dir;
  t.after(async () => {
    try { wc.child.kill("SIGKILL"); } catch {}
    if (prevAgentDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevAgentDir;
    fs.rmSync(dir, { recursive: true, force: true });
  });

  await wc.waitFor((m) => m.type === "ready", "python driver ready", 10_000);
  delete process.env.PI_WEECHAT_URL;
  delete process.env.PI_WEECHAT_TOKEN;
  process.env.PI_WEECHAT_SOCK = sockPath;
  await loadExt();
  await mock.fire("session_start");
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("pi connected"),
    "buffer: connected"
  );

  // the stub weechat module maps colors to single letters and attributes to
  // real control codes: M=magenta, E=yellow, BOLD/UNDER are raw \x1a codes
  const BOLD = "\x1a\x01", UNDER = "\x1a\x04", R = "0";

  await mock.fire("agent_start");
  await mock.fire("message_start", { message: { role: "assistant" } });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "# Title\n" },
  });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "Some **bold** and `code`.\n" },
  });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "- item one\n" },
  });
  await mock.fire("message_update", { assistantMessageEvent: { type: "text_end", contentIndex: 0 } });
  await mock.fire("message_end", { message: { role: "assistant" } });

  const head = await wc.waitFor(
    (m) => m.type === "print" && m.text === "M" + BOLD + UNDER + "Title" + R,
    "heading rendered with heading color + bold + underline"
  );
  assert.ok(head.prefix.includes("pi"), "heading keeps the pi nick prefix");
  const para = await wc.waitFor(
    (m) => m.type === "print" && m.text === "Some " + BOLD + "bold and Ecode." + R,
    "emphasis and inline code render, their markers are gone"
  );
  assert.ok(para.text.includes(BOLD), "bold attribute reaches the buffer");
  assert.ok(!wc.lines.some((m) => m.type === "print" && (m.text || "").includes("**")),
    "no literal ** markers left in the buffer");
  await wc.waitFor(
    (m) => m.type === "print" && m.text === "\u2022 item one" + R,
    "list item renders as a bullet"
  );

  // ordering: the heading prints at once, the tool line keeps its place
  await mock.fire("message_start", { message: { role: "assistant" } });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "## Step\n" },
  });
  await mock.fire("tool_execution_start", { toolCallId: "m1", toolName: "read", args: { path: "/tmp/a" } });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "after the tool\n" },
  });
  await mock.fire("message_update", { assistantMessageEvent: { type: "text_end", contentIndex: 0 } });
  await mock.fire("message_end", { message: { role: "assistant" } });
  const h2 = await wc.waitFor(
    (m) => m.type === "print" && m.text === "M" + BOLD + "Step" + R,
    "level-2 heading rendered"
  );
  const tool = await wc.waitFor(
    (m) => m.type === "print" && (m.tags || "").includes("nick_read"),
    "tool line under the tool nick"
  );
  const after = await wc.waitFor(
    (m) => m.type === "print" && m.text === "after the tool" + R,
    "paragraph after the tool line"
  );
  assert.ok(wc.lines.indexOf(h2) < wc.lines.indexOf(tool), "heading prints before the tool line");
  assert.ok(wc.lines.indexOf(tool) < wc.lines.indexOf(after), "tool line before the following paragraph");

  // !markdown off: the very same text arrives with its markers
  wc.send({ op: "input", text: "!markdown off" });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("markdown rendering: off"),
    "markdown switched off"
  );
  await mock.fire("message_start", { message: { role: "assistant" } });
  await mock.fire("message_update", {
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "# Raw heading\n" },
  });
  await mock.fire("message_update", { assistantMessageEvent: { type: "text_end", contentIndex: 0 } });
  await mock.fire("message_end", { message: { role: "assistant" } });
  await wc.waitFor(
    (m) => m.type === "print" && m.text === "# Raw heading" + R,
    "markdown off restores the raw text, markers included"
  );
  wc.send({ op: "input", text: "!markdown on" });
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
