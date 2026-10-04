// Glowing Bear compatibility check: the attribute/color codes the pi_bridge
// markdown renderer emits must decode the way GB's own rich-text parser
// expects — bold/italic/underline classes, and the bridge's "re-apply the
// outer style after a nested span" rule (a \x19 color code resets GB's
// attributes, so the bridge re-emits them after every inner span).
//
// GB source is optional: the test skips when ~/src/glowing-bear (or
// $GLOWING_BEAR_DIR) is not checked out.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
function loadGbDecoder() {
  const dir = process.env.GLOWING_BEAR_DIR || path.join(os.homedir(), "src", "glowing-bear");
  const file = path.join(dir, "src-svelte", "src", "lib", "weechat.ts");
  if (!fs.existsSync(file)) return null;
  // Node's strip-types (see the npm test script) imports the TS directly;
  // the file's only import is type-only and gets erased.
  return import(file);
}

test("glowing bear decodes the bridge's markdown styles", async (t) => {
  const mod = await loadGbDecoder();
  if (!mod) {
    t.skip("glowing-bear source not found (set GLOWING_BEAR_DIR to enable)");
    return;
  }
  const { rawText2Rich } = mod;

  // color first, then the attribute: exactly the order the bridge emits
  let parts = rawText2Rich("\x19F11\x1a\x01BOLD\x1c");
  assert.equal(parts.length, 1);
  assert.equal(parts[0].text, "BOLD");
  assert.equal(parts[0].fgColor.name, "magenta");
  assert.equal(parts[0].attrs.override.b, true, "bold class (.a-b)");

  parts = rawText2Rich("\x1a\x03ITALIC\x1c");
  assert.equal(parts[0].attrs.override.i, true, "italic class (.a-i)");

  parts = rawText2Rich("\x1a\x04UNDER\x1c");
  assert.equal(parts[0].attrs.override.u, true, "underline class (.a-u)");

  // a nested color span resets attributes; the bridge's restore re-applies
  // them, and GB must keep the outer style on the tail
  parts = rawText2Rich("\x1a\x01outer \x19F08inner \x1a\x01tail\x1c");
  assert.equal(parts.length, 3);
  assert.equal(parts[0].attrs.override.b, true, "bold before the code span");
  assert.equal(parts[1].fgColor.name, "yellow", "inner span color");
  assert.equal(parts[1].attrs.override.b, false, "color span drops the outer bold");
  assert.equal(parts[2].attrs.override.b, true, "the restored bold survives to the tail");
});
