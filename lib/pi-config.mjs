// Pi-side config file for the weechat bridge: <agent dir>/pi-weechat.json.
// The agent dir follows pi's own convention ($PI_CODING_AGENT_DIR, default
// ~/.pi/agent) so the file sits next to settings.json. Environment variables
// always take precedence over values in this file (see resolve* in the
// extension).

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export const CONFIG_FILENAME = "pi-weechat.json";

/** Absolute path of the pi-side config file. */
export function configPath() {
  const dir = process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
  return path.join(dir, CONFIG_FILENAME);
}

/**
 * Read and parse the config file. Returns a plain object with at most the
 * known keys: `url`, `token`, `debugLog` (strings), `pick` and `askTool`
 * (string or boolean), `pickTools` (string or array of strings). Unknown
 * keys are ignored (forward-compat), absent or empty-string values mean
 * "unset". A missing file is NOT an error → {}.
 *
 * Problems (unreadable file, invalid JSON, non-object root, wrong-typed
 * value) never throw: they call onError(message) and return {} so a broken
 * config degrades to env/default behavior instead of breaking the bridge.
 */
export function loadConfig({ onError = () => {} } = {}) {
  const p = configPath();
  let raw;
  try {
    raw = fs.readFileSync(p, "utf8");
  } catch (err) {
    if (err && (err.code === "ENOENT" || err.code === "EISDIR")) return {};
    onError(`cannot read ${p}: ${String(err?.message ?? err)}`);
    return {};
  }
  let obj;
  try {
    obj = JSON.parse(raw);
  } catch (err) {
    onError(`invalid JSON in ${p}: ${String(err?.message ?? err)}`);
    return {};
  }
  if (typeof obj !== "object" || obj === null || Array.isArray(obj)) {
    onError(`invalid config in ${p}: top level must be a JSON object`);
    return {};
  }
  const out = {};
  for (const key of ["url", "token", "debugLog"]) {
    const v = obj[key];
    if (v === undefined || v === "") continue; // absent or blanked out = unset
    if (typeof v !== "string") {
      onError(`invalid config in ${p}: "${key}" must be a string (got ${JSON.stringify(v)})`);
    } else {
      out[key] = v;
    }
  }
  // Question-routing switches. A bare boolean is the natural JSON spelling for
  // an on/off knob, so both false/true and "off"/"on" are accepted here and
  // resolved by the extension (pickEnabled / resolveAskToolMode).
  for (const key of ["pick", "askTool"]) {
    const v = obj[key];
    if (v === undefined || v === "") continue;
    if (typeof v === "string" || typeof v === "boolean") {
      out[key] = v;
    } else {
      onError(`invalid config in ${p}: "${key}" must be a string or boolean (got ${JSON.stringify(v)})`);
    }
  }
  {
    const v = obj.pickTools;
    if (v !== undefined && v !== "") {
      if (typeof v === "string") out.pickTools = v;
      else if (Array.isArray(v) && v.every((s) => typeof s === "string")) out.pickTools = v;
      else
        onError(
          `invalid config in ${p}: "pickTools" must be a string or an array of strings (got ${JSON.stringify(v)})`,
        );
    }
  }
  return out;
}
