import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  CD_CREATE_PREFIX,
  cdScoreName,
  expandTilde,
  findSimilarDirs,
  isExistingDirectory,
  levenshtein,
  resolveCdTarget,
  writeCdSessionHeader,
} from "../lib/cd-search.mjs";

test("expandTilde", () => {
  const home = os.homedir();
  assert.equal(expandTilde("~"), home);
  assert.equal(expandTilde("~/proj"), path.join(home, "proj"));
  assert.equal(expandTilde("/abs/path"), "/abs/path");
  assert.equal(expandTilde("rel/path"), "rel/path");
});

test("resolveCdTarget", () => {
  assert.equal(resolveCdTarget("", "/home/u"), null);
  assert.equal(resolveCdTarget("   ", "/home/u"), null);
  assert.equal(resolveCdTarget(null, "/home/u"), null);
  assert.equal(resolveCdTarget("~/proj", "/home/u"), path.join(os.homedir(), "proj"));
  assert.equal(resolveCdTarget("/abs/proj", "/home/u"), path.resolve("/abs/proj"));
  assert.equal(resolveCdTarget("proj", "/home/u"), path.resolve("/home/u/proj"));
});

test("levenshtein basics", () => {
  assert.equal(levenshtein("kitten", "sitting"), 3);
  assert.equal(levenshtein("abc", "abc"), 0);
  assert.equal(levenshtein("", "abc"), 3);
  assert.equal(levenshtein("abc", ""), 3);
});

test("cdScoreName", () => {
  assert.equal(cdScoreName("proj-alpha", "proj-alpha"), 100);
  assert.equal(cdScoreName("Proj-Alpha", "proj-alpha"), 100); // case-insensitive
  // substring coverage: "alph" is fully inside "proj-alpha"
  assert.ok(cdScoreName("alph", "proj-alpha") >= 60);
  // unrelated names score below the accept threshold
  assert.ok(cdScoreName("completely-different", "proj-alpha") < 55);
});

test("isExistingDirectory", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cd-search-"));
  const file = path.join(dir, "file.txt");
  fs.writeFileSync(file, "x");
  assert.ok(isExistingDirectory(dir));
  assert.ok(!isExistingDirectory(file));
  assert.ok(!isExistingDirectory(path.join(dir, "nope")));
  fs.rmSync(dir, { recursive: true, force: true });
});

test("findSimilarDirs: siblings match, pruned/hidden dirs skipped", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cd-search-fs-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  fs.mkdirSync(path.join(dir, "proj-alpha"));
  fs.mkdirSync(path.join(dir, "unrelated"));
  fs.mkdirSync(path.join(dir, ".hidden-proj-alpha"));
  fs.mkdirSync(path.join(dir, "node_modules", "proj-alpha"), { recursive: true });

  const prevHome = process.env.HOME;
  t.after(() => {
    if (prevHome === undefined) delete process.env.HOME;
    else process.env.HOME = prevHome;
  });
  process.env.HOME = dir; // hermetic: the home scan must not see the real home

  const target = path.join(dir, "proj-alph"); // typo of proj-alpha
  const found = findSimilarDirs(target);

  assert.ok(found.includes(path.join(dir, "proj-alpha")), `expected sibling match, got: ${found.join(", ")}`);
  assert.ok(!found.some((p) => p.includes("node_modules")), "pruned dirs must not match");
  assert.ok(!found.some((p) => path.basename(p).startsWith(".")), "hidden dirs must not match for non-hidden targets");
  assert.ok(found.length <= 8, "candidate cap");
});

test("findSimilarDirs: hidden dirs scanned only for hidden targets", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cd-search-hidden-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  fs.mkdirSync(path.join(dir, ".config-proj"));
  fs.mkdirSync(path.join(dir, "config-proj"));

  const prevHome = process.env.HOME;
  t.after(() => {
    if (prevHome === undefined) delete process.env.HOME;
    else process.env.HOME = prevHome;
  });
  process.env.HOME = dir;

  const found = findSimilarDirs(path.join(dir, ".config-pro"));
  // hidden dirs are only scanned for hidden targets (allowHidden);
  // visible siblings are scored on their bare name, so they may match too
  assert.ok(found.includes(path.join(dir, ".config-proj")), `expected hidden sibling, got: ${found.join(", ")}`);

  // and the converse: a visible target never sees hidden dirs
  const foundVisible = findSimilarDirs(path.join(dir, "config-pro"));
  assert.ok(!foundVisible.some((p) => path.basename(p).startsWith(".")), "hidden dirs must not match visible targets");
});

test("writeCdSessionHeader: pre-writes header with target cwd", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cd-search-hdr-"));
  const target = path.join(dir, "my-project");
  fs.mkdirSync(target);
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));

  const prevAgentDir = process.env.PI_CODING_AGENT_DIR;
  t.after(() => {
    if (prevAgentDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevAgentDir;
  });
  process.env.PI_CODING_AGENT_DIR = dir;

  const file = writeCdSessionHeader(target);
  assert.ok(fs.existsSync(file), "session file exists on disk");
  assert.ok(file.startsWith(dir), "stored under PI_CODING_AGENT_DIR");
  assert.match(path.basename(file), /^\d{4}-\d{2}-\d{2}T[\d-]+Z_[0-9a-f-]{36}\.jsonl$/);

  const lines = fs.readFileSync(file, "utf8").trim().split("\n");
  assert.equal(lines.length, 1);
  const header = JSON.parse(lines[0]);
  assert.equal(header.type, "session");
  assert.equal(header.cwd, target);
  assert.ok(header.version >= 1);
  assert.ok(header.id);
});

test("CD_CREATE_PREFIX is stable (bridge and TUI must show the same option)", () => {
  assert.equal(CD_CREATE_PREFIX, "➕ create ");
});
