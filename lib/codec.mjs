// Shared NDJSON wire format for the pi <-> weechat bridge.
// Used by the pi extension (lib) and by tests. The WeeChat side implements
// the same framing in Python (see weechat/pi_bridge.py).

export const PROTOCOL_VERSION = 1;
export const MAX_LINE_BYTES = 1024 * 1024; // 1 MiB hard limit per message

/** Encode a protocol message into a single NDJSON line. */
export function encodeMessage(obj) {
  return JSON.stringify(obj) + "\n";
}

/**
 * Incremental line decoder. Feed it raw socket chunks (Buffer or string);
 * it calls onMessage for every complete, valid JSON line.
 *
 * Errors are reported via onError({ code, message }) instead of throwing,
 * so a single bad line never kills the connection (the frame is dropped).
 */
export class LineDecoder {
  constructor(onMessage, { maxLineBytes = MAX_LINE_BYTES, onError } = {}) {
    this.onMessage = onMessage;
    this.maxLineBytes = maxLineBytes;
    this.onError = onError ?? (() => {});
    this.pending = [];
    this.pendingLen = 0;
  }

  feed(chunk) {
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk), "utf8");
    if (buf.length === 0) return;
    this.pending.push(buf);
    this.pendingLen += buf.length;

    // Trim frames that already exceed the limit so memory stays bounded.
    if (this.pendingLen > this.maxLineBytes && !this.pending.some((b) => b.includes(10))) {
      this.pending = [];
      this.pendingLen = 0;
      this.onError({ code: "line_too_long", message: `message exceeds ${this.maxLineBytes} bytes` });
      return;
    }

    let data = Buffer.concat(this.pending);
    for (;;) {
      const nl = data.indexOf(10); // \n
      if (nl === -1) break;
      const line = data.subarray(0, nl);
      data = data.subarray(nl + 1);
      if (line.length === 0) continue; // blank line — ignore
      if (line.length > this.maxLineBytes) {
        this.onError({ code: "line_too_long", message: `message exceeds ${this.maxLineBytes} bytes` });
        continue;
      }
      let msg;
      try {
        msg = JSON.parse(line.toString("utf8"));
      } catch (err) {
        this.onError({ code: "bad_json", message: String(err.message ?? err) });
        continue;
      }
      if (msg && typeof msg === "object" && typeof msg.type === "string") {
        this.onMessage(msg);
      } else {
        this.onError({ code: "bad_message", message: "missing string 'type' field" });
      }
    }
    this.pending = [data];
    this.pendingLen = data.length;
  }

  /** Reset internal buffering (e.g. on reconnect). */
  reset() {
    this.pending = [];
    this.pendingLen = 0;
  }
}

/** Standard hello for the handshake. */
export function makeHello(name) {
  return { type: "hello", protocol: PROTOCOL_VERSION, name };
}
