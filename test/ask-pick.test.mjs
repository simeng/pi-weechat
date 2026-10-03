// End-to-end: the REAL extension (TS) + REAL weechat script (Python) over a
// Unix socket. pi-ask-user is NOT installed in this scenario (getAllTools →
// []), so the bridge registers its built-in ask_user fallback; decision
// questions raised via the tool_call hook are answered from the buffer with
// !pick, and a blocked tool_call returns the choice to the LLM as `reason`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const PY_DRIVER = new URL("./py_driver.py", import.meta.url).pathname;
// !pick replies are rate-limited to 5/s on the buffer side — pace ourselves.
const PICK_PAUSE_MS = 1_100;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

const MOCK_CTX = {
  cwd: "/tmp/ask-pick",
  model: { provider: "prov", id: "model-ask" },
  isIdle: () => true,
  abort() {},
  // pi-ask-user is NOT installed in this scenario → no ask_user tool.
  getAllTools: () => [],
};

function makePiMock() {
  const handlers = {};
  const registeredTools = {};
  return {
    registeredTools,
    api: {
      on: (name, fn) => {
        (handlers[name] ??= []).push(fn);
      },
      registerTool: (tool) => {
        registeredTools[tool.name] = tool;
      },
      registerCommand: () => {},
      sendUserMessage: () => {},
      getSessionName: () => "ask-pick-session",
    },
    // Returns non-undefined handler results (e.g. the tool_call block result).
    fire: async (name, event = {}, ctx = MOCK_CTX) => {
      const outs = [];
      for (const fn of handlers[name] ?? []) {
        const r = await fn(event, ctx);
        if (r !== undefined) outs.push(r);
      }
      return outs;
    },
  };
}

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

test("integration: ask_user → WeeChat !pick (built-in fallback tool)", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-ask-pick-"));
  const sockPath = path.join(dir, "bridge.sock");

  const wc = startWeechatSide(sockPath);
  const prevAgentDir = process.env.PI_CODING_AGENT_DIR;
  t.after(async () => {
    try { wc.child.kill("SIGKILL"); } catch {}
    if (prevAgentDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevAgentDir;
    delete process.env.PI_WEECHAT_SOCK;
    fs.rmSync(dir, { recursive: true, force: true });
  });

  await wc.waitFor((m) => m.type === "ready", "python driver ready", 10_000);
  assert.ok(fs.existsSync(sockPath), "weechat side created the socket");

  delete process.env.PI_WEECHAT_URL;
  delete process.env.PI_WEECHAT_TOKEN;
  process.env.PI_WEECHAT_SOCK = sockPath;
  process.env.PI_CODING_AGENT_DIR = dir; // hermetic config lookup
  await loadExt();

  // --- no bridge yet: the hook must NOT intercept (local UI takes over) ---
  let outs = await mock.fire(
    "tool_call",
    { type: "tool_call", toolName: "ask_user", toolCallId: "q0", input: { question: "Too early?" } },
    MOCK_CTX,
  );
  assert.equal(outs.length, 0, "no bridge → tool_call hook falls through");

  await mock.fire("session_start");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("pi connected"), "buffer: pi connected");

  // fallback ask_user registered (none provided by another extension)
  const tool = mock.registeredTools["ask_user"];
  assert.ok(tool, "built-in fallback ask_user is registered");
  assert.equal(tool.executionMode, "sequential");
  assert.ok(tool.parameters && typeof tool.parameters === "object", "has a parameter schema");

  // Run a live tool prompt so the title also verifies pause accounting and
  // that accepting !pick clears only the prompt hint, not the tool state.
  await mock.fire("agent_start");
  await mock.fire("tool_execution_start", {
    toolCallId: "q1", toolName: "ask_user", args: { question: "Which color?" },
  });
  // --- flow 1: single-select options → !pick <n> --------------------------
  const p1 = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user",
      toolCallId: "q1",
      input: {
        question: "Which color?",
        context: "a short context line",
        options: [{ title: "Red" }, { title: "Blue" }],
        allowFreeform: false,
      },
    },
    MOCK_CTX,
  );
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Which color?"), "buffer: question");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Context:"), "buffer: context block");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("1. Red"), "buffer: option 1");
  assert.ok(
    !wc.lines.some((m) => m.type === "print" && m.text.includes("Type custom response")),
    "allowFreeform:false → no freeform sentinel offered"
  );
  await wc.waitFor((m) => m.type === "title" && m.text.includes("awaiting !pick"), "title: awaiting !pick");
  const titleWhilePrompt = wc.lines.filter((m) => m.type === "title").at(-1)?.text ?? "";
  await mock.fire("ui_prompt_start", { kind: "select", reason: "ui_prompt" });

  await sleep(PICK_PAUSE_MS);
  assert.equal(
    wc.lines.filter((m) => m.type === "title").at(-1)?.text,
    titleWhilePrompt,
    "WeeChat !pick wait must pause the active run clock",
  );
  wc.send({ op: "input", text: "!pick 2" });
  outs = await p1;
  assert.equal(outs.length, 1, "hook blocked the tool call");
  assert.equal(outs[0].block, true);
  assert.match(outs[0].reason, /User answered: "Blue"/);
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes('(question answered: "Blue")'),
    "buffer: answered note",
  );
  const titleAfterAnswer = wc.lines.filter((m) => m.type === "title").at(-1)?.text ?? "";
  const runSeconds = (title) => Number(title.match(/ · run (\d+)s/)?.[1] ?? 0);
  assert.match(titleAfterAnswer, /\(tool: ask_user · run \d+s\)$/);
  assert.doesNotMatch(titleAfterAnswer, /awaiting !pick/);
  await sleep(PICK_PAUSE_MS * 2);
  const stillPiPaused = wc.lines.filter((m) => m.type === "title").at(-1)?.text ?? "";
  assert.equal(stillPiPaused, titleAfterAnswer, "!pick must not release the overlapping Pi UI pause");
  const elapsedBeforeResume = runSeconds(titleAfterAnswer);
  const linesBeforePiResume = wc.lines.length;
  await mock.fire("ui_prompt_end", { kind: "select", reason: "ui_prompt" });
  await wc.waitFor(
    (m) => m.type === "title" && wc.lines.indexOf(m) >= linesBeforePiResume &&
      runSeconds(m.text) > elapsedBeforeResume,
    "title: run timer resumes after overlapping prompts",
    4000,
  );
  const afterPiResume = wc.lines.filter((m) => m.type === "title").at(-1)?.text ?? "";
  assert.ok(runSeconds(afterPiResume) > elapsedBeforeResume, afterPiResume);
  await mock.fire("tool_execution_end", {
    toolCallId: "q1", isError: false, result: { content: [{ type: "text", text: "answered" }] },
  });
  await mock.fire("agent_settled");

  // A run that settles (e.g. after abort) while paused stays frozen when a
  // late prompt-end arrives, and the stale end cannot affect a new run.
  await mock.fire("agent_start");
  await mock.fire("ui_prompt_start", { kind: "input", reason: "ui_prompt" });
  await sleep(PICK_PAUSE_MS * 2);
  const linesBeforeSettle = wc.lines.length;
  await mock.fire("agent_settled");
  await wc.waitFor(
    (m) => m.type === "title" && wc.lines.indexOf(m) >= linesBeforeSettle &&
      /\(idle · last run \d+s · 0 turns\)$/.test(m.text),
    "title: run settled while prompt paused",
  );
  const abortedTitle = wc.lines.filter((m) => m.type === "title").at(-1)?.text ?? "";
  await mock.fire("ui_prompt_end", { kind: "input", reason: "ui_prompt" });
  await sleep(PICK_PAUSE_MS * 2);
  assert.equal(
    wc.lines.filter((m) => m.type === "title").at(-1)?.text,
    abortedTitle,
    "late prompt completion must not restart settled clocks",
  );

  await mock.fire("agent_start");
  await mock.fire("turn_start", { turnIndex: 0, timestamp: Date.now() });
  const linesBeforeLateEnd = wc.lines.length;
  await mock.fire("ui_prompt_end", { kind: "input", reason: "ui_prompt" });
  await wc.waitFor(
    (m) => {
      if (m.type !== "title" || wc.lines.indexOf(m) < linesBeforeLateEnd) return false;
      const run = runSeconds(m.text);
      const turn = Number(m.text.match(/ · (\d+)s · turn 1\)$/)?.[1] ?? 0);
      return run > 0 && turn > 0;
    },
    "title: new run continues after stale prompt end",
    4000,
  );
  const afterLateEnd = wc.lines.filter((m) => m.type === "title").at(-1)?.text ?? "";
  assert.match(afterLateEnd, /\(thinking… · run [1-9]\d*s · [1-9]\d*s · turn 1\)$/);
  await mock.fire("agent_settled");

  // --- flow 2: freeform sentinel → follow-up input prompt -----------------
  wc.lines.length = 0;
  const p2 = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user",
      toolCallId: "q2",
      input: { question: "Pick or type", options: [{ title: "Alpha" }, { title: "Beta" }] },
    },
    MOCK_CTX,
  );
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("Type custom response"),
    "buffer: freeform sentinel option",
  );
  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick 3" }); // the sentinel → input follow-up
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("reply !pick <your answer>"),
    "buffer: freeform input prompt",
  );
  wc.send({ op: "input", text: "!pick make it green" });
  outs = await p2;
  assert.equal(outs[0].block, true);
  assert.match(outs[0].reason, /User answered: "make it green"/);

  // --- flow 3: no options → direct freeform input --------------------------
  wc.lines.length = 0;
  const p3 = mock.fire(
    "tool_call",
    { type: "tool_call", toolName: "ask_user", toolCallId: "q3", input: { question: "How many?" } },
    MOCK_CTX,
  );
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("reply !pick <your answer>"),
    "buffer: input prompt (no options)",
  );
  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick four" });
  outs = await p3;
  assert.equal(outs[0].block, true);
  assert.match(outs[0].reason, /User answered: "four"/);

  // --- flow 4: !pick cancel → blocked with a cancellation result ----------
  wc.lines.length = 0;
  const p4 = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user",
      toolCallId: "q4",
      input: { question: "Sure?", options: [{ title: "Yes" }, { title: "No" }] },
    },
    MOCK_CTX,
  );
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Sure?"), "buffer: cancel-flow question");
  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick cancel" });
  outs = await p4;
  assert.equal(outs[0].block, true);
  assert.match(outs[0].reason, /cancelled/);
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("(question cancelled)"),
    "buffer: cancelled note",
  );

  // --- flow 5: fallback tool's LOCAL path (no bridge / no UI) -------------
  const noUi = await tool.execute("t1", { question: "Local?" }, undefined, undefined, { hasUI: false });
  assert.match(noUi.content[0].text, /Ask requires interactive mode/);
  assert.equal(noUi.isError, true);

  const uiCtx = {
    hasUI: true,
    ui: { select: async () => "Blue", input: async () => undefined },
  };
  const local = await tool.execute(
    "t2",
    { question: "Pick?", options: [{ title: "Red" }, { title: "Blue" }] },
    undefined,
    undefined,
    uiCtx,
  );
  assert.match(local.content[0].text, /User answered: Blue/);

  const cancelledLocal = await tool.execute(
    "t3",
    { question: "Q?" },
    undefined,
    undefined,
    { hasUI: true, ui: { input: async () => undefined } },
  );
  assert.match(cancelledLocal.content[0].text, /User cancelled the question/);

  await mock.fire("session_shutdown");
});
