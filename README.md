# pi-weechat

Mirror a [pi](https://pi.dev) coding agent session into a WeeChat buffer — and type back at pi — over a local Unix socket.

- **WeeChat side**: Python script that creates a `pi` buffer and serves an NDJSON protocol on a Unix socket (the *server*).
- **pi side**: TypeScript extension installed as a pi package; connects as *client*, mirrors assistant output / tool calls / status into the buffer, and forwards lines you type back to pi as user input.

Architecture and wire protocol: [PLAN.md](./PLAN.md).

## Layout

```
extensions/weechat-bridge.ts   pi extension (client)
weechat/pi_bridge.py           WeeChat script (socket server)
lib/codec.mjs                  shared NDJSON codec constants (protocol version, limits)
test/                          node:test suite + python smoke + cross-language integration
```

## Requirements

- Node.js ≥ 22.18 (`--experimental-strip-types`) or a pi version that bundles one; pi itself for the extension side.
- Python 3.9+ (stdlib only) and WeeChat ≥ 3.x with the Python plugin, for the buffer side.
- Both components on the **same machine** (Unix domain socket, no network exposure).

## Install

### 1. pi extension (pi package)

```sh
pi install /path/to/pi-weechat      # or: git clone … && pi install <dir>
```

This loads `extensions/weechat-bridge.ts` into every pi session. It stays dormant
until a WeeChat bridge socket exists; when one appears it connects automatically
(retries with backoff until then, and reconnects if the connection drops).

### 2. WeeChat script

```sh
mkdir -p ~/.local/share/weechat/python
cp weechat/pi_bridge.py ~/.local/share/weechat/python/
```

Start (or restart) WeeChat — a new buffer named **`pi`** appears, and the script
listens on the socket. In a running WeeChat:

```
/python load pi_bridge
```

## Usage

Open the `pi` buffer:

- **Output**: assistant messages stream in as whole lines; tool calls show as
  `⚙ name {args}` with indented results (`✔` ok / `✘` error); the buffer title
  tracks state — `(idle)`, `(thinking…)`, `(tool: bash)` — and shows
  `(disconnected — waiting for pi)` when pi is not connected.
- **Input**: type a line and press enter to send it as a prompt to pi.
  - `!s <text>` — steer the current run (interrupts, injects)
  - `!q <text>` — queue a follow-up message for after the current turn
  - `!new` — new session
  - `!compact` — compact the session
  - `!abort` — abort the current run
  - `!status` / `!model` — resend session info / list scoped models
  - `!model <provider/model>` — switch model (routes to pi's `/model`)
- Prompts you type directly in pi's own terminal are echoed into the buffer too,
  so both surfaces stay in sync.

### Socket path

Both sides resolve it identically (first match wins):

1. `$PI_WEECHAT_SOCK`
2. `$XDG_RUNTIME_DIR/pi-weechat.sock`
3. `~/.local/state/pi-weechat/pi-weechat.sock`

Set `PI_WEECHAT_SOCK` in both environments to override (e.g. for multiple
machines' worth of sandboxes, or when `$XDG_RUNTIME_DIR` is absent). The socket
file is created `0700`; only local users who can read/write it can connect.

## Notes & limitations (v1)

- **One pi session per WeeChat buffer.** A second connecting client is rejected
  (`client_already_connected`). Multi-session multiplexing is on the roadmap (PLAN §10).
- Assistant text is rendered in whole lines (WeeChat has no partial-line redraw);
  the extension batches token deltas and flushes completed lines.
- Tool output is truncated to ~8 KiB per result (whole-line boundary) to keep
  buffers readable.
- WeeChat config options (`/set`) are a v2 item; v1 uses constants + env var.

## Development

```sh
npm test          # node:test suite (codec + extension) + python smoke test
npm run test:js   # JS tests only (needs Node ≥ 22.18)
npm run test:py   # weechat script smoke test only (python3, stdlib only)
```

The integration test spawns the real Python bridge and drives it with the real
TypeScript extension over a live socket — no mocks on the wire.
