/**
 * cd.ts — pi extension: /cd <path> — switch pi to another project directory.
 *
 * TUI counterpart of the bridge's !cd command (same behavior, same shared
 * helpers in lib/cd-search.mjs): an exact existing directory switches
 * immediately to a new session in that cwd; otherwise similar directories
 * are fuzzy-searched and picked in a local select dialog, always including a
 * "create as new project" option.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as fs from "node:fs";
import * as path from "node:path";
import {
  CD_CREATE_PREFIX,
  findSimilarDirs,
  isExistingDirectory,
  resolveCdTarget,
  writeCdSessionHeader,
} from "../lib/cd-search.mjs";

export default function (pi: ExtensionAPI) {
  pi.registerCommand("cd", {
    description: "Switch pi to another project directory (new session there): /cd <path> — e.g. /cd ~/my-project",
    handler: async (args, ctx) => {
      await ctx.waitForIdle();
      const target = resolveCdTarget(args, ctx.cwd);
      if (!target) {
        ctx.ui.notify("usage: /cd <path> — e.g. /cd ~/my-project", "warning");
        return;
      }

      if (isExistingDirectory(target)) {
        await switchSessionToDir(target, ctx);
        return;
      }

      // Target exists but is not a directory: cannot create over it.
      let targetBlocked = false;
      try {
        targetBlocked = !fs.statSync(target).isDirectory();
      } catch {
        /* does not exist — fine */
      }

      const matches = findSimilarDirs(target);
      const createOption = `${CD_CREATE_PREFIX}${target} as new project`;

      let title: string;
      let options: string[];
      if (matches.length > 0) {
        title = `No exact match for ${target}. Which directory?`;
        options = targetBlocked ? matches : [...matches, createOption];
      } else {
        title = `Nothing similar to "${path.basename(target)}" found.`;
        options = targetBlocked ? [] : [createOption];
      }

      if (options.length === 0) {
        ctx.ui.notify(`${target} exists but is not a directory.`, "error");
        return;
      }

      const choice = await ctx.ui.select(title, options);
      if (!choice) {
        ctx.ui.notify("(cd cancelled)", "info");
        return;
      }

      let destination: string;
      if (choice === createOption) {
        try {
          fs.mkdirSync(target, { recursive: true });
        } catch (err) {
          ctx.ui.notify(`could not create ${target}: ${(err as Error).message}`, "error");
          return;
        }
        destination = target;
      } else {
        destination = choice;
      }

      await switchSessionToDir(destination, ctx);
    },
  });

  /**
   * Pre-write a minimal session header (cwd = target dir), then switch to it
   * so the replacement session — and pi's working directory with it — is the
   * target. Without the pre-written header a brand-new session manager would
   * keep the old cwd (it only flushes its file on the first assistant
   * message).
   */
  async function switchSessionToDir(targetCwd: string, ctx: any): Promise<void> {
    const resolvedTarget = path.resolve(targetCwd);
    const sessionFile = writeCdSessionHeader(resolvedTarget);
    const result = await ctx.switchSession(sessionFile);
    if (result?.cancelled) {
      ctx.ui.notify("(cd cancelled)", "info");
    } else {
      ctx.ui.notify(`switched to ${resolvedTarget}`, "info");
    }
  }
}
