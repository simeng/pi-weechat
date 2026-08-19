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

try:
    import weechat
except ImportError:  # allows importing this module outside WeeChat (tests)
    weechat = None

PROTOCOL = 1
MAX_LINE = 1024 * 1024  # must match MAX_LINE_BYTES in lib/codec.mjs

# Colors (WeeChat color tags; "gray" is dim)
C_USER = "color:white"
C_PI = "color:default"
C_TOOL = "color:blue"
C_STATUS = "color:cyan"
C_ERR = "color:red"
C_OK = "color:green"
C_DIM = "color:gray"
R = "color:reset"


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
        self._print(C_DIM + "type a line to send it to pi; "
                         "!s <text> steer · !q <text> queue · !new !compact !abort !status !model%s" % R)

    def _print(self, text):
        """Print one plain line (no time, no prefix)."""
        if self.alive and self.buffer:
            weechat.prnt(self.buffer, "\t\t%s" % text)

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
        weechat.hook_fd(sock.fileno(), 1, 0, 0, "pi_listen_cb", "")
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
                client.close()
                continue
            client.setblocking(False)
            self.client = client
            self.rxbuff = b""
            self.outq = b""
            weechat.hook_fd(client.fileno(), 1, 0, 0, "pi_client_cb", "")
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
        except OSError:
            data = b""
        if not data:  # peer closed (recv == 0) or error → disconnect
            self.drop_client()
            return
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
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass
            self.client = None
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
            return
        self.outq += (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        if self.write_hook is None:
            self.write_hook = weechat.hook_fd(self.client.fileno(), 0, 1, 0, "pi_write_cb", "")

    def flush_outq(self, fd):
        if self.client is None or not self.outq:
            if self.write_hook:
                weechat.unhook(self.write_hook)
                self.write_hook = None
            return
        try:
            n = self.client.send(self.outq)
        except BlockingIOError:
            return
        except OSError:
            self.drop_client()
            return
        self.outq = self.outq[n:]
        if not self.outq and self.write_hook:
            weechat.unhook(self.write_hook)
            self.write_hook = None

    # ---------------------------------------------------------- dispatching

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
        if t == "assistant_flush":
            return  # lines already complete; nothing to render
        if t == "tool_start":
            name = msg.get("toolName", "?")
            args = msg.get("args") or {}
            summary = ""
            try:
                s = json.dumps(args, separators=(",", ":"))
                summary = (" " + s[:120] + "…") if len(s) > 120 else (" " + s if s and s != "{}" else "")
            except (TypeError, ValueError):
                pass
            self._print(C_TOOL + "⚙ " + name + C_DIM + summary + R)
            return
        if t == "tool_end":
            ok = not msg.get("isError")
            color = C_OK if ok else C_ERR
            self._print(color + ("✔ " if ok else "✘ ") +
                        (msg.get("toolName", "tool") or "tool") + R)
            for line in str(msg.get("output") or "").splitlines():
                self._print(C_DIM + "  " + line + R)
            return
        if t == "error":
            self._print(C_ERR + "pi bridge: %s: %s%s" % (
                msg.get("code", "?"), msg.get("message", ""), R))
            return
        # unknown type: ignore (forward-compat)

    # ---------------------------------------------------------- user input

    def on_input(self, line):
        if self.client is None:
            self._print(C_ERR + "not connected to pi (see buffer title)" + R)
            return
        line = line.strip()
        if not line:
            return
        # buffer-local control commands → protocol 'command' messages
        command_map = {
            "!new": "new_session",
            "!compact": "compact",
            "!abort": "abort",
            "!status": "status",
            "!model": "model",
        }
        if line in command_map:
            self._send({"type": "command", "name": command_map[line]})
        elif line.startswith("!model "):
            # let pi's built-in /model command do the selection
            self._send({"type": "user_input", "text": "/model " + line[7:].strip()})
        elif line.startswith("!s "):
            self._send({"type": "user_input", "text": line[3:], "deliverAs": "steer"})
        elif line.startswith("!q "):
            self._send({"type": "user_input", "text": line[3:], "deliverAs": "followUp"})
        else:
            self._send({"type": "user_input", "text": line})
        self._print(C_USER + "> " + R + line)

    # -------------------------------------------------------------- cleanup

    def cleanup(self):
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass
            self.client = None
        if self.listen_sock is not None:
            try:
                self.listen_sock.close()
            except OSError:
                pass
            self.listen_sock = None
        if self.write_hook:
            weechat.unhook(self.write_hook)
            self.write_hook = None
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass


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


def pi_signal_cb(data, signal_name, _signal_data):
    if signal_name in ("quit", "upgrade"):
        BRIDGE.cleanup()
    return weechat.WEECHAT_RC_OK


# -------------------------------------------------------------------- main

def main():
    weechat.register("pi_bridge", "simeng", "0.1.0", "MIT",
                     "mirror a pi coding agent session through a WeeChat buffer",
                     "", "")
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
