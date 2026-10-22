// `ui_cancel`: a prompt pi stops waiting for must be released on the buffer.
// A question whose timeout expires (or that gets answered in another UI)
// leaves no armed `?` prompt behind, so `!pick` afterwards says there is
// nothing to pick instead of answering a dead question. Real extension +
// real pi_bridge.py over a live socket.
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
  cwd: "/tmp/ui-cancel",
  model: { provider: "prov", id: "model-1" },
  isIdle: () => true,
  abort() {},
};

test("integration: abandoned prompt is released with ui_cancel", async (t) => {
  const env = makeBridgeEnv("ui-cancel");
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
  const mock = makePiMock(MOCK_CTX, ["read", "bash"]);
  await loadExtension(mock);
  await mock.fire("session_start");
  await wc.waitFor((m) => m.type === "print" && m.text.includes("pi connected"), "buffer: pi connected");

  // --- question with a short timeout, never answered --------------------
  const outs = await mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user",
      toolCallId: "u1",
      input: { question: "Will nobody answer me?", options: ["Yes", "No"], timeout: 400 },
    },
  );
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Will nobody answer me?"), "buffer: ? prompt");

  // Expired: the hook falls back to the tool's own UI (no block result) and
  // the buffer prompt is released.
  assert.deepEqual(outs, [], "timed-out question must not block the tool call");
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("dismissed by pi"),
    "buffer: prompt released",
    8_000,
  );

  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick 1" });
  await wc.waitFor(
    (m) => m.type === "print" && m.text.includes("nothing to pick"),
    "buffer: nothing to pick after release",
  );

  // --- an answered question is NOT released ----------------------------
  const pending = mock.fire(
    "tool_call",
    {
      type: "tool_call",
      toolName: "ask_user",
      toolCallId: "u2",
      input: { question: "Answered in time?", options: ["Yes", "No"] },
    },
  );
  await wc.waitFor((m) => m.type === "print" && m.text.includes("Answered in time?"), "buffer: second ? prompt");
  await sleep(PICK_PAUSE_MS);
  wc.send({ op: "input", text: "!pick 1" });
  const res = await pending;
  assert.equal(res.length, 1, "answered question blocks the call");
  assert.match(res[0].reason, /User answered: "Yes"/);
  await sleep(300);
  assert.ok(
    !wc.lines.some((m) => m.type === "print" && m.text.includes("dismissed by pi") && m.text.includes("Answered")),
    "an answered prompt must not be reported as released",
  );
});
