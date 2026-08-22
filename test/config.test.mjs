// Unit tests for the pi-side config file loader (lib/pi-config.mjs).
// Run: node --experimental-strip-types --test test/   (or `npm test`)
import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { loadConfig, configPath, CONFIG_FILENAME } from "../lib/pi-config.mjs";

// The loader resolves <agent dir>/pi-weechat.json, with the agent dir taken
// from $PI_CODING_AGENT_DIR (pi's own override; default ~/.pi/agent). Point
// it at a temp dir so the tests never touch the real config.
function withAgentDir(t, write) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-cfg-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  process.env.PI_CODING_AGENT_DIR = dir;
  if (write !== undefined) {
    fs.writeFileSync(path.join(dir, CONFIG_FILENAME), write);
  }
  return path.join(dir, CONFIG_FILENAME);
}

test("configPath follows PI_CODING_AGENT_DIR, default ~/.pi/agent", () => {
  const prev = process.env.PI_CODING_AGENT_DIR;
  try {
    process.env.PI_CODING_AGENT_DIR = "/tmp/some-agent-dir";
    assert.equal(configPath(), path.join("/tmp/some-agent-dir", "pi-weechat.json"));
    delete process.env.PI_CODING_AGENT_DIR;
    const home = process.env.HOME || os.homedir();
    assert.equal(configPath(), path.join(home, ".pi", "agent", "pi-weechat.json"));
  } finally {
    if (prev === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prev;
  }
});

test("loadConfig: missing file → {}, no error", () => {
  withAgentDir(test, undefined); // temp dir, no config file
  const errors = [];
  assert.deepEqual(loadConfig({ onError: (m) => errors.push(m) }), {});
  assert.equal(errors.length, 0);
});

test("loadConfig: parses known keys, ignores unknown ones", () => {
  withAgentDir(test, JSON.stringify({
    url: "tcp://box:52311",
    token: "secret-abc",
    debugLog: "/tmp/bridge.debug",
    futureOption: true, // forward-compat: ignored, not an error
  }));
  const errors = [];
  assert.deepEqual(loadConfig({ onError: (m) => errors.push(m) }), {
    url: "tcp://box:52311",
    token: "secret-abc",
    debugLog: "/tmp/bridge.debug",
  });
  assert.equal(errors.length, 0);
});

test("loadConfig: empty-string values are treated as unset", () => {
  withAgentDir(test, JSON.stringify({ url: "", token: "keep-me" }));
  const errors = [];
  assert.deepEqual(loadConfig({ onError: (m) => errors.push(m) }), { token: "keep-me" });
  assert.equal(errors.length, 0);
});

test("loadConfig: invalid JSON → onError + {}", () => {
  withAgentDir(test, "{ not json");
  const errors = [];
  assert.deepEqual(loadConfig({ onError: (m) => errors.push(m) }), {});
  assert.equal(errors.length, 1);
  assert.match(errors[0], /invalid JSON/);
});

test("loadConfig: non-object root → onError + {}", () => {
  withAgentDir(test, JSON.stringify(["url"]));
  const errors = [];
  assert.deepEqual(loadConfig({ onError: (m) => errors.push(m) }), {});
  assert.equal(errors.length, 1);
  assert.match(errors[0], /top level must be a JSON object/);
});

test("loadConfig: wrong-typed value → that key dropped + onError, valid keys kept", () => {
  withAgentDir(test, JSON.stringify({ url: "tcp://box:52311", token: 42 }));
  const errors = [];
  assert.deepEqual(loadConfig({ onError: (m) => errors.push(m) }), { url: "tcp://box:52311" });
  assert.equal(errors.length, 1);
  assert.match(errors[0], /"token" must be a string/);
});

test("loadConfig: a directory in place of the file → {}, no error (EISDIR = missing)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-wc-cfg-dir-"));
  test.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  process.env.PI_CODING_AGENT_DIR = dir;
  fs.mkdirSync(path.join(dir, CONFIG_FILENAME)); // DIRECTORY named pi-weechat.json
  const errors = [];
  assert.deepEqual(loadConfig({ onError: (m) => errors.push(m) }), {});
  assert.equal(errors.length, 0);
});
