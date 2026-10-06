// Loads extensions/cd.ts (the /cd TUI command) with a mock ExtensionAPI and
// drives the handler: usage, exact switch, fuzzy select + create, cancel.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { CD_CREATE_PREFIX } from "../lib/cd-search.mjs";

const mod = await import(new URL("../extensions/cd.ts", import.meta.url).href);

let handler;
const apiCalls = [];
mod.default({
  registerCommand: (name, options) => {
    apiCalls.push(name);
    handler = options.handler;
  },
});

assert.deepEqual(apiCalls, ["cd"], "registers exactly the /cd command");

function makeCtx(cwd) {
  const calls = { switch: [], notify: [] };
  let selectResponder;
  const ctx = {
    cwd,
    waitForIdle: async () => {},
    ui: {
      notify: (msg, type) => calls.notify.push({ msg, type }),
      select: async (title, options) => {
        calls.select = { title, options };
        return selectResponder ? selectResponder(title, options) : undefined;
      },
    },
    switchSession: async (file) => {
      calls.switch.push(file);
      return { cancelled: false };
    },
    calls,
    setSelect: (fn) => (selectResponder = fn),
  };
  return ctx;
}

test("handler: empty arg answers with usage", async () => {
  const ctx = makeCtx(process.cwd());
  await handler("", ctx);
  assert.equal(ctx.calls.switch.length, 0);
  assert.ok(
    ctx.calls.notify.some((n) => n.type === "warning" && n.msg.includes("usage: /cd")),
    `expected usage warning, got: ${JSON.stringify(ctx.calls.notify)}`
  );
});

test("handler: exact existing dir switches immediately (no select)", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cd-ext-"));
  const proj = path.join(dir, "proj-exact");
  fs.mkdirSync(proj);
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  const prevAgentDir = process.env.PI_CODING_AGENT_DIR;
  t.after(() => {
    if (prevAgentDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevAgentDir;
  });
  process.env.PI_CODING_AGENT_DIR = dir; // headers land in the sandbox

  const ctx = makeCtx(dir);
  await handler("proj-exact", ctx);

  assert.equal(ctx.calls.select, undefined, "no select dialog for an exact dir");
  assert.equal(ctx.calls.switch.length, 1, "one session switch");
  const header = JSON.parse(fs.readFileSync(ctx.calls.switch[0], "utf8"));
  assert.equal(header.cwd, proj);
  assert.ok(ctx.calls.notify.some((n) => n.msg.includes(`switched to ${proj}`)));
});

test("handler: typo fuzzy-selects 'create as new project'", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cd-ext-create-"));
  fs.mkdirSync(path.join(dir, "existing-proj"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  const prevAgentDir = process.env.PI_CODING_AGENT_DIR;
  t.after(() => {
    if (prevAgentDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevAgentDir;
  });
  process.env.PI_CODING_AGENT_DIR = dir;

  const target = path.join(dir, "existing-projj"); // typo
  const ctx = makeCtx(dir);
  ctx.setSelect((title, options) => {
    const create = `${CD_CREATE_PREFIX}${target} as new project`;
    assert.ok(options.includes(create), `create option offered: ${options.join(" | ")}`);
    assert.ok(options.includes(path.join(dir, "existing-proj")), `fuzzy match offered: ${options.join(" | ")}`);
    return create;
  });
  await handler("existing-projj", ctx);

  assert.ok(fs.statSync(target).isDirectory(), "target dir was created");
  assert.equal(ctx.calls.switch.length, 1);
  const header = JSON.parse(fs.readFileSync(ctx.calls.switch[0], "utf8"));
  assert.equal(header.cwd, target);
});

test("handler: cancelled select does not switch", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cd-ext-cancel-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  const ctx = makeCtx(dir);
  // no selectResponder → select resolves undefined (cancel)
  await handler("no-such-dir-xyz", ctx);

  assert.equal(ctx.calls.switch.length, 0);
  assert.ok(ctx.calls.notify.some((n) => n.msg === "(cd cancelled)"));
});
