// End-to-end: a `questions[]` questionnaire (the ask_user_question shape used
// by rpiv-ask-user-question) asked through the WeeChat buffer and answered
// with !pick. The bridge blocks the tool call and hands the composed answers
// back to the LLM as the tool result — the ask extension itself is untouched.
//
// This scenario mirrors a real setup: ask_user_question IS loaded, so the
// bridge must not register its built-in ask_user fallback at all.
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

const MOCK_CTX = {
  cwd: "/tmp/ask-pick-multi",
  model: { provider: "prov", id: "model-multi" },
  isIdle: () => true,
  abort() {},
  // rpiv-ask-user-question is loaded → the fallback must stay unregistered.
  getAllTools: () => [{ name: "ask_user_question", description: "provided by rpiv" }],
};

test("integration: ask_user_question questions[] → WeeChat !pick", async (t) => {
  const env = makeBridgeEnv("ask-pick-multi");
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
  const mock = makePiMock(MOCK_CTX);
  await loadExtension(mock);
  await mock.fire("session_start");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("pi connected"), "buffer: pi connected");

  assert.equal(
    mock.registeredTools["ask_user"],
    undefined,
    "ask_user_question is loaded → no built-in ask_user fallback",
  );

  // --- questionnaire: 2 questions, one single-select + one multi-select ----
  const pending = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user_question",
      toolCallId: "m1",
      input: {
        questions: [
          {
            question: "Which auth library?",
            header: "Auth method",
            options: [
              { label: "OAuth", description: "delegate to a provider" },
              { label: "JWT", description: "self-issued tokens", preview: "sign(payload, secret)" },
            ],
          },
          {
            question: "Which features do you want to enable?",
            header: "Features",
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
    MOCK_CTX,
  );

  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("Q1/2 · Auth method — Which auth library?"),
    "buffer: question 1 with header chip",
  );
  await wc.waitFor((m) => m.type === "print" && m.text.includes("1. OAuth"), "buffer: option 1");
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("sign(payload, secret)"),
    "buffer: option preview folded into the description line",
  );
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("Type custom response"),
    "buffer: freeform option",
  );

  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick 1" });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes('(question 1/2 answered: "OAuth")'),
    "buffer: per-question answer note",
  );

  // The second question is asked only after the first is answered: the buffer
  // tracks one pending prompt at a time.
  await wc.waitFor(
    (m) =>
      m.type === "print" &&
      m.text.includes("Q2/2 · Features — Which features do you want to enable?"),
    "buffer: question 2",
  );
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("!pick 1,3 (multiple)"),
    "buffer: multiSelect hint",
  );

  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick 1,3" });
  const outs = await pending;
  assert.equal(outs.length, 1, "hook blocked the tool call");
  assert.equal(outs[0].block, true);
  const reason = outs[0].reason;
  assert.match(reason, /User answered 2 question\(s\) \(via WeeChat !pick\)/);
  assert.match(reason, /1\. "Which auth library\?" → "OAuth"/);
  assert.match(reason, /2\. "Which features do you want to enable\?" → "Refresh", "Audit"/);
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes('(question 2/2 answered: "Refresh", "Audit")'),
    "buffer: second answer note",
  );

  // --- cancel mid-questionnaire → an explicit "no answers" result ----------
  const p2 = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user_question",
      toolCallId: "m2",
      input: {
        questions: [
          { question: "Still ok?", header: "Check", options: [{ label: "Yes", description: "y" }] },
          { question: "Ship it?", header: "Ship", options: [{ label: "Now", description: "n" }] },
        ],
      },
    },
    MOCK_CTX,
  );
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Q1/2 · Check — Still ok?"), "buffer: cancel-flow Q1");
  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick 1" });
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Q2/2 · Ship — Ship it?"), "buffer: cancel-flow Q2");
  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick cancel" });
  const outs2 = await p2;
  assert.equal(outs2[0].block, true);
  assert.match(outs2[0].reason, /User cancelled question 2\/2/);
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("(question 2/2 cancelled)"),
    "buffer: cancelled note",
  );

  // --- a lone question keeps the plain (unnumbered) wording ----------------
  const p3 = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user_question",
      toolCallId: "m3",
      input: {
        questions: [
          { question: "Which color?", options: [{ label: "Red", description: "warm" }] },
        ],
      },
    },
    MOCK_CTX,
  );
  await wc.waitFor((m) => m.type === "print" && m.text.includes("? Which color?"), "buffer: single question, no Q prefix");
  assert.ok(
    !wc.lines.some((m) => m.type === "print" && m.text.includes("Q1/1")),
    "one question is not numbered",
  );
  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick 1" });
  const outs3 = await p3;
  assert.equal(outs3[0].block, true);
  assert.match(outs3[0].reason, /User answered: "Red" \(via WeeChat !pick\)/);

  await mock.fire("session_shutdown");
});
