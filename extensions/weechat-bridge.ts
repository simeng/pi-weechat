/**
 * weechat-bridge.ts — pi extension: mirror this session into a WeeChat buffer.
 *
 * Connects (as client) to the Unix socket served by the WeeChat script
 * (weechat/pi_bridge.py), mirrors assistant text (batched into whole lines),
 * tool calls/results, and status; forwards lines typed in the WeeChat buffer
 * back to pi as user input. Wire format: NDJSON, see PLAN.md §2.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
// @ts-ignore - plain ESM module, no types needed
import { LineDecoder, PROTOCOL_VERSION } from "../lib/codec.mjs";

const MAX_TOOL_OUTPUT = 8192;
const PING_INTERVAL_MS = 30_000;
const PONG_TIMEOUT_MS = 90_000;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

function resolveSocketPath(): string {
  if (process.env.PI_WEECHAT_SOCK) return process.env.PI_WEECHAT_SOCK;
  const xdg = process.env.XDG_RUNTIME_DIR;
  if (xdg) return path.join(xdg, "pi-weechat.sock");
  return path.join(os.homedir(), ".local", "state", "pi-weechat", "pi-weechat.sock");
}

export default function weechatBridge(pi: ExtensionAPI) {
  let socketPath = resolveSocketPath();
  let sock: net.Socket | null = null;
  let decoder: LineDecoder | null = null;
  let shutdown = false; // session_shutdown was emitted; stop reconnecting
  let attempt = 0;
  let connectTimer: NodeJS.Timeout | null = null;
  let pingTimer: NodeJS.Timeout | null = null;
  let lastPongAt = 0;
  let ctxRef: any = null; // latest ExtensionContext (for ctx.abort())
  let pendingOut: string[] = []; // messages emitted before the socket is up

  // Streaming assembly: assistant text blocks, keyed by contentIndex.
  let blockBufs = new Map<number, string>();
  let msgSeq = 0;
  let currentMsgId = 0;

  // ------------------------------------------------------------------ send

  function send(obj: Record<string, unknown>): void {
    const line = JSON.stringify(obj) + "\n";
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
    connectTimer = setTimeout(() => connect(), delay);
    void connectTimer.unref?.();
  }

  function disconnect(): void {
    stopPing();
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

    const s = net.connect(socketPath);
    sock = s;
    decoder = new LineDecoder(onMessage, {
      onError: (e: Error) =>
        send({ type: "error", code: "client_error", message: e.message }),
    });

    s.once("connect", () => {
      attempt = 0;
      lastPongAt = Date.now();
      flushPending(); // session_info/status emitted before connect
      send({ type: "hello", protocol: PROTOCOL_VERSION, name: "pi-weechat-bridge" });
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
    s.once("error", () => {
      // ECONNREFUSED etc. — weechat not running yet; retry with backoff.
      disconnect();
      scheduleReconnect();
    });
    s.once("close", () => {
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
    switch (msg.type) {
      case "hello":
        if (intOf(msg.protocol) !== PROTOCOL_VERSION) {
          send({ type: "error", code: "protocol_mismatch" });
          disconnect();
          scheduleReconnect();
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
        const deliverAs =
          msg.deliverAs === "steer" || msg.deliverAs === "followUp" ? msg.deliverAs : undefined;
        pi.sendUserMessage(text, deliverAs ? { deliverAs } : undefined);
        return;
      }
      case "command":
        handleCommand(msg.name as string | undefined);
        return;
      default:
        // unknown type from weechat: ignore (forward-compat)
    }
  }

  function handleCommand(name?: string): void {
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
        // route through a registered extension command so session-control
        // methods (newSession, …) run in the safe command context
        pi.sendUserMessage(`/weechat-ctl ${name}`, { deliverAs: "followUp" });
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
    shutdown = false;
    attempt = 0;
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
    setState("thinking");
  });

  pi.on("agent_settled", async (_event, ctx) => {
    ctxRef = ctx;
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
  pi.on("message_start", async (event) => {
    const msg = event.message as any;
    if (msg?.role === "assistant") {
      blockBufs.clear();
      currentMsgId = ++msgSeq;
    }
  });

  pi.on("message_update", async (event) => {
    const ev = (event as any).assistantMessageEvent;
    if (!ev || typeof ev !== "object") return;
    if (ev.type === "text_start") {
      blockBufs.set(ev.contentIndex ?? 0, "");
    } else if (ev.type === "text_delta") {
      const key = ev.contentIndex ?? 0;
      const cur = (blockBufs.get(key) ?? "") + String(ev.delta ?? "");
      blockBufs.set(key, cur);
      // flush complete lines as they form
      const nl = cur.indexOf("\n");
      if (nl !== -1) {
        send({ type: "assistant_line", msgId: currentMsgId, text: cur.slice(0, nl) });
        blockBufs.set(key, cur.slice(nl + 1));
      }
    } else if (ev.type === "text_end") {
      // remaining partial line of this block is complete now
      const key = ev.contentIndex ?? 0;
      const tail = blockBufs.get(key);
      if (tail && tail.trim()) {
        send({ type: "assistant_line", msgId: currentMsgId, text: tail });
      }
      blockBufs.set(key, "");
    } else if (ev.type === "toolcall_start") {
      setState("thinking");
    }
  });

  pi.on("message_end", async (event) => {
    const msg = event.message as any;
    if (msg?.role !== "assistant") return;
    // authoritative final text: re-emit any tail the deltas didn't flush
    const blocks = [...blockBufs.values()].filter((t) => t.trim()).join("\n");
    if (blocks) {
      for (const line of blocks.split("\n")) {
        send({ type: "assistant_line", msgId: currentMsgId, text: line });
      }
    }
    blockBufs.clear();
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
    handler: async (args, ctx) => {
      const name = String(args ?? "").trim();
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
            const models = (ctx as any).scopedModels ?? [];
            if (models.length === 0) {
              send({ type: "assistant_line", msgId: 0, text: "(no scoped models)" });
            } else {
              for (const m of models) {
                send({
                  type: "assistant_line",
                  msgId: 0,
                  text: `model: ${m.model?.provider ?? ""}/${m.model?.id ?? "?"}`,
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
