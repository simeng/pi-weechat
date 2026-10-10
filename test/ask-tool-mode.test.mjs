// Which ask tool a session ends up with. The bridge's built-in ask_user is a
// FALLBACK: with "auto" (the default) it is skipped whenever any other
// question-shaped tool is loaded (ask_user_question, askUserQuestion, …), so a
// session never carries two tools for the same job. PI_WEECHAT_ASK_TOOL /
// "askTool" overrides that: "off" never registers, "force" always does.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadExtension, makeBridgeEnv, makePiMock, writeConfig } from "./wc-harness.mjs";

function ctxWithTools(names) {
  return {
    cwd: "/tmp/ask-tool-mode",
    model: { provider: "prov", id: "m" },
    isIdle: () => true,
    abort() {},
    getAllTools: () => names.map((name) => ({ name, description: "provided by another extension" })),
  };
}

/** Load the extension against a dead socket and report what it registered. */
async function registeredAskTool({ tools = [], env, config } = {}) {
  const env2 = makeBridgeEnv("ask-tool-mode");
  env2.applyEnv();
  try {
    if (env) Object.assign(process.env, env);
    if (config) writeConfig(env2.dir, config);
    const mock = makePiMock(ctxWithTools(tools));
    await loadExtension(mock);
    await mock.fire("session_start");
    await mock.fire("session_shutdown");
    return mock.registeredTools["ask_user"];
  } finally {
    env2.restoreEnv();
    env2.cleanup();
  }
}

test("auto: a foreign ask_user_question provider suppresses the built-in fallback", async () => {
  const tool = await registeredAskTool({ tools: ["read", "bash", "ask_user_question"] });
  assert.equal(tool, undefined, "ask_user_question already covers asking — no second ask tool");
});

test("auto: camelCase and dashed ask-tool names are recognized too", async () => {
  for (const name of ["askUserQuestion", "ask-user-question", "Ask_User_Question"]) {
    const tool = await registeredAskTool({ tools: [name] });
    assert.equal(tool, undefined, `${name} must count as a question tool`);
  }
});

test("auto: a non-question tool named *question* is not mistaken for an ask tool", async () => {
  const tool = await registeredAskTool({ tools: ["questionnaire_stats"] });
  assert.ok(tool, "no question tool loaded → the fallback is registered");
  assert.equal(tool.name, "ask_user");
});

test("auto: nothing else loaded → the built-in fallback is registered", async () => {
  const tool = await registeredAskTool({ tools: ["read", "bash"] });
  assert.ok(tool, "fallback ask_user registered");
});

test("askTool=off: never registers, even with no other question tool", async () => {
  const viaEnv = await registeredAskTool({ env: { PI_WEECHAT_ASK_TOOL: "off" } });
  assert.equal(viaEnv, undefined, "PI_WEECHAT_ASK_TOOL=off → no ask_user");
  const viaFile = await registeredAskTool({ config: { askTool: "off" } });
  assert.equal(viaFile, undefined, '"askTool": "off" in the config file → no ask_user');
});

test("askTool=force: registers alongside a foreign question tool", async () => {
  const tool = await registeredAskTool({
    tools: ["ask_user_question"],
    env: { PI_WEECHAT_ASK_TOOL: "force" },
  });
  assert.ok(tool, "force registers the fallback even though ask_user_question exists");
});

test("a tool literally named ask_user always wins — no duplicate name", async () => {
  for (const mode of [undefined, "force"]) {
    const tool = await registeredAskTool({
      tools: ["ask_user"],
      env: mode ? { PI_WEECHAT_ASK_TOOL: mode } : undefined,
    });
    assert.equal(tool, undefined, `ask_user already exists (askTool=${mode ?? "auto"})`);
  }
});

test("askTool routing still intercepts a foreign tool's questions", async () => {
  const env2 = makeBridgeEnv("ask-tool-route");
  env2.applyEnv();
  try {
    const ctx = ctxWithTools(["ask_user_question"]);
    const mock = makePiMock(ctx);
    await loadExtension(mock);
    await mock.fire("session_start");
    assert.equal(mock.registeredTools["ask_user"], undefined, "no duplicate ask tool");
    // No bridge is connected here (dead socket), so the hook must fall through
    // and leave ask_user_question's own UI in charge.
    const outs = await mock.fire(
      "tool_call",
      {
        type: "tool_call",
        toolName: "ask_user_question",
        toolCallId: "q1",
        input: { questions: [{ question: "Which one?", header: "Pick", options: [{ label: "A", description: "a" }] }] },
      },
      ctx,
    );
    assert.equal(outs.length, 0, "no bridge → no interception");
    await mock.fire("session_shutdown");
  } finally {
    env2.restoreEnv();
    env2.cleanup();
  }
});
