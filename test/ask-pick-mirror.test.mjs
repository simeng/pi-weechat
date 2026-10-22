// pick=mirror: the question is asked in the pi terminal AND in the WeeChat
// buffer at once, and the first answer wins — the other UI is dismissed
// (terminal dialog aborted, buffer prompt released with ui_cancel). Real
// extension + real pi_bridge.py over a live socket; the pi dialog is a mock
// that resolves on demand and resolves `undefined` when aborted, exactly like
// pi's own extension selector.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  PICK_PAUSE_MS,
  loadExtension,
  makeBridgeEnv,
  makePiMock,
  sleep,
  startWeechatSide,
} from "./wc-harness.mjs";

/** pi dialog stand-in: records calls, resolves them from the test. */
function makeUi() {
  const calls = [];
  let settle = null;
  const pending = (method) => (title, arg, opts) => {
    const call = { method, title, arg, signal: opts?.signal, aborted: false };
    calls.push(call);
    return new Promise((resolve) => {
      opts?.signal?.addEventListener("abort", () => {
        call.aborted = true;
        resolve(undefined); // pi's selector resolves undefined on abort
      });
      settle = (value) => resolve(value);
    });
  };
  return {
    calls,
    select: pending("select"),
    input: pending("input"),
    answer(value) {
      const fn = settle;
      settle = null;
      assert.ok(fn, "no dialog is open");
      fn(value);
    },
  };
}

async function startMirror(t, tag, ctx) {
  const env = makeBridgeEnv(tag);
  const wc = startWeechatSide(env.sockPath);
  t.after(async () => {
    try {
      wc.child.kill("SIGKILL");
    } catch {}
    env.restoreEnv();
    env.cleanup();
  });
  await wc.waitFor((m) => m.type === "ready", "python driver ready", 10_000);
  env.applyEnv();
  process.env.PI_WEECHAT_PICK = "mirror";
  const mock = makePiMock(ctx, ["read", "bash", "ask_user_question"]);
  await loadExtension(mock);
  await mock.fire("session_start");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("pi connected"), "buffer: pi connected");
  return { mock, wc };
}

test("mirror: terminal answer wins and releases the buffer prompt", async (t) => {
  const ui = makeUi();
  const ctx = {
    cwd: "/tmp/ask-mirror",
    model: { provider: "prov", id: "model-m" },
    isIdle: () => true,
    hasUI: true,
    mode: "tui",
    ui,
    abort() {},
  };
  const { mock, wc } = await startMirror(t, "ask-mirror-term", ctx);

  const pending = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user_question",
      toolCallId: "m1",
      input: {
        questions: [
          {
            question: "Which editor?",
            header: "Editor",
            options: [{ label: "vim" }, { label: "emacs" }],
          },
        ],
      },
    },
  );

  // both UIs are armed
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Which editor?"), "buffer: ? prompt");
  assert.equal(ui.calls.length, 1, "the pi terminal dialog is open too");
  assert.equal(ui.calls[0].method, "select");
  assert.deepEqual(ui.calls[0].arg, ["vim", "emacs", "✏️ Type custom response..."]);

  await sleep(PICK_PAUSE_MS);
  ui.answer("emacs");
  const outs = await pending;
  assert.equal(outs.length, 1, "the call is blocked with an answer");
  assert.match(outs[0].reason, /User answered: "emacs" \(in the pi terminal\)/);

  // the buffer prompt is released, and the terminal dialog was not aborted
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("dismissed by pi"),
    "buffer: prompt released",
  );
  assert.equal(ui.calls[0].aborted, false, "the answering dialog stays settled");
  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick 1" });
  await wc.waitFor((m) => m.type === "print" && m.text.includes("nothing to pick"), "buffer: nothing to pick");
});

test("mirror: buffer answer wins and dismisses the terminal dialog", async (t) => {
  const ui = makeUi();
  const ctx = {
    cwd: "/tmp/ask-mirror-wc",
    model: { provider: "prov", id: "model-m2" },
    isIdle: () => true,
    hasUI: true,
    mode: "tui",
    ui,
    abort() {},
  };
  const { mock, wc } = await startMirror(t, "ask-mirror-wc", ctx);

  const pending = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user_question",
      toolCallId: "m2",
      input: {
        questions: [{ question: "Ship it?", options: [{ label: "No" }, { label: "Yes" }] }],
      },
    },
  );
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Ship it?"), "buffer: ? prompt");
  assert.equal(ui.calls.length, 1, "the terminal dialog is open");

  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick 2" });
  const outs = await pending;
  assert.match(outs[0].reason, /User answered: "Yes" \(via WeeChat !pick\)/);
  assert.equal(ui.calls[0].aborted, true, "the terminal dialog is dismissed");
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes('(question answered: "Yes")'),
    "buffer: answer note",
  );
});

test("mirror: multiSelect is answered from the terminal as a comma list", async (t) => {
  const ui = makeUi();
  const ctx = {
    cwd: "/tmp/ask-mirror-multi",
    model: { provider: "prov", id: "model-m3" },
    isIdle: () => true,
    hasUI: true,
    mode: "tui",
    ui,
    abort() {},
  };
  const { mock, wc } = await startMirror(t, "ask-mirror-multi", ctx);

  const pending = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user_question",
      toolCallId: "m3",
      input: {
        questions: [
          {
            question: "Which features?",
            options: [
              { label: "Refresh", description: "renew tokens" },
              { label: "Scopes", description: "narrow permissions" },
              { label: "Audit", description: "log every grant" },
            ],
            multiSelect: true,
          },
        ],
      },
    },
  );
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Which features?"), "buffer: ? prompt");
  assert.equal(ui.calls[0].method, "input", "ui.select is single-choice, so multiple uses an input dialog");
  assert.match(ui.calls[0].title, /1\. Refresh — renew tokens/);

  await sleep(PICK_PAUSE_MS);
  ui.answer("1, Audit"); // numbers and exact labels, mixed
  const outs = await pending;
  assert.match(outs[0].reason, /User answered: "Refresh", "Audit" \(in the pi terminal\)/);
});

test("mirror without a dialog UI (print mode) still answers in the buffer", async (t) => {
  const ctx = {
    cwd: "/tmp/ask-mirror-noui",
    model: { provider: "prov", id: "model-m4" },
    isIdle: () => true,
    hasUI: false,
    mode: "print",
    abort() {},
  };
  const { mock, wc } = await startMirror(t, "ask-mirror-noui", ctx);

  const pending = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user",
      toolCallId: "m4",
      input: { question: "Only the buffer?", options: ["Yes", "No"] },
    },
  );
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Only the buffer?"), "buffer: ? prompt");
  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick 1" });
  const outs = await pending;
  assert.match(outs[0].reason, /User answered: "Yes" \(via WeeChat !pick\)/);
});

test("mirror: dismissing the terminal dialog is a cancellation, not a fallback", async (t) => {
  const ui = makeUi();
  const ctx = {
    cwd: "/tmp/ask-mirror-esc",
    model: { provider: "prov", id: "model-m5" },
    isIdle: () => true,
    hasUI: true,
    mode: "tui",
    ui,
    abort() {},
  };
  const { mock, wc } = await startMirror(t, "ask-mirror-esc", ctx);

  const pending = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user",
      toolCallId: "m5",
      input: { question: "Esc key?", options: ["Yes", "No"] },
    },
  );
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Esc key?"), "buffer: ? prompt");
  await sleep(PICK_PAUSE_MS);
  ui.answer(undefined); // pi's selector resolves undefined when dismissed
  const outs = await pending;
  assert.match(outs[0].reason, /User cancelled the question in the pi terminal/);
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("(question cancelled)"),
    "buffer: cancelled note",
  );
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("dismissed by pi"),
    "buffer: prompt released",
  );
});
