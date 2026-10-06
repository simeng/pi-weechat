/**
 * weechat-bridge.ts — pi extension: mirror this session into a WeeChat buffer.
 *
 * Dials (as client) the WeeChat script (weechat/pi_bridge.py) over a Unix
 * socket or TCP, mirrors assistant text (batched into whole lines), tool
 * calls/results, and status; forwards lines typed in the WeeChat buffer back
 * to pi as user input. Decision questions raised by ask_user-style tools are
 * routed to the WeeChat buffer and answered with !pick (falling back to the pi
 * terminal when the bridge is down); if no other extension provides an
 * ask_user tool, a minimal built-in one is registered. Endpoint/token/debug
 * are read from environment
 * variables (PI_WEECHAT_URL / PI_WEECHAT_TOKEN / PI_BRIDGE_DEBUG) or from the
 * config file <agent dir>/pi-weechat.json (default ~/.pi/agent/) — env vars
 * win when both set. Wire format: NDJSON, protocol 2 (challenge-response auth
 * when a token is configured — the token is never sent). See PLAN.md §2.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { performance } from "node:perf_hooks";
import { Type } from "@sinclair/typebox";
// @ts-ignore - plain ESM module, no types needed
import { LineDecoder, PROTOCOL_VERSION, parseEndpoint } from "../lib/codec.mjs";
// @ts-ignore - plain ESM module, no types needed
import { loadConfig } from "../lib/pi-config.mjs";
import {
  CD_CREATE_PREFIX,
  expandTilde,
  findSimilarDirs,
  isExistingDirectory,
  writeCdSessionHeader,
} from "../lib/cd-search.mjs";

const MAX_TOOL_OUTPUT = 8192;
const DEBUG_LOG_MAX_BYTES = 1_000_000; // rotate above this

// Same freeform sentinel pi-ask-user offers in its overlay, so !pick can pick
// "type custom response" by number and follow up with an input prompt.
const FREEFORM_SENTINEL = "\u270f\ufe0f Type custom response...";

/** "off"/"0"/"false" disable ask interception; anything else keeps it on. */
function pickEnabled(env: string | undefined, cfg?: string | boolean): boolean {
  const v = env ?? (cfg === undefined ? undefined : String(cfg).trim().toLowerCase());
  return v !== "off" && v !== "0" && v !== "false";
}

/** Tool names whose question prompts are routed to the buffer (default: ask_user). */
function pickToolNames(env: string | undefined, cfg?: string | string[]): Set<string> {
  const raw =
    env ?? (Array.isArray(cfg) ? cfg.join(",") : typeof cfg === "string" ? cfg : undefined);
  const names = String(raw ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return new Set(names.length > 0 ? names : ["ask_user"]);
}

/** Coerce the `options` param of an ask-style tool into {label, description?} entries. */
function coerceAskOptions(raw: unknown): Array<{ label: string; description?: string }> {
  if (!Array.isArray(raw)) return [];
  const out: Array<{ label: string; description?: string }> = [];
  for (const item of raw) {
    if (typeof item === "string" && item.trim()) {
      out.push({ label: item.trim() });
    } else if (
      item &&
      typeof item === "object" &&
      typeof (item as { title?: unknown }).title === "string" &&
      ((item as { title: string }).title).trim()
    ) {
      const t = item as { title: string; description?: unknown };
      const description =
        typeof t.description === "string" && t.description.trim() ? t.description.trim() : undefined;
      out.push({ label: t.title.trim(), description });
    }
  }
  return out;
}

/** Question + optional (truncated) context, as the buffer prompt title. */
function buildAskTitle(question: string, context: unknown): string {
  const ctxText = typeof context === "string" ? context.trim() : "";
  if (!ctxText) return question;
  return `${question}\n\nContext:\n${truncate(ctxText, 800)}`;
}

/** Quote each choice so the LLM sees unambiguous tool-result text. */
function formatPickSummary(value: string | string[]): string {
  const vals = Array.isArray(value) ? value : [value];
  return vals.map((v) => JSON.stringify(v)).join(", ");
}

// Values from the pi-side config file (lib/pi-config.mjs). Env vars always
// win over these; see resolve*() below.
type BridgeConfig = {
  url?: string;
  token?: string;
  debugLog?: string;
  /** "off" disables routing decision questions to the buffer (default: on). */
  pick?: string | boolean;
  /** Comma list / array of question tool names to route (default: ask_user). */
  pickTools?: string | string[];
};

// Opt-in wire debug log. Enabled by PI_BRIDGE_DEBUG=<path>, then the
// config-file "debugLog" key, then the marker file
// $XDG_RUNTIME_DIR/pi-weechat.debug (the WeeChat script honors the same
// marker, so one file enables both sides).
function resolveDebugLogPath(cfg: BridgeConfig = {}): string | null {
  const env = process.env.PI_BRIDGE_DEBUG;
  if (env) return env;
  if (cfg.debugLog) return cfg.debugLog;
  const xdg = process.env.XDG_RUNTIME_DIR;
  if (!xdg) return null;
  const marker = path.join(xdg, "pi-weechat.debug");
  try {
    fs.accessSync(marker);
    return marker;
  } catch {
    return null;
  }
}

let debugLogPath: string | null = null;

function dbg(msg: string): void {
  const p = debugLogPath;
  if (!p) return;
  try {
    let st: fs.Stats | null = null;
    try {
      st = fs.statSync(p);
    } catch {
      /* file may not exist yet */
    }
    if (st && st.size > DEBUG_LOG_MAX_BYTES) {
      const data = fs.readFileSync(p, "utf8");
      fs.writeFileSync(p, "\n… (log rotated) …\n" + data.slice(-400_000));
    }
    fs.appendFileSync(p, `[${new Date().toISOString()}] ${msg}\n`);
  } catch {
    /* debug must never break the bridge */
  }
}

function reinitDebugLog(cfg: BridgeConfig = {}): void {
  const p = resolveDebugLogPath(cfg);
  if (p !== debugLogPath) {
    dbg(`debug log ${p ? "ENABLED" : "disabled"}: ${p ?? "(no marker)"}`);
    debugLogPath = p;
  }
}

const PING_INTERVAL_MS = 30_000;
const PONG_TIMEOUT_MS = 90_000;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const DIAL_TIMEOUT_MS = 10_000; // TCP: filtered/blackholed ports never ECONNREFUSE
const CHALLENGE_WAIT_MS = 3_000; // wait for the server's challenge when a token is set

type Endpoint =
  | { kind: "tcp"; host: string; port: number }
  | { kind: "unix"; path: string };

function describeEndpoint(ep: Endpoint): string {
  return ep.kind === "tcp"
    ? `tcp://${ep.host}:${ep.port}`
    : `unix:${ep.path}`;
}

let sockDeprecationNoted = false;

/**
 * Resolve where to dial, first match wins: $PI_WEECHAT_URL, then the config
 * file's `url`, then the deprecated $PI_WEECHAT_SOCK, then the default unix
 * path. (tcp://host:port, unix://<path>, schemeless host:port → tcp,
 * anything else → socket path.)
 */
function resolveEndpoint(cfg: BridgeConfig = {}, onProblem?: (msg: string) => void): Endpoint {
  const url = process.env.PI_WEECHAT_URL;
  if (url) {
    try {
      return parseEndpoint(url) as Endpoint;
    } catch (err) {
      dbg(`PI_WEECHAT_URL is invalid (${String(err)}); falling back to the unix socket path`);
    }
  }
  if (cfg.url) {
    try {
      return parseEndpoint(cfg.url) as Endpoint;
    } catch (err) {
      const msg = `pi-weechat.json "url" is invalid (${String(err)}); falling back to the unix socket path`;
      dbg(msg);
      onProblem?.(msg);
    }
  }
  if (process.env.PI_WEECHAT_SOCK) {
    if (!sockDeprecationNoted) {
      sockDeprecationNoted = true;
      dbg("note: PI_WEECHAT_SOCK is deprecated — use PI_WEECHAT_URL (e.g. unix://<path>)");
    }
    return { kind: "unix", path: process.env.PI_WEECHAT_SOCK };
  }
  const xdg = process.env.XDG_RUNTIME_DIR;
  if (xdg) return { kind: "unix", path: path.join(xdg, "pi-weechat.sock") };
  return { kind: "unix", path: path.join(os.homedir(), ".local", "state", "pi-weechat", "pi-weechat.sock") };
}

/** Shared-secret proof for the protocol-2 challenge (the token itself is never sent). */
function makeProof(token: string, nonce: string): string {
  return crypto.createHmac("sha256", token).update(nonce).digest("hex");
}

export default function weechatBridge(pi: ExtensionAPI) {
  let endpoint: Endpoint;
  let token = "";
  let sock: net.Socket | null = null;
  let decoder: LineDecoder | null = null;
  let shutdown = false; // session_shutdown was emitted; stop reconnecting
  let attempt = 0;
  let connectTimer: NodeJS.Timeout | null = null;
  let pingTimer: NodeJS.Timeout | null = null;
  let lastPongAt = 0;
  let ctxRef: any = null; // latest ExtensionContext (for ctx.abort())
  let busy = false;           // agent_start seen without agent_settled
  let currentState = "idle";
  let bridgeReady = false;
  let runActive = false;
  let runHasStarted = false;
  let runElapsedMs = 0;
  let runStartedAt: number | null = null;
  let turnActive = false;
  let turnElapsedMs = 0;
  let turnStartedAt: number | null = null;
  let turnNumber: number | null = null;
  let turnCount = 0;
  const timingPauseReasons = new Set<string>();
  let askPickEnabled = true;              // PI_WEECHAT_PICK=off disables question routing
  let askToolNames = new Set(["ask_user"]); // PI_WEECHAT_PICK_TOOLS (comma list)
  let fallbackAskRegistered = false;      // we registered the built-in ask_user
  let pendingOut: string[] = []; // messages emitted before the socket is up
  let helloSent = false;         // our hello went out for the current connection
  let challengeTimer: NodeJS.Timeout | null = null;

  // Protocol-3 UI prompt channel: select/input prompts asked of the WeeChat
  // buffer (answered with !pick). Keyed by the ui_request id.
  type UIAskOutcome =
    | { kind: "value"; value: string | string[] } // user answered
    | { kind: "cancelled" } // explicit `!pick cancel`
    | { kind: "fallback" }; // timeout/disconnect/malformed/abort — caller falls back to local UI
  let uiSeq = 0;
  const pendingUIs = new Map<number, (o: UIAskOutcome) => void>();

  // Streaming assembly: assistant text + thinking blocks, keyed by contentIndex.
  let blockBufs = new Map<number, string>();
  let thinkBufs = new Map<number, string>();
  let msgSeq = 0;
  let currentMsgId = 0;

  // -------------------------------------------------------------- config

  /**
   * (Re)read the config file + env vars. Called at extension load and at
   * every session_start, so edits to pi-weechat.json or the environment are
   * picked up by /reload without restarting pi. A broken file never breaks
   * the bridge: it degrades to env/default values, is noted in the debug
   * log, and surfaces as a red `config_error` line in the buffer.
   */
  function refreshConfig(): void {
    const cfg = loadConfig({
      onError: (m: string) => {
        dbg(m);
        send({ type: "error", code: "config_error", message: m });
      },
    }) as BridgeConfig;
    reinitDebugLog(cfg); // picks up a marker file / debugLog seen after load
    endpoint = resolveEndpoint(cfg, (m) => send({ type: "error", code: "config_error", message: m }));
    token = process.env.PI_WEECHAT_TOKEN ?? (cfg.token ?? "");
    askPickEnabled = pickEnabled(process.env.PI_WEECHAT_PICK, cfg.pick);
    askToolNames = pickToolNames(process.env.PI_WEECHAT_PICK_TOOLS, cfg.pickTools);
  }
  refreshConfig(); // at load time (session_start refreshes again)

  // ------------------------------------------------------------------ send

  function send(obj: Record<string, unknown>): void {
    const line = JSON.stringify(obj) + "\n";
    dbg(">> " + line.trim().slice(0, 400));
    if (sock && !sock.destroyed) {
      sock.write(line);
    } else if (!shutdown) {
      if (pendingOut.length > 10_000) pendingOut.shift(); // bound the queue
      pendingOut.push(line);
    }
  }

  function flushPending(): void {
    for (const line of pendingOut) sock?.write(line);
    pendingOut = [];
  }

  // Pi owns lifecycle timing; WeeChat receives elapsed snapshots and only
  // advances them locally for title redraws. Use a monotonic clock so system
  // clock adjustments cannot make an active run jump backwards or forwards.
  function elapsedMs(total: number, startedAt: number | null, now: number): number {
    return total + (startedAt === null ? 0 : Math.max(0, now - startedAt));
  }

  function sendTimingSnapshot(): void {
    if (!bridgeReady) return;
    const now = performance.now();
    send({
      type: "timing",
      runMs: Math.floor(elapsedMs(runElapsedMs, runStartedAt, now)),
      turnMs: Math.floor(elapsedMs(turnElapsedMs, turnStartedAt, now)),
      turn: turnNumber,
      turns: turnCount,
      runActive,
      turnActive,
      runPaused: timingPauseReasons.size > 0,
      hasRun: runHasStarted,
    });
  }

  function resetTiming(): void {
    runActive = false;
    runHasStarted = false;
    runElapsedMs = 0;
    runStartedAt = null;
    turnActive = false;
    turnElapsedMs = 0;
    turnStartedAt = null;
    turnNumber = null;
    turnCount = 0;
    timingPauseReasons.clear();
  }

  function startAgentRun(): void {
    resetTiming();
    runHasStarted = true;
    runActive = true;
    runStartedAt = performance.now();
  }

  function startModelTurn(index: unknown): void {
    const turnIndex = Math.max(0, intOf(index));
    turnActive = true;
    turnElapsedMs = 0;
    turnStartedAt = timingPauseReasons.size === 0 ? performance.now() : null;
    turnNumber = turnIndex + 1;
    turnCount = Math.max(turnCount, turnNumber);
    sendTimingSnapshot();
  }

  function finishModelTurn(index?: unknown): void {
    if (typeof index === "number" && Number.isFinite(index)) {
      turnNumber = Math.max(0, Math.trunc(index)) + 1;
      turnCount = Math.max(turnCount, turnNumber);
    }
    if (turnStartedAt !== null) {
      turnElapsedMs = elapsedMs(turnElapsedMs, turnStartedAt, performance.now());
    }
    turnStartedAt = null;
    turnActive = false;
    sendTimingSnapshot();
  }

  function settleAgentRun(): void {
    if (turnActive) finishModelTurn();
    if (runStartedAt !== null) {
      runElapsedMs = elapsedMs(runElapsedMs, runStartedAt, performance.now());
    }
    runStartedAt = null;
    runActive = false;
    timingPauseReasons.clear();
    sendTimingSnapshot();
  }

  function pauseTiming(reason: string): void {
    if (timingPauseReasons.has(reason)) return;
    const wasPaused = timingPauseReasons.size > 0;
    timingPauseReasons.add(reason);
    if (!runActive || wasPaused) return;
    const now = performance.now();
    if (runStartedAt !== null) runElapsedMs = elapsedMs(runElapsedMs, runStartedAt, now);
    if (turnStartedAt !== null) turnElapsedMs = elapsedMs(turnElapsedMs, turnStartedAt, now);
    runStartedAt = null;
    turnStartedAt = null;
    sendTimingSnapshot();
  }

  function resumeTiming(reason: string): void {
    if (!timingPauseReasons.delete(reason) || timingPauseReasons.size > 0 || !runActive) return;
    const now = performance.now();
    runStartedAt = now;
    if (turnActive) turnStartedAt = now;
    sendTimingSnapshot();
  }

  /**
   * Send the handshake hello, then the pending queue.
   *
   * Protocol 2: the hello MUST go out first (the server ignores everything
   * before a valid hello). With a configured token the server answers its
   * challenge with proof = HMAC-SHA256(token, nonce) — the token itself is
   * never transmitted. `proof` is only included when non-empty.
   */
  function sendHello(proof?: string): void {
    if (helloSent) return;
    helloSent = true;
    if (challengeTimer) {
      clearTimeout(challengeTimer);
      challengeTimer = null;
    }
    const hello: Record<string, unknown> = {
      type: "hello",
      protocol: PROTOCOL_VERSION,
      name: "pi-weechat-bridge",
    };
    if (proof) hello.proof = proof;
    send(hello);
    flushPending();
  }

  // -------------------------------------------------------------- connect

  function clearConnectTimer(): void {
    if (connectTimer) {
      clearTimeout(connectTimer);
      connectTimer = null;
    }
  }

  function stopPing(): void {
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
  }

  function scheduleReconnect(): void {
    clearConnectTimer();
    if (shutdown || sock?.destroyed === false) return;
    attempt += 1;
    const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** (attempt - 1));
    dbg(`reconnect scheduled: attempt=${attempt} delay=${delay}ms`);
    connectTimer = setTimeout(() => connect(), delay);
    void connectTimer.unref?.();
  }

  function disconnect(): void {
    stopPing();
    if (challengeTimer) {
      clearTimeout(challengeTimer);
      challengeTimer = null;
    }
    helloSent = false;
    bridgeReady = false;
    setWeechatUIConnected(false); // other extensions fall back to local UI
    clearPendingUIs("disconnect");
    if (sock) {
      sock.destroy();
      sock = null;
    }
    pendingOut = [];
    decoder?.reset();
  }

  function connect(): void {
    clearConnectTimer();
    if (shutdown) return;
    if (sock && !sock.destroyed) return;
    // A server-side close takes us through "close" → scheduleReconnect()
    // without disconnect(), so handshake state must be reset here: a
    // stale helloSent=true would skip the hello on the new socket and
    // every reconnect would die on the server's auth timeout.
    helloSent = false;
    if (challengeTimer) {
      clearTimeout(challengeTimer);
      challengeTimer = null;
    }

    const ep = endpoint;
    dbg(`dialing ${describeEndpoint(ep)}`);
    const s =
      ep.kind === "tcp"
        ? net.connect({ host: ep.host, port: ep.port })
        : net.connect(ep.path);
    sock = s;
    decoder = new LineDecoder(onMessage, {
      onError: (e: Error) =>
        send({ type: "error", code: "client_error", message: e.message }),
    });

    if (ep.kind === "tcp") {
      // filtered/blackholed ports never deliver ECONNREFUSED; time the dial
      s.setTimeout(DIAL_TIMEOUT_MS);
      s.once("timeout", () => {
        dbg(`dial timeout after ${DIAL_TIMEOUT_MS}ms`);
        s.destroy(); // → "close" → scheduleReconnect
      });
    }

    s.once("connect", () => {
      s.setTimeout(0); // dial timeout done; the ping/pong loop covers liveness
      dbg(`connected (flushing ${pendingOut.length} pending after hello)`);
      attempt = 0;
      lastPongAt = Date.now();
      if (token) {
        // The server sends a challenge only when IT has a token configured.
        // Wait briefly for it; if none arrives (server has no token) a bare
        // hello is accepted anyway, so the fallback is safe.
        challengeTimer = setTimeout(() => sendHello(undefined), CHALLENGE_WAIT_MS);
        void challengeTimer.unref?.();
      } else {
        sendHello(undefined);
      }
      stopPing();
      pingTimer = setInterval(() => {
        if (Date.now() - lastPongAt > PONG_TIMEOUT_MS) {
          disconnect();
          scheduleReconnect();
          return;
        }
        send({ type: "ping", ts: Date.now() });
      }, PING_INTERVAL_MS);
      void pingTimer.unref?.();
    });
    s.on("data", (chunk) => decoder?.feed(chunk));
    s.once("error", (err) => {
      // ECONNREFUSED etc. — weechat not running yet; retry with backoff.
      dbg(`socket error: ${String((err as Error)?.message ?? err)}`);
      disconnect();
      scheduleReconnect();
    });
    s.once("close", (hadError) => {
      dbg(`socket closed (hadError=${hadError})`);
      bridgeReady = false;
      setWeechatUIConnected(false);
      clearPendingUIs("disconnect");
      if (!shutdown) scheduleReconnect();
    });
  }

  function teardown(): void {
    shutdown = true;
    clearConnectTimer();
    disconnect();
  }

  // -------------------------------------------------------------- receive

  function onMessage(msg: Record<string, any>): void {
    dbg(
      "<< " +
        JSON.stringify(msg).slice(0, 400) +
        (typeof msg.text === "string" && msg.text.length > 200 ? ` …(+${msg.text.length - 200} chars)` : ""),
    );
    switch (msg.type) {
      case "challenge": {
        // Server-side shared-secret challenge (token configured on the
        // WeeChat side). Answer with the HMAC proof; the token itself is
        // never sent, and a captured proof is useless (fresh nonce per
        // connection).
        if (!helloSent) {
          const nonce = typeof msg.nonce === "string" ? msg.nonce : "";
          sendHello(nonce ? makeProof(token, nonce) : undefined);
        }
        return;
      }
      case "hello":
        if (intOf(msg.protocol) !== PROTOCOL_VERSION) {
          send({ type: "error", code: "protocol_mismatch" });
          disconnect();
          scheduleReconnect();
        } else {
          bridgeReady = true;
          setWeechatUIConnected(true);
          // A reconnect gets an authoritative current state and timing
          // snapshot; never infer the offline interval on the WeeChat side.
          setState(currentState);
          sendTimingSnapshot();
        }
        return;
      case "error":
        if (msg.code === "auth_failed") {
          // The server dropped us for a bad proof — usually a token
          // mismatch. Keep retrying with the normal backoff; the buffer
          // side already printed the red error line.
          dbg("AUTH FAILED: token mismatch? check PI_WEECHAT_TOKEN / pi-weechat.json \"token\" / pi_bridge.token — retrying");
        } else {
          dbg("error from weechat: " + JSON.stringify(msg).slice(0, 200));
        }
        return;
      case "pong":
        lastPongAt = Date.now();
        return;
      case "ping":
        send({ type: "pong", ts: msg.ts });
        return;
      case "user_input": {
        const text = typeof msg.text === "string" ? msg.text : "";
        if (!text) return;
        let deliverAs =
          msg.deliverAs === "steer" || msg.deliverAs === "followUp" ? msg.deliverAs : undefined;
        // Plain input while a turn is running: queue it as a follow-up.
        // Without this, pi's prompt() throws ("Agent is already processing")
        // and the message is lost — with no error visible in WeeChat.
        if (!deliverAs && busy) deliverAs = "followUp";
        dbg(
          `sendUserMessage: deliverAs=${deliverAs ?? "(default)"} text=${JSON.stringify(text.slice(0, 200))}`,
        );
        try {
          pi.sendUserMessage(text, deliverAs ? { deliverAs } : undefined);
          dbg("sendUserMessage: ok");
        } catch (err) {
          const msgText = String((err as Error)?.message ?? err);
          dbg(`sendUserMessage THREW: ${msgText}`);
          send({
            type: "error",
            code: "input_failed",
            message: msgText,
          });
        }
        return;
      }
      case "command":
        dbg(`command: ${msg.name} ${String(msg.arg ?? "")}`);
        handleCommand(msg.name as string | undefined, msg.arg as string | undefined);
        return;
      case "ui_response": {
        // Answer to one of our ui_request prompts (!pick in the buffer).
        const id = intOf(msg.id);
        const finish = pendingUIs.get(id);
        if (!finish) {
          dbg(`ui_response for unknown/stale id ${id} — ignored`);
          return;
        }
        let outcome: UIAskOutcome;
        if (msg.cancelled === true) {
          outcome = { kind: "cancelled" };
        } else if (typeof msg.value === "string") {
          outcome = { kind: "value", value: msg.value };
        } else if (Array.isArray(msg.value) && msg.value.every((v) => typeof v === "string")) {
          outcome = { kind: "value", value: msg.value as string[] };
        } else {
          dbg(`ui_response ${id} has a malformed value — falling back to local UI`);
          outcome = { kind: "fallback" };
        }
        finish(outcome);
        // The buffer is no longer waiting on a prompt: clear the "awaiting
        // !pick" title hint (the running state may have changed meanwhile).
        const nextState =
          busy && (currentState === "thinking" || currentState.startsWith("tool:"))
            ? currentState
            : busy
              ? "thinking"
              : "idle";
        setState(nextState);
        return;
      }
      default:
        // unknown type from weechat: ignore (forward-compat)
    }
  }

  function handleCommand(name?: string, arg?: string): void {
    switch (name) {
      case "abort":
        // ctx.abort() is safe from event handlers
        try {
          ctxRef?.abort?.();
        } catch {
          /* no active run */
        }
        return;
      case "new_session":
      case "compact":
      case "status":
      case "model":
      case "cd":
        // Route through the registered extension command.
        // expandPromptTemplates: true is REQUIRED: sendUserMessage defaults
        // it to false, which skips extension-command dispatch and would send
        // the literal text "/weechat-ctl …" to the LLM. With it on, the
        // command executes immediately — even while a turn is streaming.
        pi.sendUserMessage(
          `/weechat-ctl ${name}${arg ? " " + arg : ""}`,
          { expandPromptTemplates: true },
        );
        return;
      default:
        send({ type: "error", code: "unknown_command", message: `!${name ?? "?"}` });
    }
  }

  // --------------------------------------------------------------- status

  function setState(state: string): void {
    currentState = state;
    send({ type: "status", state });
  }

  function sendSessionInfo(ctx: any): void {
    const model = ctx?.model;
    send({
      type: "session_info",
      cwd: ctx?.cwd,
      model: model ? `${model.provider ?? ""}/${model.id ?? "?"}`.replace(/^\//, "") : undefined,
      name: (() => {
        try {
          return pi.getSessionName();
        } catch {
          return undefined;
        }
      })(),
    });
  }

  // --------------------------------------------------------------- events

  pi.on("session_start", async (_event, ctx) => {
    // (re-)read config file + env: both can change across /reload
    refreshConfig();
    shutdown = false;
    attempt = 0;
    busy = false;
    resetTiming();
    ctxRef = ctx;
    blockBufs.clear();
    sendSessionInfo(ctx);
    setState("idle");
    sendTimingSnapshot();
    maybeRegisterFallbackAskTool(ctx);
    connect();
  });

  pi.on("session_shutdown", () => {
    teardown();
  });

  pi.on("agent_start", async (_event, ctx) => {
    ctxRef = ctx;
    busy = true;
    startAgentRun();
    setState("thinking");
    sendTimingSnapshot();
  });

  pi.on("turn_start", (event) => {
    startModelTurn(event.turnIndex);
  });

  pi.on("turn_end", (event) => {
    finishModelTurn(event.turnIndex);
  });

  (pi as any).on("ui_prompt_start", () => {
    pauseTiming("pi-ui");
  });

  (pi as any).on("ui_prompt_end", () => {
    resumeTiming("pi-ui");
  });

  pi.on("agent_settled", async (_event, ctx) => {
    ctxRef = ctx;
    busy = false;
    settleAgentRun();
    setState("idle");
  });

  pi.on("model_select", async (event, ctx) => {
    sendSessionInfo(ctx);
    void event;
  });

  pi.on("session_info_changed", async () => {
    try {
      send({ type: "session_info", name: pi.getSessionName() });
    } catch {
      /* ignore */
    }
  });

  // Mirror prompts typed in the pi terminal itself (both surfaces stay in sync).
  pi.on("input", (event) => {
    if (event.source === "interactive" || event.source === "rpc") {
      send({ type: "user_echo", text: String(event.text ?? "") });
    }
  });

  // Streaming: assemble text blocks from delta events, emit whole lines.
  // Append a delta to one streaming block; emit whole lines as they form.
  function streamLine(
    bufs: Map<number, string>,
    key: number,
    delta: string,
    type: "assistant_line" | "thinking_line",
  ): void {
    const cur = (bufs.get(key) ?? "") + delta;
    bufs.set(key, cur);
    // flush complete lines as they form
    const nl = cur.indexOf("\n");
    if (nl !== -1) {
      const line = cur.slice(0, nl);
      // blank lines are paragraph separators in text but pure noise in thinking
      if (line.trim() || type === "assistant_line") {
        send({ type, msgId: currentMsgId, text: line });
      }
      bufs.set(key, cur.slice(nl + 1));
    }
  }

  function flushTail(
    bufs: Map<number, string>,
    key: number,
    type: "assistant_line" | "thinking_line",
  ): void {
    // remaining partial line of this block is complete now
    const tail = bufs.get(key);
    if (tail && tail.trim()) {
      send({ type, msgId: currentMsgId, text: tail });
    }
    bufs.set(key, "");
  }

  pi.on("message_start", async (event) => {
    const msg = event.message as any;
    if (msg?.role === "assistant") {
      blockBufs.clear();
      thinkBufs.clear();
      currentMsgId = ++msgSeq;
    }
  });

  pi.on("message_update", async (event) => {
    const ev = (event as any).assistantMessageEvent;
    if (!ev || typeof ev !== "object") return;
    const key = ev.contentIndex ?? 0;
    switch (ev.type) {
      case "text_start":
        blockBufs.set(key, "");
        break;
      case "text_delta":
        streamLine(blockBufs, key, String(ev.delta ?? ""), "assistant_line");
        break;
      case "text_end":
        flushTail(blockBufs, key, "assistant_line");
        break;
      // thinking blocks (reasoning models): mirrored as their own line type;
      // the buffer decides whether to render them (!think on|off)
      case "thinking_start":
        thinkBufs.set(key, "");
        break;
      case "thinking_delta":
        streamLine(thinkBufs, key, String(ev.delta ?? ""), "thinking_line");
        break;
      case "thinking_end":
        flushTail(thinkBufs, key, "thinking_line");
        break;
      case "toolcall_start":
        setState("thinking");
        break;
    }
  });

  pi.on("message_end", async (event) => {
    const msg = event.message as any;
    if (msg?.role !== "assistant") return;
    // authoritative final text: re-emit any tail the deltas didn't flush
    for (const [bufs, type] of [
      [blockBufs, "assistant_line"],
      [thinkBufs, "thinking_line"],
    ] as const) {
      for (const key of [...bufs.keys()]) flushTail(bufs, key, type);
      bufs.clear();
    }
    send({ type: "assistant_flush", msgId: currentMsgId });
  });

  pi.on("tool_execution_start", async (event) => {
    const e = event as any;
    setState(`tool:${e.toolName ?? "tool"}`);
    send({
      type: "tool_start",
      toolCallId: e.toolCallId,
      toolName: e.toolName,
      args: e.args,
    });
  });

  pi.on("tool_execution_end", async (event) => {
    const e = event as any;
    const output = extractToolOutput(e.result);
    send({
      type: "tool_end",
      toolCallId: e.toolCallId,
      isError: Boolean(e.isError),
      output: truncate(output, MAX_TOOL_OUTPUT),
    });
    if (!ctxRef?.isIdle?.()) setState("thinking");
  });

  // ----------------------------------------------------------------- tools

  pi.registerCommand("weechat-ctl", {
    description: "Control commands forwarded from the WeeChat bridge buffer (internal)",
    handler: async (rawArgs, ctx) => {
      // args is everything after "/weechat-ctl": first token = subcommand,
      // remainder (if any) = argument (e.g. model id)
      const trimmed = String(rawArgs ?? "").trim();
      const spaceIdx = trimmed.indexOf(" ");
      const name = spaceIdx === -1 ? trimmed : trimmed.slice(0, spaceIdx);
      const arg =
        spaceIdx === -1 ? undefined : trimmed.slice(spaceIdx + 1).trim() || undefined;
      try {
        switch (name) {
          case "new_session": {
            await ctx.newSession({
              withSession: async (nctx) => {
                send({ type: "status", state: "idle" });
                sendSessionInfo(nctx);
              },
            });
            return;
          }
          case "compact":
            ctx.compact({
              onComplete: () => {
                send({ type: "status", state: "idle" });
                send({ type: "assistant_line", msgId: 0, text: "(compaction complete)" });
              },
              onError: (err) => {
                send({ type: "error", code: "compact_failed", message: String(err?.message ?? err) });
              },
            });
            send({ type: "assistant_line", msgId: 0, text: "(compaction started…)" });
            return;
          case "status":
            sendSessionInfo(ctx);
            setState(ctx.isIdle() ? "idle" : currentState === "idle" ? "thinking" : currentState);
            sendTimingSnapshot();
            return;
          case "model": {
            // Mirror the TUI /model behavior: scoped models when scoping is
            // configured, otherwise the full available catalogue.
            const scoped: readonly any[] = ctx.scopedModels ?? [];
            const models: any[] =
              scoped.length > 0
                ? scoped.map((m: any) => m.model).filter(Boolean)
                : (ctx.modelRegistry?.getAvailable?.() ?? []);
            if (arg) {
              // !model provider/id → select via pi.setModel()
              const want = arg;
              const candidates: any[] = [
                ...(ctx.model ? [ctx.model] : []),
                ...models,
              ];
              const found = candidates.find(
                (m) => `${m.provider}/${m.id}` === want || m.id === want,
              );
              if (!found) {
                send({
                  type: "error",
                  code: "model_not_found",
                  message:
                    `no model matching "${want}" — use !model to list available models`,
                });
                return;
              }
              let ok = false;
              try {
                ok = await pi.setModel(found);
              } catch (err) {
                ok = false;
                send({
                  type: "error",
                  code: "model_set_failed",
                  message: String((err as Error)?.message ?? err),
                });
                return;
              }
              if (!ok) {
                send({
                  type: "error",
                  code: "model_set_failed",
                  message: `setModel rejected ${found.provider}/${found.id} (auth?)`,
                });
                return;
              }
              // model_select event will resend session_info
              return;
            }
            if (models.length === 0) {
              send({ type: "assistant_line", msgId: 0, text: "(no available models)" });
            } else {
              for (const m of models) {
                const label = `${m.provider ?? ""}/${m.id ?? "?"}`;
                const current =
                  ctx.model && m.id === ctx.model.id && m.provider === ctx.model.provider;
                send({
                  type: "assistant_line",
                  msgId: 0,
                  text: `model: ${label}${current ? "  (current)" : ""}`,
                });
              }
            }
            return;
          }
          case "cd": {
            // !cd <path> — switch pi to another project directory (new
            // session there). Fuzzy matches and the "create as new project"
            // option are picked in the WeeChat buffer via !pick.
            await runCd(ctx, arg);
            return;
          }
          default:
            send({ type: "error", code: "unknown_command", message: `!${name || "?"}` });
        }
      } catch (err) {
        send({
          type: "error",
          code: "command_failed",
          message: String((err as Error)?.message ?? err),
        });
      }
    },
  });

  // ------------------------------------------- UI prompt channel (!pick)

  /**
   * Ask the WeeChat buffer for interactive input (protocol-3 `ui_request`):
   *   - method "select": numbered options — answered with `!pick <n>`
   *     (comma list when multiple, exact option text also accepted)
   *   - method "input":  free-form text — answered with `!pick <text>`
   * Resolves with a UIAskOutcome:
   *   - { kind: "value" }     the chosen option(s) / entered text
   *   - { kind: "cancelled" } the user typed `!pick cancel`
   *   - { kind: "fallback" }  not connected, optional timeout expired, client
   *     disconnected, malformed reply, or the abort signal fired — callers
   *     should fall back to local UI. Throws nothing.
   */
  function askWeechatUIOutcome(
    req:
      | {
          method: "select";
          title: string;
          options: Array<string | { label: string; description?: string }>;
          multiple?: boolean;
        }
      | { method: "input"; title: string; placeholder?: string },
    opts?: { timeoutMs?: number; signal?: AbortSignal },
  ): Promise<UIAskOutcome> {
    if (!sock || sock.destroyed) return Promise.resolve({ kind: "fallback" });
    const id = ++uiSeq;
    return new Promise((resolve) => {
      let done = false;
      const finish = (o: UIAskOutcome): void => {
        if (done) return;
        done = true;
        pendingUIs.delete(id);
        signal?.removeEventListener("abort", onAbort);
        resumeTiming(`weechat-ui:${id}`);
        resolve(o);
      };
      const signal = opts?.signal;
      const onAbort = (): void => {
        dbg(`ui_request ${id} aborted by the agent — falling back`);
        finish({ kind: "fallback" });
      };
      pendingUIs.set(id, finish);
      pauseTiming(`weechat-ui:${id}`);
      send({ type: "ui_request", id, ...req });
      if (signal) {
        if (signal.aborted) onAbort();
        else signal.addEventListener("abort", onAbort, { once: true });
      }
      if (opts?.timeoutMs && opts.timeoutMs > 0) {
        const t = setTimeout(() => {
          dbg(`ui_request ${id} timed out after ${opts.timeoutMs}ms`);
          finish({ kind: "fallback" });
        }, opts.timeoutMs);
        void t.unref?.();
      }
    });
  }

  /** Promise shape used by !cd and the global handle: value or null. */
  function askWeechatUI(
    req:
      | {
          method: "select";
          title: string;
          options: Array<string | { label: string; description?: string }>;
          multiple?: boolean;
        }
      | { method: "input"; title: string; placeholder?: string },
    timeoutMs?: number,
  ): Promise<string | string[] | null> {
    return askWeechatUIOutcome(req, { timeoutMs }).then((o) => (o.kind === "value" ? o.value : null));
  }

  function clearPendingUIs(reason: string): void {
    if (pendingUIs.size === 0) return;
    dbg(`clearing ${pendingUIs.size} pending ui_request(s): ${reason}`);
    for (const finish of pendingUIs.values()) finish({ kind: "fallback" });
    pendingUIs.clear();
  }

  // Well-known handle on globalThis so OTHER extensions (e.g. ask_user-style
  // tools) can offer their prompts in the WeeChat buffer instead of the TUI:
  // check `globalThis.__pi_weechat_bridge__.isConnected()` first, and fall
  // back to local UI when it is false.
  const WEECHAT_UI_GLOBAL = "__pi_weechat_bridge__";
  let weechatUIConnected = false;

  function setWeechatUIConnected(v: boolean): void {
    weechatUIConnected = v;
    const g = globalThis as Record<string, unknown>;
    const existing = g[WEECHAT_UI_GLOBAL] as { isConnected?: () => boolean } | undefined;
    if (existing && typeof existing.isConnected === "function") return; // already published
    g[WEECHAT_UI_GLOBAL] = {
      isConnected: () => weechatUIConnected,
      select: (
        title: string,
        options: string[],
        opts?: { multiple?: boolean; timeoutMs?: number },
      ) => askWeechatUI({ method: "select", title, options, multiple: opts?.multiple }, opts?.timeoutMs),
      input: (title: string, placeholder?: string, opts?: { timeoutMs?: number }) =>
        askWeechatUI({ method: "input", title, placeholder }, opts?.timeoutMs),
    };
  }

  // ------------------------- decision questions: route to the buffer (!pick)
  //
  // ask_user-style tools (pi-ask-user and friends) block inside execute()
  // waiting for their TUI. We intercept them EARLIER — in the tool_call hook,
  // before execution starts — so when the bridge is connected the question
  // is asked of the WeeChat buffer instead, with no modification to the ask
  // extension itself:
  //   - answered  → block the call; the LLM receives `reason` as the tool
  //                 result (a blocked tool_call surfaces it)
  //   - cancelled → block with a "user cancelled" result
  //   - fallback  → return undefined; the tool runs its own local UI
  //                 (terminal user, bridge down, timeout, abort)
  pi.on("tool_call", async (event: any, ctx: any) => {
    if (!askPickEnabled) return;
    const toolName = String(event?.toolName ?? "");
    if (!askToolNames.has(toolName)) return;
    const input = (event?.input ?? {}) as Record<string, unknown>;
    const question = typeof input.question === "string" ? input.question.trim() : "";
    if (!question) return; // not a question-shaped call — let it run locally
    if (!weechatUIConnected) return; // no bridge: the tool's own UI handles it

    const options = coerceAskOptions(input.options);
    const allowMultiple = input.allowMultiple === true;
    const allowFreeform = input.allowFreeform !== false;
    const title = buildAskTitle(question, input.context);
    const timeoutMs =
      typeof input.timeout === "number" && Number.isFinite(input.timeout) && input.timeout > 0
        ? input.timeout
        : undefined;

    let outcome: UIAskOutcome;
    if (options.length === 0) {
      outcome = await askWeechatUIOutcome(
        { method: "input", title, placeholder: "Type your answer..." },
        { timeoutMs, signal: ctx?.signal },
      );
    } else {
      const selectOptions: Array<string | { label: string; description?: string }> = options.map(
        (o) => (o.description ? { label: o.label, description: o.description } : o.label),
      );
      if (allowFreeform) selectOptions.push(FREEFORM_SENTINEL);
      outcome = await askWeechatUIOutcome(
        { method: "select", title, options: selectOptions, multiple: allowMultiple },
        { timeoutMs, signal: ctx?.signal },
      );
      if (outcome.kind === "value") {
        const picked = Array.isArray(outcome.value) ? outcome.value : [outcome.value];
        if (picked.length === 1 && picked[0] === FREEFORM_SENTINEL) {
          // The user chose the freeform option — ask for the text itself.
          outcome = await askWeechatUIOutcome(
            { method: "input", title, placeholder: "Type your answer..." },
            { timeoutMs, signal: ctx?.signal },
          );
        }
      }
    }

    if (outcome.kind === "fallback") return; // local TUI takes over
    if (outcome.kind === "cancelled") {
      send({ type: "assistant_line", msgId: 0, text: "(question cancelled)" });
      return {
        block: true,
        reason:
          "User cancelled the question via WeeChat (!pick cancel). No answer was given — do not assume one.",
      };
    }
    const summary = formatPickSummary(outcome.value);
    send({ type: "assistant_line", msgId: 0, text: `(question answered: ${summary})` });
    return { block: true, reason: `User answered: ${summary} (via WeeChat !pick)` };
  });

  // ------------------- built-in ask_user fallback (when nothing else provides one)

  const ASK_TOOL_NAME = "ask_user";

  /**
   * When no other extension provides an ask_user tool (e.g. pi-ask-user is
   * not installed), register a minimal built-in one so decision questions
   * are still asked structurally — and answered from WeeChat with !pick via
   * the tool_call hook above (which intercepts before execute() runs). The
   * local path below only runs when the bridge is down; it mirrors
   * pi-ask-user's dialog fallback: select for single choice, comma-list
   * input for multi-select, plain input when there are no options.
   */
  function maybeRegisterFallbackAskTool(ctx: any): void {
    if (fallbackAskRegistered) return;
    try {
      const tools: Array<{ name?: unknown }> = ctx?.getAllTools?.() ?? [];
      if (tools.some((t) => t?.name === ASK_TOOL_NAME)) {
        dbg("ask_user already provided by another extension — skipping the built-in fallback");
        return;
      }
    } catch (err) {
      dbg(`getAllTools failed (${String((err as Error)?.message ?? err)}) — registering the fallback ask tool`);
    }
    fallbackAskRegistered = true;
    dbg("no ask_user tool found — registering the built-in fallback (WeeChat !pick or local TUI)");
    pi.registerTool({
      name: ASK_TOOL_NAME,
      label: "Ask User",
      description:
        "Ask the user a question with optional multiple-choice answers. Use this to gather information interactively. Ask exactly one focused question per call. When the WeeChat bridge is connected the question appears in the WeeChat buffer and is answered there (!pick); otherwise it appears in the pi terminal.",
      promptSnippet:
        "Ask the user one focused question with optional multiple-choice answers to gather information interactively",
      promptGuidelines: [
        "Before calling ask_user, gather context with tools and pass a short summary via the context field.",
        "Use ask_user when the user's intent is ambiguous, when a decision requires explicit user input, or when multiple valid options exist.",
        "Ask exactly one focused question per ask_user call.",
        "Do not combine multiple numbered, multipart, or unrelated questions into one ask_user prompt.",
      ],
      executionMode: "sequential",
      parameters: Type.Object({
        question: Type.String({ description: "The question to ask the user" }),
        context: Type.Optional(
          Type.String({
            description: "Relevant context to show before the question (summary of findings)",
          }),
        ),
        options: Type.Optional(
          Type.Array(
            Type.Object({
              title: Type.String({ description: "Short title for this option" }),
              description: Type.Optional(
                Type.String({ description: "Longer description explaining this option" }),
              ),
            }),
            { description: "List of options for the user to choose from" },
          ),
        ),
        allowMultiple: Type.Optional(
          Type.Boolean({ description: "Allow selecting multiple options. Default: false" }),
        ),
        allowFreeform: Type.Optional(
          Type.Boolean({ description: "Add a freeform text option. Default: true" }),
        ),
        timeout: Type.Optional(
          Type.Number({
            description: "Auto-dismiss after N milliseconds. Returns null (cancelled) when expired.",
          }),
        ),
      }),
      async execute(_toolCallId, params, signal, _onUpdate, ectx: any) {
        // WeeChat answering happens in the tool_call hook BEFORE execution
        // reaches here; this local path runs when the bridge is disconnected
        // (or routing disabled via PI_WEECHAT_PICK=off).
        const p = params as {
          question?: string;
          context?: string;
          options?: unknown;
          allowMultiple?: boolean;
          allowFreeform?: boolean;
          timeout?: number;
        };
        const question = String(p?.question ?? "").trim();
        const options = coerceAskOptions(p?.options);
        const allowMultiple = p?.allowMultiple === true;
        const allowFreeform = p?.allowFreeform !== false;
        const contextText = typeof p?.context === "string" ? p.context.trim() : "";
        const prompt = contextText ? `${question}\n\nContext:\n${contextText}` : question;
        const dialogOpts =
          typeof p?.timeout === "number" && p.timeout > 0 ? { timeout: p.timeout } : undefined;

        if (signal?.aborted) {
          return { content: [{ type: "text", text: "Cancelled" }], details: {} };
        }

        if (!ectx?.hasUI || !ectx.ui) {
          const optionText =
            options.length > 0
              ? `\n\nOptions:\n${options.map((o, i) => `${i + 1}. ${o.label}`).join("\n")}`
              : "";
          return {
            content: [
              { type: "text", text: `Ask requires interactive mode. Please answer:\n\n${prompt}${optionText}` },
            ],
            isError: true,
            details: {},
          };
        }

        let answer: string | undefined;
        if (options.length === 0) {
          answer = await ectx.ui.input(prompt, "Type your answer...", dialogOpts);
        } else if (allowMultiple) {
          // ui.select is single-choice only: mirror pi-ask-user's comma-list input
          const optionList = options.map((o, i) => `${i + 1}. ${o.label}${o.description ? ` — ${o.description}` : ""}`).join("\n");
          answer = await ectx.ui.input(
            `${prompt}\n\nOptions (select one or more):\n${optionList}`,
            "Type your selection(s)...",
            dialogOpts,
          );
          if (answer) {
            const names = answer.split(",").map((s) => s.trim()).filter(Boolean);
            const labels = names.map((n) => {
              const idx = parseInt(n, 10);
              if (Number.isInteger(idx) && options[idx - 1]) return options[idx - 1].label;
              const byTitle = options.find((o) => o.label === n);
              return byTitle ? byTitle.label : n;
            });
            answer = labels.length > 0 ? labels.join(", ") : undefined;
          }
        } else {
          const selectOptions = options.map((o) => o.label);
          if (allowFreeform) selectOptions.push(FREEFORM_SENTINEL);
          const selected = await ectx.ui.select(prompt, selectOptions, dialogOpts);
          if (selected === FREEFORM_SENTINEL) {
            answer = await ectx.ui.input(prompt, "Type your answer...", dialogOpts);
          } else {
            answer = selected;
          }
        }

        const trimmed = typeof answer === "string" ? answer.trim() : "";
        if (!trimmed) {
          return { content: [{ type: "text", text: "User cancelled the question" }], details: {} };
        }
        return { content: [{ type: "text", text: `User answered: ${trimmed}` }], details: {} };
      },
    });
  }

  // ------------------------------------------------------------- !cd

  /**
   * !cd <path>: switch pi to a different project directory (new session in
   * that cwd). Shares lib/cd-search.mjs with the /cd TUI command
   * (extensions/cd.ts): an exact existing directory switches immediately;
   * otherwise similar directories are fuzzy-searched and picked in the
   * WeeChat buffer (!pick), always including a "create as new project"
   * option.
   */
  async function runCd(ctx: any, rawArg?: string): Promise<void> {
    await ctx.waitForIdle?.();
    const input = expandTilde(String(rawArg ?? "").trim());
    if (!input) {
      send({ type: "error", code: "cd_usage", message: "usage: !cd <path> — e.g. !cd ~/my-project" });
      return;
    }
    const target = path.isAbsolute(input) ? path.resolve(input) : path.resolve(ctx.cwd, input);

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
      send({ type: "error", code: "cd_not_a_dir", message: `${target} exists but is not a directory.` });
      return;
    }

    // Tell the buffer we are waiting for !pick (shown in its title).
    send({ type: "status", state: busy ? "thinking" : "idle", detail: "awaiting !pick" });
    const choice = await askWeechatUI({ method: "select", title, options });
    send({ type: "status", state: busy ? "thinking" : "idle" });
    if (!choice || (Array.isArray(choice) && choice.length === 0)) {
      send({ type: "assistant_line", msgId: 0, text: "(cd cancelled)" });
      return;
    }
    const picked = Array.isArray(choice) ? choice[0] : choice;

    let destination: string;
    if (picked === createOption) {
      try {
        fs.mkdirSync(target, { recursive: true });
      } catch (err) {
        send({
          type: "error",
          code: "cd_create_failed",
          message: `could not create ${target}: ${(err as Error).message}`,
        });
        return;
      }
      destination = target;
    } else {
      destination = picked;
    }

    await switchSessionToDir(destination, ctx);
  }

  /**
   * Pre-write a minimal session header (cwd = target dir) into the default
   * session directory for that cwd, then switch to it. A brand-new
   * SessionManager does not flush its file until the first assistant message
   * — without the pre-written header pi would stay on the old cwd. After the
   * switch pi re-emits session_start (reason "resume"), which makes this
   * extension resend session_info/status for the new cwd.
   */
  async function switchSessionToDir(targetCwd: string, ctx: any): Promise<void> {
    const resolvedTarget = path.resolve(targetCwd);
    const sessionFile = writeCdSessionHeader(resolvedTarget);
    dbg(`cd: switching to ${resolvedTarget} (pre-wrote ${sessionFile})`);
    const result = await ctx.switchSession(sessionFile, {
      withSession: (next: any) => {
        ctxRef = next; // old command ctx is stale after the replacement
      },
    });
    if (result?.cancelled) {
      send({ type: "assistant_line", msgId: 0, text: "(cd cancelled)" });
    }
  }
}

// ------------------------------------------------------------------ helpers

function intOf(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

function extractToolOutput(result: any): string {
  if (result == null) return "";
  if (typeof result === "string") return result;
  if (Array.isArray(result.content)) {
    return result.content
      .map((c: any) => (c && typeof c.text === "string" ? c.text : ""))
      .filter(Boolean)
      .join("\n");
  }
  try {
    return JSON.stringify(result);
  } catch {
    return "";
  }
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  const lines = text.split("\n");
  let out = "";
  for (const line of lines) {
    if (out.length + line.length + 1 > max) break;
    out += (out ? "\n" : "") + line;
  }
  return out + `\n… (${text.length - out.length} more characters truncated)`;
}
