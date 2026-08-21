# -*- coding: utf-8 -*-
###
# pi_bridge.py — mirror a pi coding agent session through a WeeChat buffer.
#
# Creates a "pi" buffer that acts as both display and input for a pi session.
# This script is the Unix-socket SERVER; the pi extension (see
# extensions/weechat-bridge.ts) connects to it. Wire format: NDJSON, see
# PLAN.md §2.
#
# Install: copy into ~/.local/share/weechat/python/ and start WeeChat, or
#   /python load pi_bridge
# Reload after changes: /python reload pi_bridge
#
# Socket path: $PI_WEECHAT_SOCK, else $XDG_RUNTIME_DIR/pi-weechat.sock,
# else ~/.local/state/pi-weechat/pi-weechat.sock (same logic as the pi side).
###

import errno
import json
import os
import socket
import time

try:
    import weechat
except ImportError:  # allows importing this module outside WeeChat (tests)
    weechat = None

# Opt-in wire debug log — same convention as the pi extension:
# PI_BRIDGE_DEBUG=<path>, or the marker file $XDG_RUNTIME_DIR/pi-weechat.debug.
_DBG_PATH = os.environ.get("PI_BRIDGE_DEBUG")
if not _DBG_PATH and os.environ.get("XDG_RUNTIME_DIR"):
    _candidate = os.path.join(os.environ["XDG_RUNTIME_DIR"], "pi-weechat.debug")
    if os.path.exists(_candidate):
        _DBG_PATH = _candidate
_DBG_MAX = 1_000_000
_T0 = time.time()


def dbg(msg):
    if not _DBG_PATH:
        return
    try:
        try:
            if os.path.getsize(_DBG_PATH) > _DBG_MAX:
                with open(_DBG_PATH, "rb") as f:
                    data = f.read()
                with open(_DBG_PATH, "wb") as f:
                    f.write(b"\n... (log rotated) ...\n" + data[-400_000:])
        except OSError:
            pass
        with open(_DBG_PATH, "a") as f:
            f.write("[wc %.3f] %s\n" % (time.time() - _T0, msg))
    except Exception:
        pass  # debug must never break the bridge

PROTOCOL = 1
MAX_LINE = 1024 * 1024  # must match MAX_LINE_BYTES in lib/codec.mjs


def _color(name):
    """Binary WeeChat color code for `name` ("" outside WeeChat or on error).

    WeeChat 4.x does NOT interpret legacy text tags like "color:cyan" in
    printed messages — they would be displayed literally. weechat.color()
    returns the internal binary code that the display layer decodes.
    """
    if weechat is None:
        return ""
    try:
        return weechat.color(name) or ""
    except Exception:
        return ""


def _theme(theme_name, fallback):
    """Binary color code for a *theme* color name (weechat.conf [color]).

    `theme_name` is one of the colors defined in the [color] section of
    weechat.conf — the user's theme, changeable live with /color. WeeChat's
    weechat.color() accepts those names directly and returns a reference the
    display layer resolves at render time, so the buffer follows whatever
    palette the user configured. If the name is not defined (other WeeChat
    versions, minimal configs), fall back to a fixed palette color; outside
    WeeChat everything resolves to "".
    """
    return _color(theme_name) or _color(fallback)


# Colors: (theme name from weechat.conf [color], palette fallback).
# Fallbacks mirror the 4.x default theme values for each role.
C_USER = _theme("chat_nick_self", "white")          # user's own input lines
C_PI = _theme("chat", "")                           # assistant text (default fg)
C_TOOL = _theme("chat_prefix_network", "magenta")   # tool activity lines
C_STATUS = _theme("chat_value", "cyan")             # info lines (session, modes)
C_ERR = _theme("chat_prefix_error", "yellow")       # errors
C_OK = _theme("chat_status_enabled", "green")       # successes
C_DIM = _theme("chat_host", "cyan")                  # hints / 💭 thinking lines
# Tool *output* body: own color, deliberately a fixed palette color (no
# canonical WeeChat [color] slot for this role; theme-following risks
# collapsing back onto chat_host/cyan and looking identical to thinking).
C_TOOL_OUT = _color("blue")
R = _color("reset")

# Per-tool summary of tool_start args: the "main content" of each tool's
# argument struct, in display order (see format_tool_args).
TOOL_ARG_KEYS = {
    "bash": ("command",),
    "read": ("path",),
    "write": ("path", "content"),
    "edit": ("path",),  # + edit count, handled in format_tool_args
    "memory_write": ("target", "content"),
    "memory_read": ("target",),
    "memory_search": ("query",),
    "memory_forget": ("match",),
    "scratchpad": ("action", "text"),
    "todo": ("action", "subject"),
    "web_search": ("query",),
    "web_fetch": ("url",),
}
ARG_VALUE_LIMIT = 300  # clip a single arg value beyond this many characters


def _clip(value, limit=ARG_VALUE_LIMIT):
    """Single-line, length-limited rendering of one arg value."""
    s = " ".join(str(value).split())  # newlines/indent → single spaces
    if len(s) > limit:
        return s[:limit].rstrip() + "…(+%d)" % (len(s) - limit)
    return s


# Tool output display mode (pi_bridge.tool_output option)
TOOL_OUTPUT_MODES = ("full", "summary", "off")
DEFAULT_TOOL_OUTPUT = "summary"

# Thinking visibility (pi_bridge.thinking option)
THINKING_MODES = ("on", "off")
DEFAULT_THINKING = "off"

HELP_TEXT = (
    "!s <text> steer current turn · !q <text> queue follow-up\n"
    "!new new session · !compact compact context · !abort abort current turn\n"
    "!status refresh session/model info · !model [provider/id] list or set model\n"
    "!tools [full|summary|off] tool output verbosity (default: summary)\n"
    "!think [on|off] show/hide thinking lines (default: off)\n"
    "anything else is sent to pi as a normal message"
)


def default_socket_path():
    env = os.environ.get("PI_WEECHAT_SOCK")
    if env:
        return env
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "pi-weechat.sock")
    state = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return os.path.join(state, "pi-weechat", "pi-weechat.sock")


class Bridge(object):
    def __init__(self):
        self.buffer = None
        self.alive = False            # buffer still open?
        self.sock_path = default_socket_path()
        self.listen_sock = None
        self.client = None            # accepted client socket (or None)
        self.listen_hook = None       # hook_fd handle for the listen socket
        self.client_hook = None       # hook_fd handle for the client read side
        self.write_hook = None        # hook_fd handle for the write side
        self.rxbuff = b""
        self.outq = b""
        self.state = "waiting"        # waiting | idle | thinking | tool:<name>

    # ---------------------------------------------------------------- buffer

    def make_buffer(self):
        self.buffer = weechat.buffer_new("pi", "pi_input_cb", "", "pi_close_cb", "")
        weechat.buffer_set(self.buffer, "title", "pi: (waiting for pi)")
        weechat.buffer_set(self.buffer, "localvar_set_no_log", "1")
        self.alive = True
        self._print(C_STATUS + "pi bridge ready — socket %s%s" % (self.sock_path, R))
        self._print(C_DIM + "type a line to send it to pi; !help lists commands%s" % R)

    def _print(self, text):
        """Print one line, stamped with the current time.

        Lines are printed WITHOUT a leading "\t\t" prefix: that trick
        suppresses the timestamp in the terminal UI, but it also zeroes the
        line's stored date — relay clients (e.g. Glowing Bear over the relay
        websocket) would then show 01.01.1970 or no useful time. With a real
        date, both the TUI and relay clients show proper HH:MM timestamps.
        """
        if self.alive and self.buffer:
            weechat.prnt(self.buffer, text)

    def set_state(self, state, detail=None):
        self.state = state
        titles = {
            "waiting": "pi: (disconnected — waiting for pi)",
            "idle": "pi: (idle)",
            "thinking": "pi: (thinking…)",
        }
        if state in titles:
            title = titles[state]
            if detail and state == "idle":
                title += " — " + detail
        elif state.startswith("tool:"):
            title = "pi: (tool: %s)" % state[5:]
        else:
            title = None  # unknown state: keep current title
        if self.alive and self.buffer and title:
            weechat.buffer_set(self.buffer, "title", title)

    # ---------------------------------------------------------------- socket

    def make_server(self):
        path = self.sock_path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        try:
            os.unlink(path)  # stale socket from a previous run
        except OSError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        os.chmod(path, 0o700)
        sock.listen(1)
        sock.setblocking(False)
        self.listen_sock = sock
        self.listen_hook = weechat.hook_fd(sock.fileno(), 1, 0, 0, "pi_listen_cb", "")
        return True

    def accept_pending(self):
        """Called on read event of the listen socket."""
        while True:
            try:
                client, _addr = self.listen_sock.accept()
            except BlockingIOError:
                return
            except OSError:
                self._print(C_ERR + "pi bridge: accept error" + R)
                return
            if self.client is not None:
                # one client at a time (v1)
                try:
                    client.sendall((json.dumps({"type": "error", "code":
                        "client_already_connected"}) + "\n").encode())
                except OSError:
                    pass
                dbg("accept: rejected second client")
                client.close()
                continue
            client.setblocking(False)
            dbg("accept: new client fd=%s" % client.fileno())
            self.client = client
            self.rxbuff = b""
            self.outq = b""
            self.client_hook = weechat.hook_fd(client.fileno(), 1, 0, 0, "pi_client_cb", "")
            self._send({"type": "hello", "protocol": PROTOCOL,
                        "name": "weechat-pi-bridge"})
            self.set_state("idle")
            self._print(C_OK + "— pi connected —" + R)

    def client_event(self, fd):
        """Read events (and HUP) for the accepted client."""
        if self.client is None:
            return
        if fd is not None and int(fd) < 0:
            # hooked fd gone (e.g. we closed it)
            self.drop_client()
            return
        try:
            data = self.client.recv(65536)
        except BlockingIOError:
            return
        except OSError as e:
            dbg("recv error: %s" % e)
            data = b""
        if not data:  # peer closed (recv == 0) or error → disconnect
            dbg("recv 0 bytes — peer closed, dropping client")
            self.drop_client()
            return
        dbg("recv %d bytes" % len(data))
        self.rxbuff += data
        while b"\n" in self.rxbuff:
            line, self.rxbuff = self.rxbuff.split(b"\n", 1)
            if len(line) > MAX_LINE:
                self._print(C_ERR + "pi bridge: dropped oversized message" + R)
                continue
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                self._print(C_ERR + "pi bridge: bad JSON line ignored" + R)
                continue
            if isinstance(msg, dict):
                try:
                    self.dispatch(msg)
                except Exception as e:  # never let one bad message kill the loop
                    self._print(C_ERR + "pi bridge: dispatch error: %s%s" % (e, R))
        if len(self.rxbuff) > MAX_LINE:
            self.rxbuff = b""

    def drop_client(self):
        dbg("drop_client")
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass
            self.client = None
        # unhook BOTH fd hooks for the old client; leaving them behind makes
        # WeeChat poll a closed fd ("Bad file descriptor used in hook_fd")
        # and re-fire stale callbacks if a new client reuses the fd number.
        if self.client_hook:
            weechat.unhook(self.client_hook)
            self.client_hook = None
        if self.write_hook:
            weechat.unhook(self.write_hook)
            self.write_hook = None
        self.rxbuff = b""
        self.outq = b""
        self.set_state("waiting")
        self._print(C_DIM + "— pi disconnected —" + R)

    # ------------------------------------------------------------- sending

    def _send(self, obj):
        if self.client is None:
            dbg("_send %s DROPPED (no client)" % obj.get("type"))
            return
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        self.outq += line
        dbg(">> send %s (%d bytes, outq=%d)" % (obj.get("type"), len(line), len(self.outq)))
        # Flush synchronously: do not rely on the write-readiness hook to
        # fire — if it ever doesn't, outbound messages (user input, pongs)
        # would sit in outq forever. The hook is kept only as a backpressure
        # fallback for the rare case the socket buffer is full.
        self._try_flush()

    def _try_flush(self):
        while self.outq:
            if self.client is None:
                return
            try:
                n = self.client.send(self.outq)
            except BlockingIOError:
                dbg("flush: backpressure (outq=%d), waiting for write hook" % len(self.outq))
                if self.write_hook is None:
                    self.write_hook = weechat.hook_fd(
                        self.client.fileno(), 0, 1, 0, "pi_write_cb", "")
                return
            except OSError as e:
                dbg("flush: send error %s — dropping client" % e)
                self.drop_client()
                return
            self.outq = self.outq[n:]
        dbg("flush: outq empty")
        if self.write_hook:
            weechat.unhook(self.write_hook)
            self.write_hook = None

    def flush_outq(self, fd):
        # write-readiness event: only matters after a backpressure pause
        dbg("write_cb fired (outq=%d)" % len(self.outq))
        self._try_flush()

    # ---------------------------------------------------------- dispatching

    def tool_output_mode(self):
        """pi_bridge.tool_output option: full | summary | off."""
        return self._plugin_option("tool_output", TOOL_OUTPUT_MODES, DEFAULT_TOOL_OUTPUT)

    def thinking_enabled(self):
        """pi_bridge.thinking option: on | off."""
        return self._plugin_option("thinking", THINKING_MODES, DEFAULT_THINKING) == "on"

    def _plugin_option(self, name, modes, default):
        if weechat is None:
            return default
        try:
            v = (weechat.config_get_plugin(name) or "").strip().lower()
        except Exception:
            v = ""
        return v if v in modes else default

    def dispatch(self, msg):
        t = msg.get("type")
        if t == "hello":
            if int(msg.get("protocol", 0)) != PROTOCOL:
                self._send({"type": "error", "code": "protocol_mismatch"})
                self._print(C_ERR + "pi bridge: protocol mismatch" + R)
                self.drop_client()
            return
        if t == "ping":
            self._send({"type": "pong", "ts": msg.get("ts")})
            return
        if t == "status":
            state = msg.get("state", "idle")
            self.set_state(state)
            if state == "idle" and self.state != "waiting":
                pass  # title already updated; no line needed on settle
            return
        if t == "session_info":
            bits = []
            if msg.get("cwd"):
                bits.append(msg["cwd"])
            if msg.get("model"):
                bits.append(msg["model"])
            if msg.get("name"):
                bits.append("“%s”" % msg["name"])
            self._print(C_STATUS + "session: %s%s" % (" ".join(bits) or "(unnamed)", R))
            return
        if t == "user_echo":
            text = msg.get("text", "")
            for line in str(text).splitlines() or [""]:
                self._print(C_USER + "> " + R + line)
            return
        if t == "assistant_line":
            text = msg.get("text", "")
            self._print(C_PI + text + R)
            return
        if t == "thinking_line":
            if not self.thinking_enabled():
                return  # hidden; the line is dropped entirely
            self._print(C_DIM + "  💭 " + msg.get("text", "") + R)
            return
        if t == "assistant_flush":
            return  # lines already complete; nothing to render
        if t == "tool_start":
            name = msg.get("toolName", "?")
            summary = format_tool_args(name, msg.get("args") or {})
            self._print(C_TOOL + "⚙ " + name + C_DIM +
                        (" " + summary if summary else "") + R)
            return
        if t == "tool_end":
            ok = not msg.get("isError")
            color = C_OK if ok else C_ERR
            self._print(color + ("✔ " if ok else "✘ ") +
                        (msg.get("toolName", "tool") or "tool") + R)
            for line in self._tool_output_lines(msg.get("output")):
                self._print(C_TOOL_OUT + "  " + line + R)
            return
        if t == "error":
            self._print(C_ERR + "pi bridge: %s: %s%s" % (
                msg.get("code", "?"), msg.get("message", ""), R))
            return
        # unknown type: ignore (forward-compat)

    def _tool_output_lines(self, output):
        """Apply the pi_bridge.tool_output mode to a tool result."""
        mode = self.tool_output_mode()
        lines = str(output or "").splitlines()
        if mode == "off":
            return []
        if mode == "full":
            return lines
        # summary: first 3 + last 3 lines, elide the middle (smart-filter style)
        if len(lines) > 6:
            return (lines[:3]
                    + ["… (%d more lines)" % (len(lines) - 6)]
                    + lines[-3:])
        return lines

    # ---------------------------------------------------------- user input

    def on_input(self, line):
        if self.client is None:
            self._print(C_ERR + "not connected to pi (see buffer title)" + R)
            return
        line = line.strip()
        if not line:
            return
        # buffer-local commands (handled here, never reach pi)
        if line in ("!help", "?"):
            for h in HELP_TEXT.splitlines():
                self._print(C_STATUS + h + R)
            return
        if line == "!tools":
            self._print(C_STATUS + "tool output mode: %s (full | summary | off)%s"
                        % (self.tool_output_mode(), R))
            return
        if line.startswith("!tools "):
            arg = line[7:].strip().lower()
            if arg in TOOL_OUTPUT_MODES:
                self._set_plugin_option("tool_output", arg)
                self._print(C_STATUS + "tool output mode: %s%s" % (arg, R))
            else:
                self._print(C_ERR + "unknown tool output mode: %s (full | summary | off)%s"
                            % (arg, R))
            return
        if line == "!think":
            self._print(C_STATUS + "thinking: %s (!think on|off)%s" % (
                "on" if self.thinking_enabled() else "off", R))
            return
        if line.startswith("!think "):
            arg = line[7:].strip().lower()
            if arg in THINKING_MODES:
                self._set_plugin_option("thinking", arg)
                self._print(C_STATUS + "thinking: %s%s" % (arg, R))
            else:
                self._print(C_ERR + "unknown thinking mode: %s (on | off)%s" % (arg, R))
            return
        # buffer-local control commands → protocol 'command' messages
        command_map = {
            "!new": "new_session",
            "!compact": "compact",
            "!abort": "abort",
            "!status": "status",
        }
        if line in command_map:
            self._send({"type": "command", "name": command_map[line]})
        elif line == "!model":
            self._send({"type": "command", "name": "model"})
        elif line.startswith("!model "):
            # pi's setModel via the weechat-ctl extension command
            self._send({"type": "command", "name": "model",
                        "arg": line[7:].strip()})
        elif line.startswith("!s "):
            self._send({"type": "user_input", "text": line[3:], "deliverAs": "steer"})
        elif line.startswith("!q "):
            self._send({"type": "user_input", "text": line[3:], "deliverAs": "followUp"})
        else:
            self._send({"type": "user_input", "text": line})
        self._print(C_USER + "> " + R + line)

    @staticmethod
    def _set_plugin_option(name, value):
        if weechat is None:
            return
        try:
            weechat.config_set_plugin(name, value)
        except Exception:
            pass

    # -------------------------------------------------------------- cleanup

    def cleanup(self):
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass
            self.client = None
        for attr in ("client_hook", "write_hook"):
            if getattr(self, attr):
                weechat.unhook(getattr(self, attr))
                setattr(self, attr, None)
        if self.listen_sock is not None:
            try:
                self.listen_sock.close()
            except OSError:
                pass
            self.listen_sock = None
        if self.listen_hook:
            weechat.unhook(self.listen_hook)
            self.listen_hook = None
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass


def format_tool_args(name, args):
    """Human-oriented summary of a tool call's args (one line).

    Known tools show their main content (bash: command, edit: path + number
    of edits, memory_write: target + content, …); unknown tools get compact
    key=value pairs. Values longer than ARG_VALUE_LIMIT characters are
    clipped with an "…(+N)" marker. Returns "" when there is nothing to show.
    """
    if not isinstance(args, dict) or not args:
        return ""
    parts = []
    keys = TOOL_ARG_KEYS.get(name)
    if keys:
        for key in keys:
            value = args.get(key)
            if value is None or value == "":
                continue
            parts.append(_clip(value))
        if name == "edit":
            edits = args.get("edits")
            if isinstance(edits, list):
                n = len(edits)
                parts.append("%d edit%s" % (n, "" if n == 1 else "s"))
    else:
        # unknown tool: scalar key=value pairs (values clipped harder), with
        # compact JSON as a last resort for struct-only args
        rendered = []
        for key, value in args.items():
            if isinstance(value, str) and value != "":
                rendered.append("%s=%s" % (key, _clip(value, 120)))
            elif isinstance(value, (int, float)):
                rendered.append("%s=%s" % (key, value))
            if len(rendered) >= 3:
                break
        if rendered:
            parts.extend(rendered)
            if len(args) > 3:
                parts.append("…")
        else:
            try:
                parts.append(_clip(
                    json.dumps(args, separators=(",", ":"), default=str), 200))
            except (TypeError, ValueError):
                return ""
    return " ".join(parts)


BRIDGE = Bridge()


# ------------------------------------------------------- weechat callbacks

def pi_input_cb(data, buffer, line):
    BRIDGE.on_input(line)
    return weechat.WEECHAT_RC_OK


def pi_close_cb(data, buffer):
    BRIDGE.alive = False
    return weechat.WEECHAT_RC_OK


def pi_listen_cb(data, fd):
    BRIDGE.accept_pending()
    return weechat.WEECHAT_RC_OK


def pi_client_cb(data, fd):
    BRIDGE.client_event(int(fd))
    return weechat.WEECHAT_RC_OK


def pi_write_cb(data, fd):
    BRIDGE.flush_outq(int(fd))
    return weechat.WEECHAT_RC_OK


def pi_shutdown_cb():
    # /python unload pi_bridge → release fd hooks + unlink the socket so a
    # fresh load can rebind cleanly
    BRIDGE.cleanup()
    return weechat.WEECHAT_RC_OK


# -------------------------------------------------------------------- main

def main():
    dbg("main(): loading (sock=%s, debug=%s)" % (default_socket_path(), bool(_DBG_PATH)))
    weechat.register("pi_bridge", "simeng", "0.3.1", "MIT",
                     "mirror a pi coding agent session through a WeeChat buffer",
                     "pi_shutdown_cb", "")
    # plugin options (auto-created on first run; /set pi_bridge.<name> …)
    if not weechat.config_is_set_plugin("tool_output"):
        weechat.config_set_plugin("tool_output", DEFAULT_TOOL_OUTPUT)
    if not weechat.config_is_set_plugin("thinking"):
        weechat.config_set_plugin("thinking", DEFAULT_THINKING)
    BRIDGE.make_buffer()
    try:
        BRIDGE.make_server()
    except OSError as err:
        weechat.prnt("", C_ERR + "pi_bridge: cannot listen on %s (%s)%s"
                     % (BRIDGE.sock_path, err, R))
        return
    weechat.hook_signal("quit;upgrade", "pi_signal_cb", "")


if weechat is not None:
    main()
