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
| `hello`          | `{protocol, name}`               | handshake                                      |
| `status`         | `{state, detail?}`               | state ∈ idle / thinking / tool:\<name\> / error; drives buffer title + status line |
| `assistant_line` | `{msgId, text}`                  | one **complete line** of assistant text. Pi accumulates the `message_update` deltas locally and flushes each line as soon as it is complete (WeeChat has no partial-line redraw, so raw token deltas are meaningless to it) |
| `thinking_line`  | `{msgId, text}`                  | one **complete line** of assistant *thinking* (reasoning models). Assembled from `thinking_*` deltas exactly like `assistant_line`; the WeeChat side renders it only when `pi_bridge.thinking = on` (`!think`) and drops it otherwise |
| `assistant_flush`| `{msgId}`                        | this assistant message is done; drop any partial tail state on the WeeChat side |
| `user_echo`      | `{text}`                         | user prompt as accepted by pi (mirror back if input originated in the pi terminal) |
| `tool_start`     | `{toolCallId, toolName, args}`   | from `tool_execution_start`                    |
| `tool_end`       | `{toolCallId, isError, output}`  | from `tool_execution_end`; `output` truncated at a whole-line boundary if > 8 KiB (… \\"N more characters truncated\\") |
| `session_info`   | `{name?, model?, cwd}`           | on `session_start` / `model_select` / `session_info_changed` |
| `error`          | `{code, message}`                | protocol or runtime error                      |
| `ping`           | `{ts}`                           | keepalive every 30 s (WeeChat answers with pong) |

### Message types (WeeChat → pi extension)

| type        | payload                          | meaning                                        |
|-------------|----------------------------------|------------------------------------------------|
| `hello`     | as above                         | handshake                                      |
| `user_input`| `{text, deliverAs?}`             | typed line; `deliverAs` ∈ undefined (normal), `"steer"` (`!s ...` prefix → mid-stream steering), `"followUp"` (`!q ...`). The pi side fills in `"followUp"` for plain input that arrives while a turn is running, so nothing is dropped |
| `command`   | `{name, arg?}`                   | name ∈ `new_session`, `compact`, `abort`, `status`, `model`; `arg` carries the optional argument (e.g. model id for `model`). `abort` runs `ctx.abort()` directly (safe from event handlers); the rest are routed through a registered extension command (`/weechat-ctl <name> [arg]`) sent with `expandPromptTemplates: true` so it executes immediately — even mid-stream |
| `pong`      | `{ts}`                           | keepalive response                             |

Buffer-local commands typed in the WeeChat buffer (all start with `!` so they
never reach the LLM):

- `!s <text>`  — steer (interrupt current stream, inject)
- `!q <text>`  — queue follow-up message
- `!new`       — new session (`ctx.newSession()`)
- `!compact`   — compaction (`ctx.compact()`)
- `!abort`     — abort current turn (`ctx.abort()`)
- `!model`     — list available models (scoped via `ctx.scopedModels` when scoping is configured, else the full catalogue via `ctx.modelRegistry.getAvailable()` — mirrors pi's `/model`); `!model <provider/id>` selects via `pi.setModel()`
- `!status`    — force a status refresh (session file, model)
- `!tools [full|summary|off]` — tool output verbosity in the buffer (`pi_bridge.tool_output`)
- `!think [on|off]`           — show/hide thinking lines (`pi_bridge.thinking`, default off)
- `!help` / `?`               — print this command list (answered locally, never reaches pi)

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
- **Colors:** built with `weechat.color(name)` (binary codes), NOT legacy
  `"color:xxx"` text tags — WeeChat 4.x prints those literally. Empty string
  = buffer default foreground. Colors use the user's *theme*: names from the
  `[color]` section of weechat.conf (changeable live with `/color`), resolved
  at render time; each has a palette fallback mirroring the 4.x theme default.
  Role → theme name: user input `chat_nick_self`, assistant text `chat`,
  tool lines `chat_prefix_network`, info/status `chat_value`, errors
  `chat_prefix_error`, success `chat_status_enabled`, hints/thinking 💭
  `chat_host`; the tool *output body* is a fixed palette color (`blue`,
  deliberately not theme-following so it stays distinct from the dim
  thinking lines — see `C_TOOL_OUT` in pi_bridge.py).
- **Rendering** (one `weechat.prnt(buf, text)` per line). Lines are printed
  WITHOUT a leading `\t\t`: that trick would suppress the timestamp in the
  terminal UI but zero out the stored line date, which relay clients
  (Glowing Bear over the relay websocket) would render as 01.01.1970.
  Real dates give proper HH:MM timestamps everywhere: 
  - `assistant_line`: print as-is (default color), one buffer line each.
  - `tool_start`: `⚙ tool_name <summary>` where `<summary>` is the main
    content of the args struct, per tool (`format_tool_args()`): bash→
    command, read/write→path (+content), edit→path + edit count,
    memory_write→target + content, memory_search→query, … unknown tools get
    compact `k=v` pairs. Values are flattened to one line and clipped at
    300 chars with an `…(+N)` marker.
  - `tool_end`: green ✔ / red ✘ prefix + blue indented output lines (distinct
    from the dim cyan 💭 thinking lines), filtered by the
    `pi_bridge.tool_output` option (`full` | `summary` | `off`; default
    `summary` = first 3 + last 3 lines with `… (N more lines)` in between).
- **Hook lifecycle:** every `hook_fd` handle (listen, client read, write) is
  stored and unhooked on disconnect — stale hooks make WeeChat poll closed fds
  ("Bad file descriptor used in hook_fd") and double-fire when fd numbers are
  reused by the next client.
  - `status`: update buffer `title` (`pi: (idle)`, `pi: (thinking…)`,
    `pi: (tool: bash)`, `pi: (disconnected — waiting for pi)`).
  - `session_info`: one cyan line with cwd + model + session name.
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
  | `message_update`      | `assistant_line` (deltas assembled locally; each completed line flushed immediately) + `assistant_flush` at message end |
  | `message_end`         | `assistant_end` / tool result text       |
  | `tool_execution_*`    | `tool_start` / `tool_end` (output truncated at 8 KiB, whole-line boundary) |
  | `agent_start`/`agent_settled` | `status{thinking}` / `status{idle}` |
  | `model_select`, `session_info_changed` | `session_info`          |
  | `input` (source ≠ "extension") | `user_echo` (mirror prompts typed in the pi terminal itself, so both surfaces stay in sync) |
- **Input handling:** on `user_input`, call
  `pi.sendUserMessage(text, { deliverAs })`. Plain input that arrives while a
  turn is streaming (`agent_start` without `agent_settled`) gets
  `deliverAs: "followUp"` — otherwise pi's `prompt()` throws "Agent is already
  processing" and the message would vanish with no error visible in WeeChat.
  Because injected messages arrive back at pi with
  `event.source === "extension"`, the `input` handler skips them for
  `user_echo` — no echo loops.
- **Commands:** on `command`, route to a registered extension command's handler
  (`pi.registerCommand("weechat-ctl", ...)` whose `ExtensionCommandContext` has
  `newSession` / `compact` / `abort`) via `sendUserMessage("/weechat-ctl …",
  { expandPromptTemplates: true })`. That flag is load-bearing: the default
  (`false`) skips extension-command dispatch entirely, so the slash text would
  be sent to the LLM as a literal prompt (and `followUp`-queued extension
  commands throw "cannot be queued"). With it on, commands execute immediately,
  even mid-stream.
- **Model selection:** `!model <provider/id>` is handled by the weechat-ctl
  command: the id is matched against `ctx.model` + `ctx.scopedModels` (or the full catalogue via
  `ctx.modelRegistry.getAvailable()` when no scoping is configured) and applied
  with `pi.setModel(model)` (`model_select` re-sends `session_info`).
- **Keepalive:** 30 s ping; drop and reconnect if no pong for 90 s.

### Mode note
The extension works in TUI mode (pi visible in a terminal, WeeChat mirrors the
same session — two windows on one agent) and is most useful with headless
usage (`pi -p` per message or RPC). Guard any `ctx.ui.*` calls behind
`ctx.hasUI`.

## 5. Ordering & backpressure details

- All pi→WeeChat messages for a single assistant turn are sent in event order;
  WeeChat renders in arrival order, so no sequence numbers needed (v1).
- Streaming text is flushed per completed line on the pi side: line
  boundaries are natural flush points, so no timer batching is needed and
  WeeChat's redrawing stays cheap.
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
| `pi_bridge.tool_output` | WeeChat option (`/set pi_bridge.tool_output full\|summary\|off`, or `!tools <mode>` in the buffer) | `summary` |
| socket path          | pi extension: `PI_WEECHAT_SOCK` env or `.pi/settings.json` extension setting; weechat side: same env / XDG dirs | `$XDG_RUNTIME_DIR/pi-weechat.sock` |

## 8. Milestones

1. **M0 — protocol spike:** both sides stubbed; pi prints, weechat echoes;
   NDJSON + hello over the unix socket. ✅
2. **M1 — output mirroring:** all pi→WeeChat message types; line-based
   streaming, tool lines, status/title updates; reconnect logic on pi side.
   ✅
3. **M2 — input path:** `user_input` → `sendUserMessage`, `!s`/`!q`
   steer/followUp, echo of terminal-typed prompts via `event.source`. ✅
4. **M3 — commands + polish:** `!new`/`!compact`/`!abort`/`!status`/
   `!model`, ping/pong, truncation rules, colors. ✅
5. **M4 — packaging:** `pi install /path/to/pi-weechat` works; weechat script
   install instructions in README. ✅ (npm publish for the pi.dev gallery:
   optional follow-up.)

## 9. Repo layout

```
pi-weechat/
├── PLAN.md                  # this file
├── README.md                # install + usage
├── package.json             # pi package manifest ("pi-package" keyword)
├── tsconfig.json            # typecheck only (noEmit)
├── lib/
│   └── codec.mjs            # shared NDJSON codec + protocol constants
├── extensions/
│   └── weechat-bridge.ts    # pi extension (socket client + mirroring)
├── weechat/
│   └── pi_bridge.py         # WeeChat script (socket server + buffer)
└── test/
    ├── protocol.test.mjs    # codec unit tests + extension end-to-end (mock peer)
    ├── smoke_weechat.py     # weechat script smoke test (stub weechat module, stdlib only)
    ├── py_driver.py         # stdin/stdout driver used by the integration test
    └── integration.test.mjs # REAL extension ↔ REAL weechat script over a live socket
```

## 10. Open questions

- Multi-session: one pi process = one connection. If two pi instances run,
  v1 keeps "first client wins"; v2 could multiplex by session id in the hello.
  (Decide before M3.)
- WeeChat colors: hardcode a small palette vs. read `weechat.color` settings —
  start hardcoded.
