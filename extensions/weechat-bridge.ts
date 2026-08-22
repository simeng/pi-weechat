/**
 * weechat-bridge.ts — pi extension: mirror this session into a WeeChat buffer.
 *
 * Dials (as client) the WeeChat script (weechat/pi_bridge.py) over a Unix
 * socket or TCP (PI_WEECHAT_URL: tcp://host:port, unix://<path>, …), mirrors
 * assistant text (batched into whole lines), tool calls/results, and status;
 * forwards lines typed in the WeeChat buffer back to pi as user input.
 * Wire format: NDJSON, protocol 2 (challenge-response auth when a token is
 * configured — the token is never sent). See PLAN.md §2.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
// @ts-ignore - plain ESM module, no types needed
import { LineDecoder, PROTOCOL_VERSION, parseEndpoint } from "../lib/codec.mjs";

const MAX_TOOL_OUTPUT = 8192;
const DEBUG_LOG_MAX_BYTES = 1_000_000; // rotate above this

// Opt-in wire debug log. Enabled by PI_BRIDGE_DEBUG=<path>, or by the
// existence of a marker file $XDG_RUNTIME_DIR/pi-weechat.debug (the WeeChat
// script honors the same marker, so one file enables both sides).
function resolveDebugLogPath(): string | null {
  const env = process.env.PI_BRIDGE_DEBUG;
  if (env) return env;
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

function reinitDebugLog(): void {
  const p = resolveDebugLogPath();
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
 * Resolve where to dial: PI_WEECHAT_URL (tcp://host:port, unix://<path>,
 * schemeless host:port → tcp, anything else → socket path), else the
 * deprecated PI_WEECHAT_SOCK, else the default unix path.
 */
function resolveEndpoint(): Endpoint {
  const url = process.env.PI_WEECHAT_URL;
  if (url) {
    try {
      return parseEndpoint(url) as Endpoint;
    } catch (err) {
      dbg(`PI_WEECHAT_URL is invalid (${String(err)}); falling back to the unix socket path`);
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
  reinitDebugLog();
  let endpoint: Endpoint = resolveEndpoint();
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
  let pendingOut: string[] = []; // messages emitted before the socket is up
  let helloSent = false;         // our hello went out for the current connection
  let challengeTimer: NodeJS.Timeout | null = null;

  // Streaming assembly: assistant text + thinking blocks, keyed by contentIndex.
  let blockBufs = new Map<number, string>();
  let thinkBufs = new Map<number, string>();
  let msgSeq = 0;
  let currentMsgId = 0;

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
        }
        return;
      case "error":
        if (msg.code === "auth_failed") {
          // The server dropped us for a bad proof — usually a token
          // mismatch. Keep retrying with the normal backoff; the buffer
          // side already printed the red error line.
          dbg("AUTH FAILED: token mismatch? check PI_WEECHAT_TOKEN / pi_bridge.token — retrying");
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
        send({ type: "status", state: "idle" });
        return;
      case "new_session":
      case "compact":
      case "status":
      case "model":
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
    reinitDebugLog(); // picks up a marker file created after process start
    // (re-)read the endpoint + token: env can change across /reload
    endpoint = resolveEndpoint();
    token = process.env.PI_WEECHAT_TOKEN ?? "";
    shutdown = false;
    attempt = 0;
    busy = false;
    ctxRef = ctx;
    blockBufs.clear();
    sendSessionInfo(ctx);
    setState("idle");
    connect();
  });

  pi.on("session_shutdown", () => {
    teardown();
  });

  pi.on("agent_start", async (_event, ctx) => {
    ctxRef = ctx;
    busy = true;
    setState("thinking");
  });

  pi.on("agent_settled", async (_event, ctx) => {
    ctxRef = ctx;
    busy = false;
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
            send({ type: "status", state: ctx.isIdle() ? "idle" : "thinking" });
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
