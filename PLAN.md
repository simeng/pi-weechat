# pi-weechat — Mirror pi agent I/O through a WeeChat buffer

Goal: chat with a [pi](https://pi.dev) coding agent from inside WeeChat. A pi
**package** (extension) and a **WeeChat Python script** run on the same machine
and communicate over a local Unix domain socket. Everything pi says — assistant
streaming text, tool calls, results, status changes — is mirrored into a
dedicated WeeChat buffer, and anything typed in that buffer is sent to pi as
user input.

## 1. Architecture

```
┌───────────────────────────┐          ┌──────────────────────────────┐
│  WeeChat                   │          │  pi (TUI or headless)         │
│  ┌───────────────────────┐ │  Unix   │  ┌──────────────────────────┐ │
│  │ buffer "pi"           │ │  domain │  │ extension                 │ │
│  │  pi_bridge.py         │◄┼─socket──┼►│  weechat-bridge.ts        │ │
│  │  · socket SERVER      │ │  (NDJSON│  │  · socket CLIENT          │ │
│  │  · hook_fd read/write │ │   JSON) │  │  · node:net client +      │ │
│  │  · buffer input_cb    │ │         │  │    reconnect backoff      │ │
│  └──────────┬────────────┘ │         │  └──────────┬───────────────┘ │
│             │              │         │             │ pi.on(...)       │
│   user types line          │         │   message_start /              │
│   Enter → input callback   │         │   message_update (stream)      │
└───────────────────────────┘         └──────────────────────────────┘
```

- **WeeChat script = socket server.** WeeChat is the long-lived process; pi
  sessions start/stop/reload all the time. The script `bind()`s the socket at
  load and accepts exactly one client (extra clients are rejected with a short
  error message).
- **Pi extension = socket client.** Connects on `session_start`, retries with
  exponential backoff (1s → 30s cap) while WeeChat/pi order is different, and
  transparently reconnects if the peer dies. On `session_shutdown` it closes
  the socket cleanly.

Why a Unix socket (not TCP loopback or pipes): same machine only, no port
collisions, no network exposure, and both sides already have first-class
non-blocking fd APIs (`hook_fd` on WeeChat, `node:net` on pi).

## 2. Socket & protocol

- **Path:** `$XDG_RUNTIME_DIR/pi-weechat.sock`, falling back to
  `~/.local/state/pi-weechat/pi-weechat.sock`. Overridable by config on both
  sides (`pi_bridge.py` reads a WeeChat option; the extension reads an env var
  / pi setting). Socket created with `0700` permissions.
- **Framing:** newline-delimited JSON (NDJSON), UTF-8. Max message size guard
  of 1 MiB (larger tool outputs are chunked by the sender, see §5).
- **Handshake:** first message in each direction is
  `{"type":"hello","protocol":1,"name":"weechat-pi-bridge"}`. Version mismatch
  → close with `{"type":"error","code":"protocol_mismatch",...}`.

### Message types (pi extension → WeeChat)

| type             | payload                          | meaning                                        |
|------------------|----------------------------------|------------------------------------------------|
| `hello`          | `{protocol, name, version}`      | handshake                                      |
| `status`         | `{state, detail?}`               | state ∈ idle / thinking / tool / error; drives buffer title + status line |
| `assistant_delta`| `{msgId, text}`                  | streaming token chunk (from `message_update`)  |
| `assistant_end`  | `{msgId, text?}`                 | final assistant message (replaces buffered stream) |
| `user_echo`      | `{text}`                         | user prompt as accepted by pi (mirror back if input originated in the pi terminal) |
| `tool_start`     | `{toolCallId, toolName, args}`   | from `tool_execution_start`                    |
| `tool_update`    | `{toolCallId, partialResult?}`   | from `tool_execution_update` (throttled)       |
| `tool_end`       | `{toolCallId, isError, output}`  | from `tool_execution_end`; `output` chunked if > 8 KiB |
| `session_info`   | `{name?, model?, cwd}`           | on `session_start` / `model_select` / `session_info_changed` |
| `error`          | `{code, message}`                | protocol or runtime error                      |
| `ping`           | `{ts}`                           | keepalive every 30 s (WeeChat answers with pong) |

### Message types (WeeChat → pi extension)

| type        | payload                          | meaning                                        |
|-------------|----------------------------------|------------------------------------------------|
| `hello`     | as above                         | handshake                                      |
| `user_input`| `{text, deliverAs?}`             | typed line; `deliverAs` ∈ undefined (normal), `"steer"` (`!s ...` prefix → mid-stream steering), `"followUp"` (`!q ...`) |
| `command`   | `{name}`                         | name ∈ `new_session`, `compact`, `abort`, `reload`; mapped to pi session controls from a registered extension command handler (never called from event handlers, to avoid deadlock) |
| `pong`      | `{ts}`                           | keepalive response                             |

Buffer-local commands typed in the WeeChat buffer (all start with `!` so they
never reach the LLM):

- `!s <text>`  — steer (interrupt current stream, inject)
- `!q <text>`  — queue follow-up message
- `!new`       — new session (`ctx.newSession()`)
- `!compact`   — compaction (`ctx.compact()`)
- `!abort`     — abort current turn (`ctx.abort()`)
- `!model`     — print available scoped models; `!model <provider/id>` selects
- `!status`    — force a status refresh (session file, model, context usage)

## 3. WeeChat side — `weechat/pi_bridge.py`

Pure CPython 3, stdlib only (`socket`, `json`, `os`). Drops into
`~/.local/share/weechat/python/`, loads at WeeChat start (or
`/python load pi_bridge`).

Key WeeChat API pieces used (verified against the official Python stub):

```python
# buffer that accepts input: Enter fires the callback, RC_OK consumes the line
buf = weechat.buffer_new("pi", "pi_input_cb", "", "pi_close_cb", "")
weechat.buffer_set(buf, "title", "pi: (connecting)")
weechat.buffer_set(buf, "localvar_set_no_log", "1")  # optional

def pi_input_cb(data, buffer, line):
    send_msg({"type": "user_input", "text": line})
    return weechat.WEECHAT_RC_OK        # clear input field

# non-blocking I/O inside WeeChat's event loop:
weechat.hook_fd(fd, 1, 0, 4, "pi_fd_cb", "")   # READ | HUP flags; cb(data, fd) -> RC_OK/RC_FAILED
```

Design:

- **Server socket:** bound in `weechat.register()` path (module load). Accept
  via a listening-fd hook (`hook_fd` with the listen flag); on connect, `bind()`
  the client fd to the same read/hup callback. Only one client at a time; if a
  second connects, reply one `error{code: "client_already_connected"}` and close.
- **Reader:** accumulate in a buffer, split on `\n`, JSON-parse each line,
  dispatch on `type`. Partial reads wait for more data (return `RC_OK`).
- **Writer:** `sendall()` is safe enough at these volumes; if the socket ever
  becomes writable-blocked, queue and re-send via `hook_fd` write flag.
- **Rendering** (`buffer_print` with WeeChat color tags):
  - `assistant_delta`: append to a "current stream" line using
    `buffer_set(buf, "clear_last_line", "1")` + reprint, or print deltas as
    short segments and rely on buffer scroll. (Decision: segment printing —
    simpler, no flicker, matches how pi's own TUI streams.)
  - `tool_start`: dim line `⚙ tool_name(arg summary)`; `tool_end`:
    green/red prefix + first N lines of output, remainder as "… (k more lines)".
  - `status`: update buffer `title` (`pi: idle`, `pi: thinking…`,
    `pi: tool: bash`) and print a dim separator on transitions.
  - `session_info`: one bold line with cwd + model.
- **Connection state in the buffer title:** `(idle)`, `(thinking…)`,
  `(tool: X)`, `(disconnected — waiting for pi)`.
- **Cleanup:** on close callback / WeeChat quit, unhook fd, unlink socket file.

## 4. Pi side — `extensions/weechat-bridge.ts` (pi package)

Loaded via the pi manifest in `package.json`:

```json
{
  "name": "pi-weechat",
  "keywords": ["pi-package"],
  "pi": { "extensions": ["./extensions"] },
  "peerDependencies": { "@earendil-works/pi-coding-agent": "*" }
}
```

Install with `pi install /path/to/pi-weechat` (or git/npm once published to
the pi.dev gallery).

Structure (single file is fine at this size; split if it grows):

- **Socket client** (`node:net`): lazy connect in `session_start` (per docs, no
  background resources from the factory — defer to session events).
  - Backoff reconnect loop (1 s → 2 s → … cap 30 s), cancelled on
    `session_shutdown`.
  - NDJSON codec: same framing + max-size guard; chunk oversized payloads as
    `{type:"chunk_start", id, total}`, `{type:"chunk", id, seq, data}`,
    `{type:"chunk_end", id}`.
- **Mirroring** (event → socket):
  | pi event              | emits                                   |
  |-----------------------|------------------------------------------|
  | `session_start`       | connect + `session_info` + `status{idle}`|
  | `message_update`      | `assistant_delta` (throttle ~30 ms batches) |
  | `message_end`         | `assistant_end` / tool result text       |
  | `tool_execution_*`    | `tool_start` / `tool_update`(throttled)/ `tool_end` |
  | `agent_start`/`agent_settled` | `status{thinking}` / `status{idle}` |
  | `model_select`, `session_info_changed` | `session_info`          |
  | `input` (source ≠ "extension") | `user_echo` (mirror prompts typed in the pi terminal itself, so both surfaces stay in sync) |
- **Input handling:** on `user_input`, call
  `pi.sendUserMessage(text, { deliverAs })`. Because injected messages arrive
  back at pi with `event.source === "extension"`, the `input` handler skips
  them for `user_echo` — no echo loops.
- **Commands:** on `command`, route to a registered extension command's handler
  (`pi.registerCommand("weechat:ctl", ...)` whose `ExtensionCommandContext` has
  `newSession` / `compact` / `abort`). Commands run outside event handlers so
  session-control calls can't deadlock.
- **Keepalive:** 30 s ping; drop and reconnect if no pong for 90 s.

### Mode note
The extension works in TUI mode (pi visible in a terminal, WeeChat mirrors the
same session — two windows on one agent) and is most useful with headless
usage (`pi -p` per message or RPC). Guard any `ctx.ui.*` calls behind
`ctx.hasUI`.

## 5. Ordering & backpressure details

- All pi→WeeChat messages for a single assistant turn are sent in event order;
  WeeChat renders in arrival order, so no sequence numbers needed (v1).
- Streaming deltas are batched on the pi side (flush every ~30 ms or 2 KiB) to
  keep WeeChat's redrawing cheap.
- Tool output > 8 KiB is truncated for display but chunked intact if the
  WeeChat side later requests it (`!tool <id>` → fetch full output; v2).
- If pi reconnects mid-turn, WeeChat prints a `— reconnected —` separator and
  pi sends a `session_info` + status snapshot; partial assistant text of the
  in-flight turn is simply re-streamed on next `message_update`.

## 6. Security

- Local socket only: file mode `0700`, created in the user's private
  state dir. No network listener, no auth needed beyond Unix ownership.
- WeeChat side never executes anything from the socket; pi side treats
  incoming JSON as data. The pi extension is untrusted-code-by-design (all pi
  extensions are) — review before installing.

## 7. Configuration

| setting              | where                          | default                             |
|----------------------|--------------------------------|-------------------------------------|
| `pi_bridge.socket`   | WeeChat option                 | `$XDG_RUNTIME_DIR/pi-weechat.sock`  |
| `pi_bridge.show_tools`| WeeChat option                | `on`                                |
| socket path          | pi extension: `PI_WEECHAT_SOCK` env or `.pi/settings.json` extension setting | same as above |

## 8. Milestones

1. **M0 — protocol spike (½ day):** both sides stubbed; pi prints, weechat
   echoes; NDJSON + hello over the unix socket. ✅ done when typing in the
   buffer shows up as a pi notification and vice versa.
2. **M1 — output mirroring (1 day):** all pi→WeeChat message types; streaming
   deltas, tool lines, status/title updates; reconnect logic on pi side.
3. **M2 — input path (½ day):** `user_input` → `sendUserMessage`, `!s`/`!q`
   steer/followUp, echo-loop suppression via `event.source`.
4. **M3 — commands + polish (½ day):** `!new`/`!compact`/`!abort`/`!status`/
   `!model`, ping/pong, truncation rules, colors.
5. **M4 — packaging (½ day):** `pi install /path/to/pi-weechat` works; weechat
   script install instructions in README; publish to npm with
   `"pi-package"` keyword for the pi.dev gallery.

## 9. Repo layout

```
pi-weechat/
├── PLAN.md                  # this file
├── README.md                # install + usage
├── package.json             # pi package manifest
├── extensions/
│   └── weechat-bridge.ts    # pi extension (socket client + mirroring)
├── weechat/
│   └── pi_bridge.py         # WeeChat script (socket server + buffer)
└── test/
    └── protocol.test.mjs    # NDJSON codec + handshake unit tests
```

## 10. Open questions

- Multi-session: one pi process = one connection. If two pi instances run,
  v1 keeps "first client wins"; v2 could multiplex by session id in the hello.
  (Decide before M3.)
- WeeChat colors: hardcode a small palette vs. read `weechat.color` settings —
  start hardcoded.
