// Shared NDJSON wire format for the pi <-> weechat bridge.
// Used by the pi extension (lib) and by tests. The WeeChat side implements
// the same framing in Python (see weechat/pi_bridge.py).

export const PROTOCOL_VERSION = 2;
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

/**
 * Standard hello for the handshake.
 *
 * Protocol 2 (shared-secret auth): when the server has a token configured it
 * first sends `{"type":"challenge","nonce":...}`; the client answers with a
 * hello carrying `proof` = hex(HMAC-SHA256(key=token, msg=nonce)). The token
 * itself is NEVER sent. `proof` is only included when non-empty, so the
 * anonymous path (no token anywhere) stays a bare hello.
 */
export function makeHello(name, proof) {
  const hello = { type: "hello", protocol: PROTOCOL_VERSION, name };
  if (proof) hello.proof = proof;
  return hello;
}

/**
 * Parse a bridge endpoint string into a transport descriptor.
 *
 *   tcp://host:port            → { kind: "tcp", host, port }
 *   unix://<path>, unix:<path> → { kind: "unix", path }
 *   host:port (schemeless, numeric port) → tcp (host may be a Tailscale
 *                        name — "weechat-box:52311" is host:port, not a
 *                        scheme)
 *   anything else              → { kind: "unix", path }  (socket path;
 *                        "C:\…" is a drive letter, not a scheme)
 *
 * An unknown prefix is a scheme only when written "prefix://…" (e.g.
 * tls://host:1) and THROWS — the intentional extension point for future
 * transports. Without "//" it stays an ordinary host:port / path.
 */
export function parseEndpoint(input) {
  const s = String(input ?? "").trim();
  if (!s) {
    throw new Error(
      'empty endpoint (expected tcp://host:port, unix://<path>, host:port, or a socket path)',
    );
  }

  const colon = s.indexOf(":");
  if (colon !== -1) {
    const prefix = s.slice(0, colon);
    const looksLikeScheme = /^[a-zA-Z][a-zA-Z0-9+.\-]*$/.test(prefix);
    const rest = s.slice(colon + 1);
    if (looksLikeScheme) {
      const scheme = prefix.toLowerCase();
      if (scheme === "tcp") {
        const target = rest.startsWith("//") ? rest.slice(2) : rest;
        const i = target.lastIndexOf(":");
        if (i <= 0) throw new Error(`tcp endpoint needs "host:port": ${s}`);
        const host = target.slice(0, i);
        const portStr = target.slice(i + 1);
        if (!host) throw new Error(`tcp endpoint needs a host: ${s}`);
        if (!/^\d+$/.test(portStr)) {
          throw new Error(`tcp endpoint needs a numeric port: ${s}`);
        }
        const port = Number(portStr);
        if (port < 1 || port > 65535) {
          throw new Error(`tcp port out of range (1-65535): ${s}`);
        }
        return { kind: "tcp", host, port };
      }
      if (scheme === "unix") {
        const target = rest.startsWith("//") ? rest.slice(2) : rest;
        if (!target) throw new Error(`unix endpoint needs a socket path: ${s}`);
        return { kind: "unix", path: target };
      }
      if (rest.startsWith("//")) {
        throw new Error(
          `unsupported endpoint scheme "${scheme}" (supported: tcp, unix): ${s}`,
        );
      }
      // otherwise: ordinary host:port / path, handled below
    }
  }

  // schemeless: "host:port" with a numeric port → TCP, else a socket path
  const bare = s.match(/^([^\s:]+):(\d+)$/);
  if (bare && Number(bare[2]) >= 1 && Number(bare[2]) <= 65535) {
    return { kind: "tcp", host: bare[1], port: Number(bare[2]) };
  }
  return { kind: "unix", path: s };
}
