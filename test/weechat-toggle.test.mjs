// `/weechat on|off|status`: the master switch (PI_WEECHAT_ENABLED / `enabled`
// in pi-weechat.json) decides what the extension registers at all — off means
// nothing but `/weechat`, so a running session can unload and reload the bridge
// through ctx.reload() without touching pi's settings.json. Pure mock: no
// WeeChat side, no socket.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { loadExtension, makeBridgeEnv, makePiMock, writeConfig } from "./wc-harness.mjs";

function makeCtx() {
  const notices = [];
  let reloads = 0;
  return {
    notices,
    get reloads() {
      return reloads;
    },
    cwd: "/tmp/weechat-toggle",
    model: { provider: "prov", id: "model-t" },
    isIdle: () => true,
    hasUI: true,
    mode: "tui",
    abort() {},
    ui: {
      notify: (message, type = "info") => notices.push({ message, type }),
      select: async () => undefined,
      input: async () => undefined,
    },
    reload: async () => {
      reloads++;
    },
  };
}

function readConfig(dir) {
  return JSON.parse(fs.readFileSync(path.join(dir, "pi-weechat.json"), "utf8"));
}

async function load(ctx, tools = ["read", "bash"]) {
  const mock = makePiMock(ctx, tools);
  await loadExtension(mock);
  return mock;
}

test("default: /weechat and /weechat-ctl are registered alongside the tools", async (t) => {
  const env = makeBridgeEnv("toggle-on");
  t.after(() => {
    env.restoreEnv();
    env.cleanup();
  });
  env.applyEnv();
  const ctx = makeCtx();
  const mock = await load(ctx);
  assert.ok(mock.registeredCommands["weechat"], "/weechat is registered");
  assert.ok(mock.registeredCommands["weechat-ctl"], "the bridge commands are registered");
  assert.ok(mock.handlers["session_start"], "the bridge hooks are registered");
});

test("enabled=false registers ONLY /weechat", async (t) => {
  const env = makeBridgeEnv("toggle-off");
  t.after(() => {
    env.restoreEnv();
    env.cleanup();
  });
  env.applyEnv();
  writeConfig(env.dir, { enabled: false, url: "tcp://box:1" });
  const ctx = makeCtx();
  const mock = await load(ctx);
  assert.ok(mock.registeredCommands["weechat"], "/weechat still works when disabled");
  assert.equal(mock.registeredCommands["weechat-ctl"], undefined, "no bridge commands");
  assert.deepEqual(Object.keys(mock.registeredTools), [], "no tools");
  assert.deepEqual(Object.keys(mock.handlers), [], "no event hooks");
  assert.equal(fs.existsSync(env.sockPath), false, "no socket is created");
  assert.deepEqual(await mock.fire("session_start"), [], "nothing happens on session_start");

  await mock.registeredCommands["weechat"].handler("status", ctx);
  assert.equal(ctx.notices.length, 1);
  assert.match(ctx.notices[0].message, /disabled/);
  assert.match(ctx.notices[0].message, /pi-weechat\.json/);
});

test("PI_WEECHAT_ENABLED=off wins over the config file", async (t) => {
  const env = makeBridgeEnv("toggle-env");
  t.after(() => {
    env.restoreEnv();
    env.cleanup();
  });
  env.applyEnv();
  writeConfig(env.dir, { enabled: true });
  process.env.PI_WEECHAT_ENABLED = "off";
  const ctx = makeCtx();
  const mock = await load(ctx);
  assert.equal(mock.registeredCommands["weechat-ctl"], undefined, "env disables the bridge");
  assert.ok(mock.registeredCommands["weechat"], "/weechat is still available");
});

test("/weechat off writes the switch and reloads the runtime", async (t) => {
  const env = makeBridgeEnv("toggle-off-cmd");
  t.after(() => {
    env.restoreEnv();
    env.cleanup();
  });
  env.applyEnv();
  writeConfig(env.dir, { url: "tcp://box:52311", token: "s3cret" });
  const ctx = makeCtx();
  const mock = await load(ctx);
  await mock.registeredCommands["weechat"].handler("off", ctx);
  assert.equal(ctx.reloads, 1, "the extension runtime is reloaded");
  assert.deepEqual(readConfig(env.dir), { url: "tcp://box:52311", token: "s3cret", enabled: false });
  assert.match(ctx.notices.at(-1).message, /disabled/);
});

test("/weechat on from a disabled runtime re-enables and reloads", async (t) => {
  const env = makeBridgeEnv("toggle-on-cmd");
  t.after(() => {
    env.restoreEnv();
    env.cleanup();
  });
  env.applyEnv();
  writeConfig(env.dir, { enabled: "off", pick: "off" });
  const ctx = makeCtx();
  const mock = await load(ctx);
  assert.equal(mock.registeredCommands["weechat-ctl"], undefined);
  await mock.registeredCommands["weechat"].handler("on", ctx);
  assert.equal(ctx.reloads, 1);
  assert.deepEqual(readConfig(env.dir), { enabled: true, pick: "off" }, "other keys survive");
});

test("/weechat is a no-op when already in the requested state", async (t) => {
  const env = makeBridgeEnv("toggle-noop");
  t.after(() => {
    env.restoreEnv();
    env.cleanup();
  });
  env.applyEnv();
  const ctx = makeCtx();
  const mock = await load(ctx);
  await mock.registeredCommands["weechat"].handler("on", ctx);
  assert.equal(ctx.reloads, 0, "no reload for a state that already holds");
  assert.match(ctx.notices[0].message, /already on/);
  assert.equal(fs.existsSync(path.join(env.dir, "pi-weechat.json")), false, "config untouched");
});

test("env override blocks a contradicting /weechat, and bad args show usage", async (t) => {
  const env = makeBridgeEnv("toggle-env-override");
  t.after(() => {
    env.restoreEnv();
    env.cleanup();
  });
  env.applyEnv();
  process.env.PI_WEECHAT_ENABLED = "off";
  const ctx = makeCtx();
  const mock = await load(ctx);
  await mock.registeredCommands["weechat"].handler("on", ctx);
  assert.equal(ctx.reloads, 0);
  assert.match(ctx.notices[0].message, /PI_WEECHAT_ENABLED=off overrides/);
  await mock.registeredCommands["weechat"].handler("wat", ctx);
  assert.match(ctx.notices[1].message, /usage: \/weechat on\|off\|status/);
});

test("a malformed config file is refused, not rewritten", async (t) => {
  const env = makeBridgeEnv("toggle-broken");
  t.after(() => {
    env.restoreEnv();
    env.cleanup();
  });
  env.applyEnv();
  const broken = "{ not json";
  fs.writeFileSync(path.join(env.dir, "pi-weechat.json"), broken);
  const ctx = makeCtx();
  const mock = await load(ctx);
  await mock.registeredCommands["weechat"].handler("off", ctx);
  assert.equal(ctx.reloads, 0, "no reload after a refused write");
  assert.equal(fs.readFileSync(path.join(env.dir, "pi-weechat.json"), "utf8"), broken);
  assert.match(ctx.notices.at(-1).message, /refusing to rewrite/);
});
