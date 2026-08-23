# pi-weechat — Mirror pi agent I/O through a WeeChat buffer

Goal: chat with a [pi](https://pi.dev) coding agent from inside WeeChat. A pi
**package** (extension) and a **WeeChat Python script** communicate over a
local Unix domain socket — or, since protocol 2, over TCP when pi runs on a
different machine (opt-in, shared-secret auth, see §6). Everything pi says —
assistant streaming text, tool calls, results, status changes — is mirrored
into a dedicated WeeChat buffer, and anything typed in that buffer is sent to
pi as user input.

## 1. Architecture

```
┌───────────────────────────┐          ┌──────────────────────────────┐
│  WeeChat (machine A)      │          │  pi (machine B, or same box) │
│  ┌───────────────────────┐ │  Unix   │  ┌──────────────────────────┐ │
│  │ buffer "pi"           │ │  domain │  │ extension                 │ │
│  │  pi_bridge.py         │◄┼─socket──┼►│  weechat-bridge.ts        │ │
│  │  · SERVER: unix always│ │  (NDJSON│  │  · CLIENT: dials unix or  │ │
│  │    + TCP when opt-in  │ │   JSON) │  │    tcp (PI_WEECHAT_URL)   │ │
│  │  · hook_fd read/write │ │         │  │  · node:net + backoff     │ │
│  │  · buffer input_cb    │ │         │  │    + challenge→proof auth │ │
│  └──────────┬────────────┘ │         │  └──────────┬───────────────┘ │
│             │              │         │             │ pi.on(...)       │
│   user types line          │         │   message_start /              │
│   Enter → input callback   │         │   message_update (stream)      │
└───────────────────────────┘         └──────────────────────────────┘
```

- **WeeChat script = socket server.** WeeChat is the long-lived process; pi
  sessions start/stop/reload all the time. The script `bind()`s the Unix
  socket at load, and — since protocol 2 — also a TCP listener when
  `pi_bridge.tcp_listen` is set (live rebind, no reload). Across BOTH
  transports it admits exactly one authenticated client at a time (extra
  clients are rejected with a short error message; in-flight handshakes are
  capped and time out — §6).
- **Pi extension = socket client.** Dials the endpoint from
  env / config file (§2 order) on `session_start`,
  retries with exponential backoff (1s → 30s cap) while WeeChat/pi order is
  different, and transparently reconnects if the peer dies. On
  `session_shutdown` it closes the socket cleanly.

Why a Unix socket locally: same machine, no port collisions, no network
exposure. Why TCP is fine remotely: both sides already have first-class
fd APIs (`hook_fd` on WeeChat, `node:net` on pi), and the shared-secret
challenge (below) keeps a captured wire useless for impersonation —
confidentiality is the VPN's job (§6).

## 2. Socket & protocol

- **Endpoints (pi dials):** first match wins — `$PI_WEECHAT_URL`, then
  `"url"` in the pi-side config file, then the deprecated `$PI_WEECHAT_SOCK`,
  then `$XDG_RUNTIME_DIR/pi-weechat.sock`, then
  `~/.local/state/pi-weechat/pi-weechat.sock`. URL syntax: `tcp://host:port`,
  `unix://<path>` (or `unix:<path>`), schemeless `host:port` (numeric port) ⇒
  TCP, anything else ⇒ socket path; unknown scheme ⇒ error (extension point).
  **pi-side config file** (`lib/pi-config.mjs`):
  `<agent dir>/pi-weechat.json` — agent dir follows `$PI_CODING_AGENT_DIR`
  (default `~/.pi/agent`, next to pi's own settings.json); keys `url`,
  `token`, `debugLog` mirror `PI_WEECHAT_URL` / `PI_WEECHAT_TOKEN` /
  `PI_BRIDGE_DEBUG`; env vars win over the file; re-read at every
  `session_start` (so `/reload` picks up edits); a broken file degrades to
  env/default behavior + red `config_error` line in the buffer. On the
  WeeChat side the Unix
  path uses the same env/XDG logic; the TCP bind address is the
  `pi_bridge.tcp_listen` plugin option. Unix socket created with `0700`.
- **Framing:** newline-delimited JSON (NDJSON), UTF-8. Max message size guard
  of 1 MiB (larger tool outputs are chunked by the sender, see §5).
- **Protocol version: 3** (v3 adds the interactive UI channel, `ui_request`
  / `ui_response` + `!pick`, and the built-in `cd` command; both sides
  upgrade together — a mismatch still yields `protocol_mismatch`).
- **Handshake (gated, both transports):** the server ignores everything from
  a client until a valid `hello` arrives (no prompt-injection before auth).
  - *No token configured:* client sends
    `{"type":"hello","protocol":3,"name":"pi-weechat-bridge"}` first, then its
    pending queue; the server answers with its own hello only after
    validating the protocol.
  - *Token configured (shared secret — the token is never sent):* the server
    first sends `{"type":"challenge","nonce":<256-bit random hex>}`; the
    client answers with the hello carrying `"proof": hex(HMAC-SHA256(key=token,
    msg=nonce))`. Constant-time compare; wrong/missing proof →
    `{"type":"error","code":"auth_failed"}` + drop. Fresh nonce per connection
    ⇒ captured proofs can't be replayed. The server's hello is withheld until
    the client is validated, so unauthenticated peers see no protocol data.

| type (new in v2) | payload | direction | meaning |
|---|---|---|---|
| `challenge` | `{nonce}` | server → client | shared-secret challenge (only when a token is set) |
| `hello.proof` | `{...}` | client | `hex(HMAC-SHA256(token, nonce))`; only when a token is set |

| type (new in v3) | payload | direction | meaning |
|---|---|---|---|
| `ui_request` | select: `{id, method:"select", title, options[], multiple?}` · input: `{id, method:"input", title, placeholder?}` | pi → WeeChat | interactive prompt rendered in the buffer ("?" + numbered options or a free-text hint); answered with `!pick`. `options` entries are strings or `{label, description?}`; `multiple` allows comma-list answers. `id` is a per-connection monotonic counter |
| `ui_response` | answer: `{id, value}` (string; array when multiple) · cancel: `{id, cancelled:true}` | WeeChat → pi | answer to `ui_request`; `value` is the picked option text(s) or the entered free text. Stale/unknown ids are ignored on the pi side |

Only one prompt is tracked per connection: a new `ui_request` supersedes the
pending one (the WeeChat side immediately sends a cancelled `ui_response` for
the old id). On disconnect, all pending prompts resolve as cancelled.

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
| `command`   | `{name, arg?}`                   | name ∈ `new_session`, `compact`, `abort`, `status`, `model`, `cd`; `arg` carries the optional argument (e.g. model id for `model`, path for `cd`). `abort` runs `ctx.abort()` directly (safe from event handlers); the rest are routed through a registered extension command (`/weechat-ctl <name> [arg]`) sent with `expandPromptTemplates: true` so it executes immediately — even mid-stream. `cd` is handled built-in by the bridge (no LLM round trip): exact existing dir ⇒ immediate session switch; otherwise fuzzy-similar dirs + a “➕ create … as new project” option are offered as a `ui_request`, answered with `!pick` |
| `pong`      | `{ts}`                           | keepalive response                             |

Buffer-local commands typed in the WeeChat buffer (all start with `!` so they
never reach the LLM):

- `!s <text>`  — steer (interrupt current stream, inject)
- `!q <text>`  — queue follow-up message
- `!new`       — new session (`ctx.newSession()`)
- `!compact`   — compaction (`ctx.compact()`)
- `!abort`     — abort current turn (`ctx.abort()`)
- `!model`     — list available models (scoped via `ctx.scopedModels` when scoping is configured, else the full catalogue via `ctx.modelRegistry.getAvailable()` — mirrors pi's `/model`); `!model <provider/id>` selects via `pi.setModel()`
- `!cd <path>` — switch pi to a different project directory (new session in that cwd). Exact existing dir ⇒ switches immediately; otherwise fuzzy-similar dirs are listed as a `ui_request` select (always including “➕ create <path> as new project”) — answer with `!pick`. Ported from the standalone `/cd` extension so it works over the bridge
- `!pick …`    — answer the pending `ui_request` prompt (“?” in the buffer): `!pick <n>` (or `!pick 1,3` when multiple), exact option text, free text for input prompts, or `!pick cancel`. With no pending prompt it is rejected locally
- `!status`    — force a status refresh (session file, model)
- `!tools [full|summary|off]` — tool output verbosity in the buffer (`pi_bridge.tool_output`)
- `!think [on|off]`           — show/hide thinking lines (`pi_bridge.thinking`, default off)
- `!highlight [on|off]`       — syntax-highlight fenced code blocks (`pi_bridge.highlight`, default on)
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

- **Server sockets:** the Unix socket is bound at module load; the optional
  TCP listener (`pi_bridge.tcp_listen`) starts/stops on option change via
  `hook_config` (live rebind — the `urlserver.py` pattern: blocking listen
  fd, one plain `accept()` per `hook_fd` event, `SO_REUSEADDR`, `listen(5)`,
  status line from `getsockname()`). Both funnels share one accept path:
  peer-IP gates first (lockout, `allowed_ips`), then one client at a time —
  an extra connected client gets one `error{code: "client_already_connected"}`;
  up to 3 in-flight handshakes are held, then closed silently. Client fds are
  non-blocking in both transports, with the shared `rxbuff` + write-hook
  backpressure model.
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
  thinking lines — see `C_TOOL_OUT` in pi_bridge.py); fenced-code tokens
  (keywords/strings/comments/numbers/…) use a fixed 7-color palette
  (see `HL_TOKENS`).
- **Rendering** (role-based: `weechat.buffer_printf(buf, tags, "%s", text)` for
  pi/user lines, plain `weechat.prnt(buf, text)` for system lines). Lines are printed
  WITHOUT a leading `\t\t`: that trick would suppress the timestamp in the
  terminal UI but zero out the stored line date, which relay clients
  (Glowing Bear over the relay websocket) would render as 01.01.1970.
  Real dates give proper HH:MM timestamps everywhere: 
  - **Prefix column (nick):** pi-originated lines (assistant prose/fences,
    thinking 💭, tool ⚙/✔ lines, tool output body) use tag `nick!pi` —
    rendered under the literal nick `pi` in the theme's nick color. User
    lines (typed buffer input, `!s`/`!q` echoes, `user_echo` from the pi
    terminal) use tag `nick!me` — rendered under the buffer localvar
    `nick`, set at buffer creation to the first non-empty entry of
    `irc.server_default.nicks` (comma-separated; the user's own IRC nick)
    and re-applied live on change via `hook_config` (no reload). If that
    option is empty, missing, or the IRC plugin is not loaded, the localvar
    is cleared and user lines fall back to the legacy `> ` marker via
    `prnt`. System lines (session info, connection state, command status,
    errors) stay prefix-less (`prnt`). Message body colors are unchanged —
    only the prefix column moves.
  - `assistant_line`: prose prints as-is (default color), one buffer line
    each. Fenced code blocks are tracked per message (`msgId`): fence lines
    print dim, the body is indented two spaces and syntax-highlighted for
    known languages (bash/sh, rust, css, html/xml/svg, php, python, json,
    yaml — `HL_LANGS`/`HL_ALIAS` in pi_bridge.py) by a small line tokenizer
    (ordered regex alternation; block-comment state carried per fence).
    Toggled by `pi_bridge.highlight` (`!highlight`, default on); unknown
    languages render plain but stay indented.
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

- **Local (Unix):** file mode `0700` in the user's private state dir. When a
  token is configured it is enforced on the Unix transport too — no weaker
  path on localhost. Handshake gating applies to both transports.
- **Remote (TCP) — shared-secret model:** the pre-shared token authenticates
  but does NOT encrypt (cleartext NDJSON). The challenge-response design
  means the token never crosses the wire and captured handshakes can't be
  replayed (fresh nonce), but an **active MITM can still relay** the session
  ⇒ run remote setups inside Tailscale/a VPN. Token guidance: ≥ 128 bits of
  entropy (`openssl rand -hex 16`), never a password/reused secret; stored
  encrypted in `sec.conf` (`${sec.data.…}` reference) on the WeeChat side and
  in `PI_WEECHAT_TOKEN` on the pi side — never literally in `weechat.conf`,
  never in the URL.
- **Server-side hardening** (module constants in `pi_bridge.py`): constant-time
  HMAC compare · 10 s auth deadline (silent drop) · ≤ 3 unauthenticated
  connections · per-IP failure lockout (> 5 failures / 60 s ⇒ 600 s silent
  ignore — no knocking oracle; behind shared NAT the NAT's IP is what gets
  locked) · `pi_bridge.allowed_ips` peer-IP regex gate at accept (silent) ·
  per-event read cap 256 KiB (bursts can't stall WeeChat's UI) · `user_input`
  rate limit 5/s (bounds LLM spend if a token leaks) · server hello withheld
  until validation (no service fingerprint for scanners).
- WeeChat side never executes anything from the socket; pi side treats
  incoming JSON as data. The pi extension is untrusted-code-by-design (all pi
  extensions are) — review before installing.

## 7. Configuration

| setting              | where                          | default                             |
|----------------------|--------------------------------|-------------------------------------|
| `pi_bridge.tcp_listen` | WeeChat option (`/set pi_bridge.tcp_listen host:port`; `0.0.0.0:port` for LAN/Tailscale) — live rebind, no reload | empty (off) |
| `pi_bridge.token` | WeeChat option; recommended value `${sec.data.pi_weechat_token}` (`/secure set pi_weechat_token …`) | empty (no enforcement) |
| `pi_bridge.allowed_ips` | WeeChat option; regex of peer IPs accepted on the TCP listener (empty = allow all) | empty |
| `pi_bridge.tool_output` | WeeChat option (`/set pi_bridge.tool_output full\|summary\|off`, or `!tools <mode>` in the buffer) | `summary` |
| `pi_bridge.highlight` | WeeChat option (`/set pi_bridge.highlight on\|off`, or `!highlight <mode>` in the buffer) — syntax highlighting of fenced code blocks in assistant messages | `on` |
| `irc.server_default.nicks` | WeeChat IRC option (read-only for the bridge): first non-empty entry = the user's nick for the `nick!me` prefix column; re-applied live via `hook_config`, no reload | the IRC plugin's `nicks` default |
| `PI_WEECHAT_URL`     | pi extension env: `tcp://host:port` / `unix://<path>` / `host:port` / path | unset ⇒ config-file `url` ⇒ `PI_WEECHAT_SOCK` (deprecated) ⇒ default unix path |
| `PI_WEECHAT_TOKEN`   | pi extension env: shared secret for the challenge (never in the URL) | unset (anonymous) ⇒ config-file `token` |
| `pi-weechat.json`    | pi-side config file `<agent dir>/pi-weechat.json` (agent dir = `$PI_CODING_AGENT_DIR`, default `~/.pi/agent`); keys `url` / `token` / `debugLog` mirror the env vars above — **env always wins over the file**; re-read at each `session_start`; broken file ⇒ env/default behavior + red `config_error` line | missing (fine) |
| socket path (unix)   | weechat side: same env / XDG dirs | `$XDG_RUNTIME_DIR/pi-weechat.sock` |

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
6. **M5 — remote (TCP):** opt-in TCP listener with shared-secret auth
   (challenge → HMAC proof, token never on the wire), handshake gating,
   `PI_WEECHAT_URL` endpoint (deprecating `PI_WEECHAT_SOCK`), server hardening
   (auth deadline, pending cap, IP lockout, `allowed_ips`, read cap, input
   rate limit), live rebind via `hook_config`. ✅
7. **M6 — interactive UI channel (protocol v3):** `ui_request`/`ui_response`
   + `!pick` in the buffer; built-in `!cd <path>` project switching (fuzzy
   search + create option, ported from the standalone `/cd` extension);
   `globalThis.__pi_weechat_bridge__` handle so other extensions (e.g.
   ask_user-style tools) can offer their prompts in the buffer via
   `isConnected()/select()/input()`. ✅ (ask-user relay itself: roadmap —
   no public hook in pi-ask-user today, so the bridge only exposes the
   channel.)

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

- Multi-session: one pi process = one connection, still — now spanning both
  transports (a TCP client and a Unix client are mutually exclusive). If two
  pi instances run, the current build keeps "first authenticated client
  wins"; a future version could multiplex by session id in the hello.
- WeeChat colors: hardcode a small palette vs. read `weechat.color` settings —
  start hardcoded.
