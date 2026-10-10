// When another extension already provides an ask_user tool (e.g. pi-ask-user
// is installed), the bridge must NOT register its built-in fallback. Runs in
// its own process: node --test isolates test files, and the extension module
// is a per-process singleton (the registration decision happens once per load).
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

// No getAllTools here: pi's event ctx has none. The inventory is on the API.
const MOCK_CTX = {
  cwd: "/tmp/ask-pick-present",
  model: { provider: "prov", id: "m" },
  isIdle: () => true,
  abort() {},
};

// Simulates pi-ask-user (or any other ask provider) already being loaded.
const MOCK_TOOLS = [
  {
    name: "ask_user",
    description: "provided by another extension",
    parameters: null,
    promptGuidelines: [],
  },
];

function makePiMock() {
  const handlers = {};
  const registeredTools = {};
  return {
    handlers,
    registeredTools,
    api: {
      getAllTools: () => MOCK_TOOLS,
      on: (name, fn) => {
        (handlers[name] ??= []).push(fn);
      },
      registerTool: (tool) => {
        registeredTools[tool.name] = tool;
      },
      registerCommand: () => {},
      sendUserMessage: () => {},
      getSessionName: () => "ask-present-session",
    },
    // Returns non-undefined handler results (e.g. a tool_call block result).
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

test("no fallback ask_user registered when another extension provides one", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-ask-present-"));
  // Hermetic config + a dead socket path: never touch the real ~/.pi/agent/
  // config or a live bridge socket.
  t.after(() => {
    if (process.env.PI_CODING_AGENT_DIR === dir) delete process.env.PI_CODING_AGENT_DIR;
    if (process.env.PI_WEECHAT_SOCK === sockPath) delete process.env.PI_WEECHAT_SOCK;
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const sockPath = path.join(dir, "no-such-bridge.sock");
  process.env.PI_CODING_AGENT_DIR = dir;
  delete process.env.PI_WEECHAT_URL;
  delete process.env.PI_WEECHAT_TOKEN;
  process.env.PI_WEECHAT_SOCK = sockPath; // ECONNREFUSED → harmless backoff retries

  const mod = await import(
    new URL("../extensions/weechat-bridge.ts", import.meta.url).href
  );
  const mock = makePiMock();
  mod.default(mock.api);

  await mock.fire("session_start");
  assert.equal(
    mock.registeredTools["ask_user"],
    undefined,
    "bridge must not register ask_user when another extension provides it"
  );

  // The tool_call hook is registered (routing works for the foreign tool)
  // but has nothing to do locally — firing it with no connection falls through.
  const outs = await mock.fire(
    "tool_call",
    { type: "tool_call", toolName: "ask_user", toolCallId: "q1", input: { question: "Which one?" } },
    MOCK_CTX,
  );
  assert.equal(outs.length, 0, "no bridge connected → hook must not intercept");

  await mock.fire("session_shutdown");
});
