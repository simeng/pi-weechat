// Shared harness for the ask/!pick integration tests: the REAL extension (TS)
// plus the REAL weechat script (Python, via py_driver.py) over a Unix socket.
// Existing test files keep their own inline copies; new tests import this one.
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const PY_DRIVER = new URL("./py_driver.py", import.meta.url).pathname;

// !pick replies are rate-limited to 5/s on the buffer side — pace ourselves.
export const PICK_PAUSE_MS = 1_100;
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function startWeechatSide(sockPath) {
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
        const timer = setTimeout(() => reject(new Error(`timeout waiting for ${what}`)), ms);
        waiters.push({ pred, resolve: (v) => { clearTimeout(timer); resolve(v); } });
      });
    },
    send(op) {
      child.stdin.write(JSON.stringify(op) + "\n");
    },
  };
}

/** pi stand-in: records registered tools and returns non-undefined handler results. */
export function makePiMock(ctx) {
  const handlers = {};
  const registeredTools = {};
  return {
    handlers,
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
      getSessionName: () => "ask-session",
    },
    fire: async (name, event = {}, c = ctx) => {
      const outs = [];
      for (const fn of handlers[name] ?? []) {
        const r = await fn(event, c);
        if (r !== undefined) outs.push(r);
      }
      return outs;
    },
  };
}

/** Hermetic config dir + a socket path; returns { dir, sockPath, applyEnv, restoreEnv }. */
export function makeBridgeEnv(tag) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `pi-wc-${tag}-`));
  const sockPath = path.join(dir, "bridge.sock");
  const saved = {
    PI_CODING_AGENT_DIR: process.env.PI_CODING_AGENT_DIR,
    PI_WEECHAT_URL: process.env.PI_WEECHAT_URL,
    PI_WEECHAT_TOKEN: process.env.PI_WEECHAT_TOKEN,
    PI_WEECHAT_SOCK: process.env.PI_WEECHAT_SOCK,
    PI_WEECHAT_PICK: process.env.PI_WEECHAT_PICK,
    PI_WEECHAT_PICK_TOOLS: process.env.PI_WEECHAT_PICK_TOOLS,
    PI_WEECHAT_ASK_TOOL: process.env.PI_WEECHAT_ASK_TOOL,
  };
  const applyEnv = () => {
    delete process.env.PI_WEECHAT_URL;
    delete process.env.PI_WEECHAT_TOKEN;
    delete process.env.PI_WEECHAT_PICK;
    delete process.env.PI_WEECHAT_PICK_TOOLS;
    delete process.env.PI_WEECHAT_ASK_TOOL;
    process.env.PI_CODING_AGENT_DIR = dir;
    process.env.PI_WEECHAT_SOCK = sockPath;
  };
  const restoreEnv = () => {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  };
  const cleanup = () => fs.rmSync(dir, { recursive: true, force: true });
  return { dir, sockPath, applyEnv, restoreEnv, cleanup };
}

/** Write a pi-weechat.json into the hermetic config dir. */
export function writeConfig(dir, obj) {
  fs.writeFileSync(path.join(dir, "pi-weechat.json"), JSON.stringify(obj));
}

export async function loadExtension(mock) {
  const mod = await import(new URL("../extensions/weechat-bridge.ts", import.meta.url).href);
  mod.default(mock.api);
}
