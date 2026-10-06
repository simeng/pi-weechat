// Shared directory-resolution + fuzzy-search helpers for the cd commands:
// !cd in the WeeChat buffer (extensions/weechat-bridge.ts) and /cd in the pi
// TUI (extensions/cd.ts). Pure functions — no bridge/codec dependencies —
// plus the session-header pre-write that makes ctx.switchSession land in a
// different working directory.
import { CURRENT_SESSION_VERSION, SessionManager } from "@earendil-works/pi-coding-agent";
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export const CD_CREATE_PREFIX = "➕ create ";
export const CD_MIN_SCORE = 55;
export const CD_MAX_CANDIDATES = 8;
export const CD_MAX_SCANNED_ENTRIES = 15_000;
export const CD_PRUNED_DIRS = new Set(["node_modules", ".git", ".cache", ".npm"]);

export function expandTilde(input) {
  if (input === "~") return os.homedir();
  if (input.startsWith("~/")) return path.join(os.homedir(), input.slice(2));
  return input;
}

export function isExistingDirectory(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

/**
 * Resolve a cd argument against the current cwd. Tilde-expanded; relative
 * paths resolve from `cwd`. Returns null for empty input.
 */
export function resolveCdTarget(rawArg, cwd) {
  const input = expandTilde(String(rawArg ?? "").trim());
  if (!input) return null;
  return path.isAbsolute(input) ? path.resolve(input) : path.resolve(cwd, input);
}

export function levenshtein(a, b) {
  if (a === b) return 0;
  if (a.length === 0) return b.length;
  if (b.length === 0) return a.length;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(
        prev[j] + 1, // deletion
        cur[j - 1] + 1, // insertion
        prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1), // substitution
      );
    }
    prev = cur;
  }
  return prev[b.length];
}

/** Case-insensitive similarity between a directory name and the requested one, 0-100. */
export function cdScoreName(name, targetName) {
  const n = name.toLowerCase();
  const t = targetName.toLowerCase();
  if (n === t) return 100;

  let score = Math.round((1 - levenshtein(n, t) / Math.max(n.length, t.length)) * 100);

  if (n.includes(t) || t.includes(n)) {
    const coverage = Math.min(n.length, t.length) / Math.max(n.length, t.length);
    score = Math.max(score, Math.round(60 + 40 * coverage));
  }
  return score;
}

/**
 * Search for directories whose names resemble the requested path's basename.
 * Scans the parent of the target (siblings) plus the home directory two
 * levels deep, with pruning and a hard cap on scanned entries.
 */
export function findSimilarDirs(target) {
  const targetName = path.basename(target);
  const allowHidden = targetName.startsWith(".");
  const parent = path.dirname(target);
  const home = path.resolve(os.homedir());

  const queue = [];
  const pushed = new Set();
  const pushRoot = (dir, maxDepth) => {
    if (pushed.has(dir)) return;
    pushed.add(dir);
    queue.push({ dir, depth: 0, maxDepth });
  };
  // parent and home can be the same directory — scan it once at the deeper depth.
  if (isExistingDirectory(parent) && path.resolve(parent) !== home) pushRoot(path.resolve(parent), 1);
  pushRoot(home, 2);

  const visited = new Set();
  const found = new Map(); // resolved path -> best score
  let scanned = 0;

  while (queue.length > 0 && scanned < CD_MAX_SCANNED_ENTRIES) {
    const { dir, depth, maxDepth } = queue.shift();
    if (visited.has(dir)) continue;
    visited.add(dir);

    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      continue; // unreadable — skip
    }
    scanned += entries.length;

    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const name = entry.name.toLowerCase();
      if (CD_PRUNED_DIRS.has(name)) continue;
      if (name.startsWith(".") && !allowHidden) continue;

      const full = path.resolve(path.join(dir, entry.name));
      if (full === path.resolve(target)) continue;

      const score = cdScoreName(entry.name, targetName);
      if (score >= CD_MIN_SCORE) found.set(full, Math.max(found.get(full) ?? 0, score));

      if (depth + 1 < maxDepth && scanned < CD_MAX_SCANNED_ENTRIES) {
        queue.push({ dir: full, depth: depth + 1, maxDepth });
      }
    }
  }

  return [...found.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, CD_MAX_CANDIDATES)
    .map(([p]) => p);
}

/**
 * Pre-write a minimal session header (cwd = target dir) into the default
 * session directory for that cwd, and return the file path. A brand-new
 * SessionManager does not flush its file until the first assistant message —
 * without the pre-written header pi would stay on the old cwd after
 * ctx.switchSession.
 */
export function writeCdSessionHeader(targetCwd) {
  const resolvedTarget = path.resolve(targetCwd);
  const sessionDir = SessionManager.create(resolvedTarget).getSessionDir();
  const id = crypto.randomUUID();
  const timestamp = new Date().toISOString();
  const fileTimestamp = timestamp.replace(/[:.]/g, "-");
  const sessionFile = path.join(sessionDir, `${fileTimestamp}_${id}.jsonl`);
  fs.writeFileSync(
    sessionFile,
    JSON.stringify({ type: "session", version: CURRENT_SESSION_VERSION, id, timestamp, cwd: resolvedTarget }) + "\n",
  );
  return sessionFile;
}
