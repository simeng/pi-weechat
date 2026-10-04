#!/usr/bin/env python3
"""In-process smoke test for weechat/pi_bridge.py.

Runs the real script against a stub `weechat` module and drives it with real
(in-process) socket clients playing the role of the pi extension — both the
Unix listener and the opt-in TCP listener. Verifies: handshake gating,
shared-secret challenge auth (protocol 2), output rendering into the buffer
(incl. markdown fenced-code highlighting and role-based nick prefixes), title updates, user input
on the wire, and the abuse-resistance measures
(auth deadline, pending cap, IP lockout, allowed_ips, read cap, rate limit).

Run: python3 test/smoke_weechat.py   (no dependencies beyond stdlib)
"""
import hashlib
import hmac
import importlib.util
import json
import os
import re
import select
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "weechat", "pi_bridge.py")

# ---------------------------------------------------------------- weechat stub

WEECHAT_RC_OK = 3


class WeechatStub:
    WEECHAT_RC_OK = 3

    def __init__(self):
        self.prints = []          # (kind, text)
        self.title = None
        self.fd_hooks = {}        # callback name -> [fd, read, write, data]
        self.buffer_name = None
        self.registered = None
        self.plugin_opts = {}     # config_*_plugin storage
        self.plugin_descs = {}    # config_set_desc_plugin storage
        self.timers = {}          # handle -> {cb, data, deadline, timeout, max_calls}
        self._timer_seq = 0
        self.config_hooks = []    # [(pattern, cb, data)]
        self.conf = {"irc.server_default.nicks": "alice,alice2"}  # global opts
        self.printf_tags = []     # [(tags, prefix, body)] from prnt_date_tags
        self.localvars = {}       # buffer localvars (localvar_set_*)

    # -- colors / plugin options -----------------------------------------
    def color(self, name):
        # Emulate weechat.color(): palette names → marker codes; theme names
        # ([color] section of weechat.conf) are undefined in the test env,
        # so the bridge's palette fallbacks are exercised end-to-end.
        return {"white": "W", "magenta": "M", "cyan": "C", "blue": "B",
                "yellow": "E", "green": "G", "red": "r", "236": "D",
                # syntax-highlight palette (HL_TOKENS) + reset marker
                "reset": "0", "bold magenta": "K", "darkgray": "d",
                "lightblue": "L", "lightgreen": "g",
                # text attributes: exactly the codes real WeeChat 4.10.1
                # returns (probed), which the TUI and relay clients decode
                "bold": "\x1a\x01", "italic": "\x1a\x03",
                "underline": "\x1a\x04", "reverse": "\x1a\x02",
                "dim": "\x1a\x06"}.get(name, "")

    def config_is_set_plugin(self, name):
        return name in self.plugin_opts

    def config_get_plugin(self, name):
        return self.plugin_opts.get(name, "")

    def config_set_plugin(self, name, value):
        self.plugin_opts[name] = value
        # real WeeChat fires hook_config callbacks on option changes
        opt = "plugins.var.python.pi_bridge." + name
        for pattern, cb, data in list(self.config_hooks):
            if pattern == opt or pattern.endswith(".*"):
                self.ns[cb](data, opt)
        return 1

    def config_set_desc_plugin(self, name, description):
        self.plugin_descs[name] = description
        return 1

    def config_get(self, name):
        # option name → pointer token, "" if the option doesn't exist (the
        # IRC plugin not loaded) — like real WeeChat, config_string takes
        # this pointer, not the name
        return ("ptr:" + name) if name in self.conf else ""

    def config_string(self, opt):
        # takes the pointer returned by config_get; anything else ⇒ ""
        if isinstance(opt, str) and opt.startswith("ptr:"):
            return self.conf.get(opt[4:], "")
        return ""

    def set_config(self, name, value):
        """Set a GLOBAL (non-plugin) option and fire hook_config callbacks
        registered for exactly that option (config_set_plugin only fires
        plugin-option patterns)."""
        self.conf[name] = value
        for pattern, cb, data in list(self.config_hooks):
            if pattern == name:
                self.ns[cb](data, name)
        return 1

    def register(self, name, author, version, license, desc, shutdown_function, charset):
        self.registered = name
        return True

    # -- buffer ----------------------------------------------------------
    def buffer_new(self, name, input_cb, input_data, close_cb, close_data):
        self.buffer_name = name
        self.input_cb_name = input_cb
        return "buffer"

    def buffer_set(self, buf, prop, value):
        if prop == "title":
            self.title = value
            self.prints.append(("TITLE", value))
        elif prop.startswith("localvar_set_"):
            self.localvars[prop[len("localvar_set_"):]] = value
        elif prop.startswith("localvar_unset_"):
            self.localvars.pop(prop[len("localvar_unset_"):], None)
        return 1

    def buffer_get_string(self, buf, prop):
        return ""

    def prnt(self, buf, msg):
        self.prints.append(("PRINT", msg))
        return 1

    def prnt_date_tags(self, buf, date, tags, message):
        # like the real API: the text before the first TAB is the line
        # prefix (prefix column); everything after it is the message body
        if "\t" in message:
            prefix, body = message.split("\t", 1)
        else:
            prefix, body = "", message
        self.prints.append(("PRINTF", body))
        self.printf_tags.append((tags, prefix, body))
        return 1

    # -- hooks -----------------------------------------------------------
    def hook_fd(self, fd, fr, fw, fe, cb, data):
        self.fd_hooks[cb] = [fd, fr, fw, data]
        return "hook:" + cb

    def hook_timer(self, interval, align_second, max_calls, cb, data):
        self._timer_seq += 1
        tid = "timer-%d" % self._timer_seq
        interval_s = max(interval, 1) / 1000.0
        self.timers[tid] = {
            "cb": cb, "data": data,
            "deadline": time.time() + interval_s,
            "timeout": interval_s,
            "max_calls": max_calls,
        }
        return tid

    def hook_config(self, pattern, cb, data):
        self.config_hooks.append((pattern, cb, data))
        return "hook:" + cb

    def unhook(self, hook):
        if hook in self.timers:
            del self.timers[hook]
            return 1
        cb = hook.split(":", 1)[1]
        self.fd_hooks.pop(cb, None)
        self.config_hooks = [p for p in self.config_hooks if p[1] != cb]
        return 1

    def hook_signal(self, sig, cb, data):
        return "sig"

    # -- event loop --------------------------------------------------------
    @staticmethod
    def _fd_open(fd):
        try:
            os.fstat(fd)
            return True
        except OSError:
            return False

    def pump(self, seconds=0.5):
        end = time.time() + seconds
        ns = self.ns
        while time.time() < end:
            # expired timers first
            now = time.time()
            fired = []
            for tid in list(self.timers):
                t = self.timers[tid]
                if now >= t["deadline"]:
                    if t["max_calls"] == 1:
                        del self.timers[tid]
                    else:
                        if t["max_calls"] > 1:
                            t["max_calls"] -= 1
                        t["deadline"] = now + t["timeout"]
                    fired.append((t["cb"], t["data"]))
            for cb, data in fired:
                ns[cb](data, 0)
            live = {n: h for n, h in self.fd_hooks.items()
                    if h[1] and h[0] >= 0 and self._fd_open(h[0])}
            # a hooked fd that was closed underneath → weechat calls the cb
            # once with fd == -1 (data carries the fd), then unhook
            for name, hook in list(self.fd_hooks.items()):
                if hook[1] and hook[0] >= 0 and not self._fd_open(hook[0]):
                    ns[name](hook[3], -1)
                    self.unhook("hook:" + name)
            read_fds = [h[0] for h in live.values()]
            r, _, _ = select.select(read_fds, [], [], 0.02) if read_fds else ([], [], [])
            progressed = False
            for fd in r:
                for name, hook in list(self.fd_hooks.items()):
                    if hook[0] == fd and hook[1]:
                        ns[name](hook[3], fd)
                        progressed = True
                        break
            for name, hook in list(self.fd_hooks.items()):
                if hook[2] and hook[0] >= 0:
                    ns[name](hook[3], hook[0])
                    progressed = True
            if not progressed and not fired:
                time.sleep(0.01)


# ------------------------------------------------------------------- helpers

def proof_for(token, nonce):
    return hmac.new(token.encode("utf-8"), nonce.encode("ascii"),
                    hashlib.sha256).hexdigest()


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def tcp_connect(port):
    c = socket.create_connection(("127.0.0.1", port), timeout=3)
    c.setblocking(False)
    return c


def send_line(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode())


def drain(sock, acc):
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                return False  # peer closed
            acc.extend(l for l in data.decode().split("\n") if l.strip())
    except BlockingIOError:
        pass
    return True


def buffer_text(stub):
    # literal replacement (a regex like color:[a-z]+ would eat the word right
    # after an untagged boundary, e.g. "color:cansession:")
    text = "\n".join(t for k, t in stub.prints)
    for tag in ("white", "default", "blue", "cyan", "red", "green",
                "gray", "reset"):
        text = text.replace("color:" + tag, "")
    return text


def main():
    tmp = tempfile.mkdtemp(prefix="pi-wc-smoke-")
    sock_path = os.path.join(tmp, "bridge.sock")
    os.environ["PI_WEECHAT_SOCK"] = sock_path

    stub = WeechatStub()
    ns = {}
    stub.ns = ns
    sys.modules["weechat"] = stub
    with open(SCRIPT) as f:
        code = f.read()
    exec(compile(code, SCRIPT, "exec"), ns)
    BRIDGE = ns["BRIDGE"]
    # expected cwd rendering for title/session assertions (respects ~)
    proj = ns["_short_path"]("/home/x/proj")

    assert stub.buffer_name == "pi", "buffer 'pi' must be created on load"
    assert os.path.exists(sock_path), "socket must be listening"
    assert oct(os.stat(sock_path).st_mode & 0o777) == "0o700", "socket perms 0700"
    assert stub.plugin_opts.get("tcp_listen") == "", "tcp_listen defaults empty"
    assert stub.plugin_opts.get("token") == "", "token defaults empty"
    assert stub.plugin_opts.get("allowed_ips") == "", "allowed_ips defaults empty"
    assert stub.plugin_opts.get("tool_output") == "summary", "tool_output defaults summary"
    # help descriptions registered for every plugin option (/help set …)
    for name in ("tcp_listen", "token", "allowed_ips",
                "tool_output", "thinking", "highlight"):
        assert name in stub.plugin_descs, "missing description for %s" % name
        assert stub.plugin_descs[name], "empty description for %s" % name
    assert stub.localvars.get("nick") == "alice", \
        "buffer localvar nick = first irc.server_default.nicks entry"

    def pump_and_drain(c=None, seconds=0.3):
        stub.pump(seconds)
        if c is not None:
            drain(c, recv_lines)

    recv_lines = []

    # ==================================================================
    # Phase A — unix hardening (fresh state, no client yet)
    # ==================================================================

    # auth deadline: a client that sends nothing is dropped silently
    ns["AUTH_TIMEOUT_S"] = 0.3
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(sock_path)
    c.setblocking(False)
    stub.pump(0.8)
    assert c.recv(10) == b"", "silent client must be dropped after auth timeout"
    assert BRIDGE.pending == [] and BRIDGE.client is None

    # unauthenticated-connection cap: 3 pendings held, 4th closed silently
    pendings = []
    for i in range(4):
        cc = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cc.connect(sock_path)
        cc.setblocking(False)
        pendings.append(cc)
        stub.pump(0.05)
    assert len(BRIDGE.pending) == 3, "exactly 3 pending unauthed connections"
    assert drain(pendings[3], []) is False, "4th connection closed silently"
    for i in range(3):
        assert drain(pendings[i], []) is True, "first 3 pendings still open"
    for cc in pendings:
        cc.close()
    stub.pump(0.2)
    assert BRIDGE.pending == [], "closed pendings dropped"
    ns["AUTH_TIMEOUT_S"] = 10

    # ==================================================================
    # Phase B — unix happy path (no token configured)
    # ==================================================================

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(sock_path)
    client.setblocking(False)
    send = lambda obj: client.sendall((json.dumps(obj) + "\n").encode())

    # protocol 2 handshake gating: the CLIENT sends its hello first (no
    # challenge when no token is configured); the server answers after
    # validating it
    send({"type": "hello", "protocol": ns["PROTOCOL"], "name": "pi"})
    pump_and_drain(client)
    hello = json.loads(recv_lines.pop(0))
    assert hello["type"] == "hello" and hello["protocol"] == ns["PROTOCOL"], hello
    assert "pi connected" in buffer_text(stub)

    # pi → weechat: mirror output into the buffer
    send({"type": "session_info", "cwd": "/home/x/proj", "model": "prov/model-a"})
    send({"type": "status", "state": "thinking"})
    send({"type": "assistant_line", "msgId": 1, "text": "Hello from pi"})
    send({"type": "tool_start", "toolCallId": "t1", "toolName": "bash",
          "args": {"command": "ls"}})
    send({"type": "tool_end", "toolCallId": "t1", "isError": False,
          "output": "a.txt\nb.txt"})
    # tool_start arg summaries: main content per tool, clipped at 300 chars
    send({"type": "tool_start", "toolCallId": "t4", "toolName": "memory_write",
          "args": {"target": "long_term", "content": "remembered fact"}})
    send({"type": "tool_start", "toolCallId": "t5", "toolName": "bash",
          "args": {"command": "echo " + "z" * 400}})
    send({"type": "user_echo", "text": "typed in pi terminal"})
    send({"type": "error", "code": "test", "message": "boom"})
    pump_and_drain(client)

    text = buffer_text(stub)
    assert "session: %s prov/model-a" % proj in text, "session line missing"
    assert "Hello from pi" in text, "assistant line missing"
    tool_rows = [(tg, p, b) for tg, p, b in stub.printf_tags
                 if "nick_bash" in tg]
    assert tool_rows, "tool lines must carry the tool nick (auto mode)"
    assert all("bash" in p for _, p, _ in tool_rows), \
        "tool start/end/output name the tool in the PREFIX column"
    assert any("ls" in b for _, _, b in tool_rows), "tool args stay in the body"
    assert not any("bash" in b for _, _, b in tool_rows), \
        "auto nick mode drops the tool name from the body (it is the nick)"
    assert "a.txt" in text and "b.txt" in text, "tool output missing"
    assert "long_term remembered fact" in text, \
        "memory_write args must show target + content"
    assert "…(+105)" in text, \
        "long bash commands must be clipped (405 - 300 = 105 more chars)"
    assert stub.title == "π: %s (thinking…)" % proj, stub.title
    # turn settle (busy → idle) ⇒ one extra highlight line below the last
    # message line (left untouched); idle → idle is not a settle
    send({"type": "status", "state": "idle"})
    pump_and_drain(client, 0.2)
    assert stub.title == "π: %s (idle)" % proj, stub.title
    hl = [(tags, p, b) for tags, p, b in stub.printf_tags
          if "notify_highlight" in tags]
    assert len(hl) == 1, "settle emits exactly one highlight line"
    assert hl[0][1] == "", "highlight line has no nick prefix"
    assert hl[0][2].strip("G0") == "✔ ready!", "highlight line text"
    n_printf = len(stub.printf_tags)
    send({"type": "status", "state": "idle"})  # idle → idle: not a settle
    pump_and_drain(client, 0.2)
    assert not [1 for tags, _, _ in stub.printf_tags[n_printf:]
               if "notify_highlight" in tags], \
        "idle → idle must not re-emit the ready line"

    # ==================================================================
    # Phase B3 — Pi-owned run/turn clocks in the buffer title
    # ==================================================================

    # _fmt_elapsed unit: 0-99s "Ns", 100-3599s "Nm", >=3600s "NhMm"
    fmt_elapsed = ns["_fmt_elapsed"]
    assert fmt_elapsed(3) == "3s", fmt_elapsed(3)
    assert fmt_elapsed(90) == "90s", fmt_elapsed(90)
    assert fmt_elapsed(99) == "99s", fmt_elapsed(99)
    assert fmt_elapsed(100) == "1m", fmt_elapsed(100)
    assert fmt_elapsed(300) == "5m", fmt_elapsed(300)
    assert fmt_elapsed(3599) == "59m", fmt_elapsed(3599)
    assert fmt_elapsed(3600) == "1h", fmt_elapsed(3600)
    assert fmt_elapsed(3665) == "1h1m", fmt_elapsed(3665)

    BRIDGE.state = "thinking"
    timing = {
        "type": "timing", "runMs": 12_000, "turnMs": 42_000,
        "turn": 10, "turns": 10, "runActive": True, "turnActive": True,
        "runPaused": False, "hasRun": True,
    }
    send(timing)
    pump_and_drain(client, 0.2)
    assert stub.title == "π: %s (thinking… · run 12s · 42s · turn 10)" % proj, stub.title

    # The local WeeChat tick advances a received active snapshot, not state
    # reconstructed from inputs. Turn elapsed includes tool time.
    BRIDGE.timing_received_at = time.monotonic() - 90
    ns["pi_tick_cb"]("", 0)
    assert stub.title == "π: %s (thinking… · run 1m · 2m · turn 10)" % proj, stub.title
    BRIDGE.state = "tool:bash"
    ns["pi_tick_cb"]("", 0)
    assert stub.title == "π: %s (tool: bash · run 1m · 2m · turn 10)" % proj, stub.title

    # A paused snapshot must not advance during a blocking UI prompt.
    paused = dict(timing, runMs=15_000, turnMs=9_000, runPaused=True)
    send(paused)
    pump_and_drain(client, 0.2)
    paused_title = stub.title
    BRIDGE.timing_received_at = time.monotonic() - 90
    ns["pi_tick_cb"]("", 0)
    assert stub.title == paused_title
    assert "run 15s · 9s · turn 10" in stub.title, stub.title

    # A completed model turn freezes while the run clock continues.
    between_turns = dict(timing, runMs=35_000, turnMs=42_000, turnActive=False)
    send(between_turns)
    pump_and_drain(client, 0.2)
    BRIDGE.timing_received_at = time.monotonic() - 10
    ns["pi_tick_cb"]("", 0)
    assert "run 45s · 42s · turn 10" in stub.title, stub.title

    # Settle freezes the final run summary, including Pi's turn count.
    settled = dict(
        timing, runMs=1_020_000, turnMs=42_000, runActive=False,
        turnActive=False, runPaused=False,
    )
    send(settled)
    BRIDGE.state = "idle"  # the settle banner is covered in the previous phase
    send({"type": "status", "state": "idle"})
    pump_and_drain(client, 0.2)
    assert stub.title == "π: %s (idle · last run 17m · 10 turns)" % proj, stub.title
    frozen_title = stub.title
    BRIDGE.timing_received_at = time.monotonic() - 90
    ns["pi_tick_cb"]("", 0)
    assert stub.title == frozen_title, "settled timing summary must remain frozen"

    # Local-only commands do not start or alter a timing clock.
    ns["pi_input_cb"]("", "buffer", "!help")
    assert stub.title == frozen_title


    # ------------------------------------------------- arg summary unit test
    fmt = ns["format_tool_args"]
    assert fmt("bash", {"command": "ls -la"}) == "ls -la"
    assert fmt("bash", {"command": "a\nb\nc"}) == "a b c", \
        "multi-line values are flattened to one line"
    long_cmd = "echo " + "x" * 500
    out = fmt("bash", {"command": long_cmd})
    assert out.startswith("echo x") and out.endswith("…(+205)"), out[-20:]
    assert fmt("edit", {"path": "/tmp/x.py",
                        "edits": [{"oldText": "a", "newText": "b"},
                                  {"oldText": "c", "newText": "d"}]}) \
        == "/tmp/x.py 2 edits"
    assert fmt("memory_read", {"target": "daily", "date": "2026-08-21"}) \
        == "daily"
    assert fmt("memory_search", {"query": "weechat colors", "mode": "deep"}) \
        == "weechat colors"
    out = fmt("memory_write", {"target": "long_term",
                               "content": "#decision\n" + "y" * 400})
    assert out.startswith("long_term #decision y") and out.endswith("…(+110)"), \
        out[-20:]
    out = fmt("custom_tool", {"a": "1", "b": "x" * 200, "c": [1, 2], "d": "z"})
    assert out.startswith("a=1 b=" + "x" * 120) and out.endswith("d=z …"), \
        "unknown tools get compact k=v pairs (strings clipped at 120)"
    assert fmt("bash", {}) == "" and fmt("bash", None) == "" and \
        fmt("bash", {"command": ""}) == "", "empty args → no summary"

    # _theme: a defined theme name wins over the palette fallback
    stub.color = lambda name: ("T" if name in ("chat", "chat_nick_self")
                               else {"white": "W", "cyan": "C"}.get(name, ""))
    assert ns["_theme"]("chat_nick_self", "white") == "T", \
        "theme names must take precedence over palette fallbacks"
    # _short_path: home prefix → ~, other paths unchanged
    home = os.path.expanduser("~")
    sp = ns["_short_path"]
    if home != "~":
        assert sp(home) == "~", sp(home)
        assert sp(home + "/sub") == "~/sub", sp(home + "/sub")
    assert sp("/elsewhere/x") == "/elsewhere/x", sp("/elsewhere/x")
    assert ns["_theme"]("no_such_name", "white") == "W", \
        "undefined theme names fall back to the palette color"

    # weechat → pi: user input over the wire
    ns["pi_input_cb"]("", "buffer", "hello from weechat")
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "user_input", "text": "hello from weechat"}, msg

    # steer prefix maps to deliverAs
    ns["pi_input_cb"]("", "buffer", "!s do this instead")
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "user_input", "text": "do this instead",
                   "deliverAs": "steer"}, msg

    # control command routing
    ns["pi_input_cb"]("", "buffer", "!new")
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "command", "name": "new_session"}, msg

    # echo of typed lines appears in the buffer — under the user's IRC nick
    # (prnt_date_tags, tag prefix_nick_chat_nick_self, nick before the TAB;
    # plain text body, no legacy '> ' marker)
    me_tags = "self_msg,notify_none,no_highlight,prefix_nick_chat_nick_self"
    me_rows = [(p, b) for tags, p, b in stub.printf_tags if tags == me_tags]
    me_lines = [b for _, b in me_rows]
    assert all("alice" in p for p, _ in me_rows), \
        "user lines carry the user's nick as the line prefix"
    assert "hello from weechat" in me_lines, "buffer input echo under user nick"
    assert "!s do this instead" in me_lines, "!s echo under user nick"
    assert not any(t.startswith("> ") for t in me_lines), \
        "user-nick lines must not carry the legacy '> ' marker"

    # ------------------------------------------------------------------
    # !cd + ui_request / !pick (protocol-3 interactive prompts)
    # ------------------------------------------------------------------

    # bare !cd → local usage hint, nothing on the wire
    ns["pi_input_cb"]("", "buffer", "!cd")
    pump_and_drain(client, 0.4)
    assert recv_lines == [], "bare !cd must not reach pi"
    assert "usage: !cd <path>" in buffer_text(stub), "!cd usage hint missing"

    # !cd <path> → command message to pi
    ns["pi_input_cb"]("", "buffer", "!cd ~/projects/foo")
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "command", "name": "cd",
                   "arg": "~/projects/foo"}, msg

    # !pick with nothing pending → local error, nothing on the wire
    ns["pi_input_cb"]("", "buffer", "!pick 1")
    pump_and_drain(client, 0.4)
    assert recv_lines == [], "!pick without a prompt must not reach pi"
    assert "nothing to pick" in buffer_text(stub)

    # select prompt: numbered options (+descriptions) and a hint line
    send({"type": "status", "state": "idle"})
    send({"type": "ui_request", "id": 7, "method": "select",
          "title": "No exact match for /x. Which directory?",
          "options": ["/opt/alpha",
                      {"label": "/opt/beta", "description": "the beta one"}]})
    pump_and_drain(client, 0.4)
    text = buffer_text(stub)
    assert "? No exact match for /x. Which directory?" in text
    assert "1. /opt/alpha" in text and "2. /opt/beta" in text
    assert "the beta one" in text, "option description must render"
    assert "!pick cancel" in text, "hint line must mention !pick"
    assert BRIDGE.pending_ui is not None and BRIDGE.pending_ui["id"] == 7
    assert stub.title == "π: %s (idle · last run 17m · 10 turns) — awaiting !pick" % proj, stub.title

    # !pick by number → ui_response with the option text; title hint clears
    ns["pi_input_cb"]("", "buffer", "!pick 2")
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "ui_response", "id": 7,
                   "value": "/opt/beta"}, msg
    assert BRIDGE.pending_ui is None
    assert stub.title == "π: %s (idle · last run 17m · 10 turns)" % proj, stub.title

    # multi-select: comma list → array value; out-of-range number rejected
    send({"type": "ui_request", "id": 8, "method": "select",
          "title": "Pick some", "multiple": True,
          "options": ["a", "b", "c"]})
    pump_and_drain(client, 0.2)
    ns["pi_input_cb"]("", "buffer", "!pick 9")
    pump_and_drain(client, 0.3)
    assert recv_lines == [], "out-of-range number must not be sent"
    assert "no such option" in buffer_text(stub)
    ns["pi_input_cb"]("", "buffer", "!pick 1,3")
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "ui_response", "id": 8, "value": ["a", "c"]}, msg

    # exact option text is accepted (single select → string value)
    send({"type": "ui_request", "id": 9, "method": "select",
          "title": "Keep or drop?", "options": ["keep", "drop"]})
    pump_and_drain(client, 0.2)
    ns["pi_input_cb"]("", "buffer", "!pick drop")
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "ui_response", "id": 9, "value": "drop"}, msg

    # a new prompt supersedes the pending one: old id released as cancelled
    send({"type": "ui_request", "id": 10, "method": "select",
          "title": "old prompt", "options": ["x"]})
    pump_and_drain(client, 0.2)
    send({"type": "ui_request", "id": 11, "method": "select",
          "title": "new prompt", "options": ["y"]})
    pump_and_drain(client, 0.3)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "ui_response", "id": 10, "cancelled": True}, msg
    assert BRIDGE.pending_ui["id"] == 11
    ns["pi_input_cb"]("", "buffer", "!pick cancel")
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "ui_response", "id": 11, "cancelled": True}, msg
    assert BRIDGE.pending_ui is None

    # input prompt: free-form answer via !pick <text> (incl. spaces)
    send({"type": "ui_request", "id": 12, "method": "input",
          "title": "Enter a value", "placeholder": "type something…"})
    pump_and_drain(client, 0.2)
    assert "!pick <your answer>" in buffer_text(stub)
    assert "type something…" in buffer_text(stub), "placeholder must render"
    ns["pi_input_cb"]("", "buffer", "!pick hello world")
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "ui_response", "id": 12,
                   "value": "hello world"}, msg

    # a malformed ui_request is ignored (no pending state, nothing sent)
    send({"type": "ui_request", "method": "select"})
    pump_and_drain(client, 0.3)
    assert recv_lines == [] and BRIDGE.pending_ui is None
    assert "bad ui_request" in buffer_text(stub)

    # the !cd / !pick lines typed above were echoed under the user's IRC nick
    # as well — same path as plain input (no legacy '> ' marker)
    me_rows = [(p, b) for tags, p, b in stub.printf_tags if tags == me_tags]
    me_lines = [b for _, b in me_rows]
    for expected in ("!cd ~/projects/foo", "!pick 2", "!pick 1,3",
                     "!pick drop", "!pick cancel", "!pick hello world"):
        assert expected in me_lines, "user-nick echo missing: %r" % expected
    assert all("alice" in p for p, _ in me_rows), \
        "user lines carry the user's nick as the line prefix"

    # ping → pong
    send({"type": "ping", "ts": 123})
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "pong", "ts": 123}, msg

    # oversized line is dropped without killing the connection.
    # Interleave sending with pumping: the bridge only reads when we pump,
    # so a plain blocking sendall would deadlock on a full socket buffer.
    payload = b"x" * (1024 * 1024 + 10) + b"\n"
    sent = 0
    while sent < len(payload):
        try:
            n = client.send(payload[sent:])
            sent += n
        except BlockingIOError:
            pass
        stub.pump(0.02)
    send({"type": "assistant_line", "msgId": 2, "text": "still alive"})
    send({"type": "assistant_flush", "msgId": 2})  # block-level flush (see _print_assistant)
    pump_and_drain(client, 0.5)
    assert any("still alive" in t for k, t in stub.prints), "connection lost after oversize"

    # tool output filtering: 'off' hides the body, 'full' shows it again
    ns["pi_input_cb"]("", "buffer", "!tools off")
    pump_and_drain(client, 0.2)
    assert recv_lines == [], "!tools must not send anything to pi"
    send({"type": "tool_end", "toolCallId": "t2", "isError": False,
          "output": "hidden line"})
    pump_and_drain(client, 0.3)
    text = buffer_text(stub)
    assert "hidden line" not in text, "tool_output=off must hide the body"
    ns["pi_input_cb"]("", "buffer", "!tools summary")
    pump_and_drain(client, 0.2)
    many = "\n".join("l%d" % i for i in range(10))
    send({"type": "tool_end", "toolCallId": "t3", "isError": False,
          "output": many})
    pump_and_drain(client, 0.3)
    text = buffer_text(stub)
    assert "l0" in text and "l9" in text, "summary keeps first/last lines"
    assert "more lines" in text, "summary elides the middle"

    # thinking lines are hidden by default; !think on enables them
    send({"type": "thinking_line", "msgId": 3, "text": "hidden thought"})
    pump_and_drain(client, 0.3)
    text = buffer_text(stub)
    assert "hidden thought" not in text, "thinking must be hidden by default"
    ns["pi_input_cb"]("", "buffer", "!think on")
    pump_and_drain(client, 0.2)
    assert recv_lines == [], "!think must not send anything to pi"
    text = buffer_text(stub)
    assert "thinking: on" in text, "!think on confirms the mode"
    send({"type": "thinking_line", "msgId": 3, "text": "visible thought"})
    pump_and_drain(client, 0.3)
    text = buffer_text(stub)
    assert "visible thought" in text, "!think on must render thinking lines"

    # tool output body and thinking lines must use different colors
    raw = [t for k, t in stub.prints if k in ("PRINT", "PRINTF")]
    assert any(t.startswith("C\U0001F4AD ") for t in raw), \
        "thinking lines are flush-left (no indent), dim cyan"
    assert any(t.startswith("B  ") for t in raw), \
        "tool output body uses its own color (blue), distinct from thinking"

    # !help is answered locally (nothing hits the wire)
    ns["pi_input_cb"]("", "buffer", "!help")
    pump_and_drain(client, 0.3)
    assert recv_lines == [], "!help must not send anything to pi"
    text = buffer_text(stub)
    assert "steer" in text and "!tools" in text and "!think" in text, \
        "!help prints the command list"

    # !model <provider/id> → command with arg
    ns["pi_input_cb"]("", "buffer", "!model prov/model-b")
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "command", "name": "model", "arg": "prov/model-b"}, msg

    # lines must keep their stored date for relay clients (Glowing Bear):
    # a leading "\t\t" prefix suppresses the TUI timestamp but zeroes the
    # line's date field (relay would render 01.01.1970)
    assert not any(t.startswith("\t") for k, t in stub.prints), \
        "lines must be printed without leading tabs so they keep real dates"

    # ==================================================================
    # Nick prefixes — who-spoke rendering (pi_bridge.nicks + irc nicks)
    # ==================================================================

    RS = "0"  # the stub's reset marker (real WeeChat: color:reset)
    # the buffer localvar carries the user nick (first config entry);
    # user lines render in the prefix column under the real nick
    assert stub.localvars.get("nick") == "alice", \
        "buffer localvar nick = first irc.server_default.nicks entry"

    # pi-originated lines render under the nick that names who spoke
    # (prnt_date_tags, tags notify_none,nick_<name>,prefix_nick_chat_nick):
    # assistant prose/fences → `pi`, tool lines + their output → the tool
    # name, thinking → `think`
    def nick_rows(tag):
        return [(p, b) for tags, p, b in stub.printf_tags if tag in tags]

    pi_rows = nick_rows("nick_pi")
    assert pi_rows and all("pi" in p for p, _ in pi_rows), \
        "assistant lines carry `pi` as the line prefix"
    pi_lines = [b for _, b in pi_rows]
    assert "Hello from pi" + RS in pi_lines, \
        "assistant prose via prnt_date_tags with the pi prefix"

    bash_rows = nick_rows("nick_bash")
    assert bash_rows and all("bash" in p for p, _ in bash_rows), \
        "tool lines carry the tool name as the line prefix"
    # exact body: C_TOOL + glyph, then C_DIM + " " + args — no double space in
    # auto mode (the name lives in the nick, not the body)
    assert "M⚙C ls" + RS in [b for _, b in bash_rows], \
        "tool_start body is glyph + args (name lives in the nick)"
    assert any(b.startswith("G✔") for _, b in bash_rows), \
        "tool_end body is the bare glyph"
    assert "B  a.txt" + RS in [b for _, b in bash_rows], \
        "tool output body under the tool nick"

    think_rows = nick_rows("nick_think")
    assert think_rows and all("think" in p for p, _ in think_rows), \
        "thinking lines carry the `think` nick"
    assert any(b.startswith("C\U0001F4AD ") for _, b in think_rows), \
        "thinking body under the think nick"

    assert nick_rows("nick_memory_write"), \
        "unknown/custom tool names get their own nick too"
    assert not any("nick_bash" in tags and "pi" in p
                   for tags, p, _ in stub.printf_tags), \
        "a tool line is never labelled `pi` in auto mode"

    # user lines (buffer input, !s echo, user_echo) render under the user's
    # nick — prefix_nick_chat_nick_self, no legacy '> ' marker in the text
    me_lines = [b for tags, _, b in stub.printf_tags if tags == me_tags]
    assert "typed in pi terminal" in me_lines, "user_echo under user nick"
    assert not any(t.startswith("> ") for t in me_lines), \
        "user-nick lines must not carry the legacy '> ' marker"

    # system lines stay prefix-less: prnt, no nick tags
    prnt_texts = [t for k, t in stub.prints if k == "PRINT"]
    assert any("session: %s prov/model-a" % proj in t for t in prnt_texts), \
        "session_info via prnt"
    assert any("pi bridge: test: boom" in t for t in prnt_texts), \
        "error line via prnt"
    assert any("steer current turn" in t for t in prnt_texts), \
        "!help line via prnt"
    all_printf = [b for _, _, b in stub.printf_tags]
    assert not any("session: " in t or "pi bridge:" in t
                   or "steer current turn" in t for t in all_printf), \
        "system lines must not carry nick tags"

    # ==================================================================
    # pi_bridge.nicks — auto (tool / think / pi) vs pi (legacy one nick)
    # ==================================================================

    # _nick_for: a nick is a prefix field AND part of a tag name, so TAB,
    # space, comma and anything outside [A-Za-z0-9_.-] must not survive
    sanitize = ns["_nick_for"]
    assert sanitize("read") == "read"
    assert sanitize("memory_search") == "memory_search"
    assert sanitize("bad\ttool,name here") == "bad_tool_name_here", \
        "TAB/space/comma are the prefix-split and tag-list separators"
    assert sanitize("  \t ") == "pi" and sanitize("") == "pi", "blank → pi"
    assert sanitize(None) == "pi" and sanitize(42) == "pi", "non-string → pi"
    assert sanitize("héllo") == "h_llo", "non-ASCII collapses to _"
    assert len(sanitize("x" * 200)) == 32, "nick length is capped"

    ns["pi_input_cb"]("", "buffer", "!nick")
    pump_and_drain(client, 0.2)
    assert recv_lines == [], "!nick must not send anything to pi"
    assert "nick mode: auto" in buffer_text(stub), "!nick reports the mode"

    ns["pi_input_cb"]("", "buffer", "!nick bogus")
    pump_and_drain(client, 0.2)
    assert "unknown nick mode" in buffer_text(stub), "!nick rejects bad modes"
    assert BRIDGE.nicks_mode() == "auto", "a rejected mode must not stick"

    # !nick pi restores the single-nick rendering, tool name back in the body
    ns["pi_input_cb"]("", "buffer", "!nick pi")
    pump_and_drain(client, 0.2)
    n_before = len(stub.printf_tags)
    send({"type": "tool_start", "toolCallId": "t7", "toolName": "bash",
          "args": {"command": "uname -a"}})
    send({"type": "tool_end", "toolCallId": "t7", "isError": True,
          "output": "nope"})
    pump_and_drain(client, 0.3)
    legacy = stub.printf_tags[n_before:]
    assert legacy and all(tags == "notify_none,prefix_nick_chat_nick"
                          for tags, _, _ in legacy), \
        "!nick pi emits exactly today's tags (no nick_<tool> tag)"
    assert all(p.endswith("pi" + RS) for _, p, _ in legacy), \
        "!nick pi prefixes every pi line with `pi`"
    assert any("bash" in b and "uname -a" in b for _, _, b in legacy), \
        "pi mode keeps the tool name in the tool_start body"
    assert any("✘ bash" in b for _, _, b in legacy), \
        "pi mode keeps the tool name on the tool_end line"

    # back to auto: tool_end reuses the nick learned from its tool_start
    ns["pi_input_cb"]("", "buffer", "!nick auto")
    pump_and_drain(client, 0.2)
    n_before = len(stub.printf_tags)
    send({"type": "tool_start", "toolCallId": "t8", "toolName": "web_fetch",
          "args": {"url": "https://x.test"}})
    send({"type": "tool_end", "toolCallId": "t8", "isError": False,
          "output": "fetched"})
    pump_and_drain(client, 0.3)
    rows = stub.printf_tags[n_before:]
    assert rows and all("nick_web_fetch" in tags and "web_fetch" in p
                        for tags, p, _ in rows), \
        "tool_end/output reuse the nick learned from tool_start"
    assert not any("web_fetch" in b for _, _, b in rows), \
        "auto mode: the name is the nick, not part of the body"
    # interleaved calls: a tool_end must reuse the nick of ITS OWN tool_start,
    # not the most recent one (parallel tool calls are normal in pi)
    n_before = len(stub.printf_tags)
    send({"type": "tool_start", "toolCallId": "t9", "toolName": "read",
          "args": {"path": "/etc/hosts"}})
    send({"type": "tool_start", "toolCallId": "t10", "toolName": "grep",
          "args": {"pattern": "nick"}})
    send({"type": "tool_end", "toolCallId": "t10", "isError": False,
          "output": "2 matches"})
    send({"type": "tool_end", "toolCallId": "t9", "isError": True,
          "output": "permission denied"})
    pump_and_drain(client, 0.3)
    rows = stub.printf_tags[n_before:]
    assert any("nick_grep" in tags and "2 matches" in body
               for tags, _, body in rows), "grep end keeps the grep nick"
    assert any("nick_read" in tags and "permission denied" in body
               for tags, _, body in rows), "read end keeps the read nick"
    assert "t9" not in BRIDGE.tool_nicks and "t10" not in BRIDGE.tool_nicks, \
        "completed calls are dropped from the id map"

    # an end whose start was never seen (reconnect mid-call) still renders,
    # under the generic `tool` nick
    n_before = len(stub.printf_tags)
    send({"type": "tool_end", "toolCallId": "ghost", "isError": False,
          "output": "orphan output"})
    pump_and_drain(client, 0.3)
    rows = stub.printf_tags[n_before:]
    assert rows and all("nick_tool" in tags for tags, _, _ in rows), \
        "unmatched tool_end falls back to the `tool` nick"

    # mode switch in the middle of a call: the call line is auto, the result
    # line is legacy — and the result still knows which tool it belongs to
    n_before = len(stub.printf_tags)
    send({"type": "tool_start", "toolCallId": "t11", "toolName": "edit",
          "args": {"path": "a.txt"}})
    pump_and_drain(client, 0.2)
    ns["pi_input_cb"]("", "buffer", "!nick pi")
    pump_and_drain(client, 0.2)
    send({"type": "tool_end", "toolCallId": "t11", "isError": False,
          "output": "saved"})
    pump_and_drain(client, 0.3)
    rows = stub.printf_tags[n_before:]
    assert any("nick_edit" in tags for tags, _, _ in rows), \
        "the call line rendered in auto mode"
    legacy_rows = [r for r in rows if r[0] == "notify_none,prefix_nick_chat_nick"]
    assert legacy_rows and any("✔ edit" in body for _, _, body in legacy_rows), \
        "the result line rendered in legacy mode, naming the remembered tool"
    ns["pi_input_cb"]("", "buffer", "!nick auto")
    pump_and_drain(client, 0.2)

    # fallback: empty irc.server_default.nicks ⇒ legacy '> ' marker via prnt,
    # no user-nick printf (and the hook clears the localvar)
    n0 = len(stub.prints)
    n1 = len(stub.printf_tags)
    stub.set_config("irc.server_default.nicks", "")
    assert "nick" not in stub.localvars, "empty nicks ⇒ localvar cleared"
    ns["pi_input_cb"]("", "buffer", "nickless line")
    pump_and_drain(client, 0.3)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "user_input", "text": "nickless line"}, \
        "the line still reaches pi (only the rendering falls back)"
    assert any(k == "PRINT" and "> 0nickless line" in t
               for k, t in stub.prints[n0:]), \
        "empty nicks ⇒ user line falls back to the '> ' marker via prnt"
    assert stub.printf_tags[n1:] == [], "no user-nick printf while nick is empty"

    # live update: hook_config re-applies the localvar without a reload
    stub.set_config("irc.server_default.nicks", "bob")
    assert stub.localvars.get("nick") == "bob", \
        "hook_config on irc.server_default.nicks re-applies the nick localvar"

    # ==================================================================
    # Phase B2 — markdown fenced code blocks: syntax highlighting
    # ==================================================================

    # color chars from the stub palette (see WeechatStub.color); C_DIM falls
    # back to palette cyan, so fence lines render "C…0" like status lines
    KW, STR, NUM, COM, FN, VAR, TYP, RS = "K", "G", "C", "d", "L", "E", "g", "0"
    DIM = "C"
    hl = ns["highlight_code"]

    # --- unit: bash — comment / string / var / env-assign / number
    assert hl("ls -la # list files", "bash", {}) == (
        "ls -la " + COM + "# list files" + RS), "bash comment"
    assert hl('echo "hi" $HOME 42', "bash", {}) == (
        "echo " + STR + '"hi"' + RS + " " + VAR + "$HOME" + RS
        + " " + NUM + "42" + RS), "bash string/var/number"
    assert hl("FOO=bar ls", "bash", {}) == (
        VAR + "FOO" + RS + "=bar ls"), "bash env-assign prefix"

    # --- unit: rust — kw/type/fn/num + block comment across lines
    assert hl("fn main() { let x: u32 = 5; }", "rust", {}) == (
        KW + "fn" + RS + " " + FN + "main" + RS + "() { "
        + KW + "let" + RS + " x: " + TYP + "u32" + RS
        + " = " + NUM + "5" + RS + "; }"), "rust basics"
    ctx = {}
    hl("let a = 1; /* open", "rust", ctx)
    assert ctx.get("com") is True, "unterminated /* must set comment state"
    assert hl("still */ let b = 2;", "rust", ctx) == (
        COM + "still */" + RS + " " + KW + "let" + RS + " b = "
        + NUM + "2" + RS + ";"), "block comment continues on next line"
    assert ctx.get("com") is False, "state cleared once the comment closes"

    # --- unit: css — at-rule, property names, hex color, units
    assert hl("@media (min-width: 600px) { color: #fff; }", "css", {}) == (
        KW + "@media" + RS + " (" + VAR + "min-width" + RS + ": "
        + NUM + "600px" + RS + ") { " + VAR + "color" + RS + ": "
        + NUM + "#fff" + RS + "; }"), "css at-rule/property/hex"

    # --- unit: html — tag, attr, value, comment
    assert hl('<div class="box">it<!-- c --></div>', "html", {}) == (
        KW + "<div" + RS + " " + VAR + "class" + RS + "="
        + STR + '"box"' + RS + ">it" + COM + "<!-- c -->" + RS
        + KW + "</div" + RS + ">"), "html"  # tag name colored, bracket plain

    # --- unit: php — open tag, keyword, variable, line comment
    assert hl('<?php echo $name; // hi', "php", {}) == (
        KW + "<?php" + RS + " " + KW + "echo" + RS + " "
        + VAR + "$name" + RS + "; " + COM + "// hi" + RS), "php"

    # --- unit: python — keyword, call, number, comment
    assert hl("def foo(x=1): return None  # done", "python", {}) == (
        KW + "def" + RS + " " + FN + "foo" + RS + "(x=" + NUM + "1" + RS
        + "): " + KW + "return" + RS + " " + KW + "None" + RS + "  "
        + COM + "# done" + RS), "python"

    # --- unit: json — key vs value; yaml — key vs comment; unknown lang
    assert hl('{"key": "val", "n": 3}', "json", {}) == (
        "{" + VAR + '"key"' + RS + ": " + STR + '"val"' + RS + ", "
        + VAR + '"n"' + RS + ": " + NUM + "3" + RS + "}"), "json key vs value"
    assert hl("name: bob # the name", "yaml", {}) == (
        VAR + "name" + RS + ": bob " + COM + "# the name" + RS), "yaml"
    assert hl("val: ~", "yaml", {}) == (
        VAR + "val" + RS + ": " + KW + "~" + RS), \
        "yaml ~ (null) is a keyword, not plain text"
    assert hl("whatever $x // c", "cobol", {}) == "whatever $x // c", \
        "unknown language passes through verbatim"

    # --- integration: streamed fence through dispatch (unix client live)
    for text in ("Here is the command to run:", "```bash",
                 "ls -la # list everything", "```",
                 "and one we do not know:", "```cobol", "EQU 1", "```"):
        send({"type": "assistant_line", "msgId": 42, "text": text})
    pump_and_drain(client)
    raw = [t for k, t in stub.prints if k in ("PRINT", "PRINTF")]
    assert "Here is the command to run:" + RS in raw, "prose line stays uncolored"
    assert DIM + "```bash" + RS in raw, "opening fence prints dim"
    assert raw.count(DIM + "```" + RS) >= 1, "closing fence prints dim"
    assert ("  ls -la " + COM + "# list everything" + RS) in raw, \
        "known language: indented + highlighted body"
    assert "  EQU 1" in raw, "unknown language: indented but plain"

    # a msgId change must reset an open fence (next message is prose again)
    send({"type": "assistant_line", "msgId": 43, "text": "```rust"})
    send({"type": "assistant_line", "msgId": 99, "text": "let a = 1;"})
    send({"type": "assistant_flush", "msgId": 99})
    pump_and_drain(client)
    raw = [t for k, t in stub.prints if k in ("PRINT", "PRINTF")]
    assert DIM + "```rust" + RS in raw, "fence opened on its own message"
    assert "let a = 1;" + RS in raw, \
        "msgId change resets an open fence (line is prose, unindented)"

    # --- the pi_bridge.highlight option gates coloring (structure kept)
    ns["pi_input_cb"]("", "buffer", "!highlight")
    pump_and_drain(client, 0.2)
    assert recv_lines == [], "!highlight must not send anything to pi"
    assert "code highlighting: on" in buffer_text(stub), \
        "!highlight reports the default state"
    ns["pi_input_cb"]("", "buffer", "!highlight off")
    pump_and_drain(client, 0.2)
    assert recv_lines == [], "!highlight off must not send anything to pi"
    assert "code highlighting: off" in buffer_text(stub)
    n0 = len(stub.prints)
    for text in ("```bash", "ls # x", "```"):
        send({"type": "assistant_line", "msgId": 44, "text": text})
    pump_and_drain(client)
    fresh = [t for k, t in stub.prints[n0:] if k in ("PRINT", "PRINTF")]
    assert fresh[1] == "  ls # x", \
        "highlight off: body indented but uncolored"
    assert fresh[2] == DIM + "```" + RS, \
        "fence tracking stays on while highlighting is off"
    ns["pi_input_cb"]("", "buffer", "!highlight on")
    pump_and_drain(client, 0.2)
    assert buffer_text(stub).count("code highlighting: on") >= 1

    # --- the pi_bridge.markdown option + !markdown command
    ns["pi_input_cb"]("", "buffer", "!markdown")
    pump_and_drain(client, 0.2)
    assert recv_lines == [], "!markdown must not send anything to pi"
    assert "markdown rendering: on" in buffer_text(stub), \
        "!markdown reports the default state"
    ns["pi_input_cb"]("", "buffer", "!markdown bogus")
    pump_and_drain(client, 0.2)
    assert "unknown markdown mode: bogus (on | off)" in buffer_text(stub), \
        "invalid mode is rejected"
    assert stub.config_get_plugin("markdown") == "on", \
        "rejected mode leaves the option untouched"
    ns["pi_input_cb"]("", "buffer", "!markdown off")
    pump_and_drain(client, 0.2)
    assert "markdown rendering: off" in buffer_text(stub)
    assert stub.config_get_plugin("markdown") == "off", \
        "!markdown off persists the plugin option"

    # markdown off must reproduce today's rendering path byte for byte,
    # fenced highlighting included (regression guard for the renderer)
    n0 = len(stub.prints)
    for text in ("Here is the command to run:", "```bash",
                 "ls -la # list everything", "```"):
        send({"type": "assistant_line", "msgId": 45, "text": text})
    pump_and_drain(client)
    fresh = [t for k, t in stub.prints[n0:] if k in ("PRINT", "PRINTF")]
    assert fresh == ["Here is the command to run:" + RS,
                     DIM + "```bash" + RS,
                     "  ls -la " + COM + "# list everything" + RS,
                     DIM + "```" + RS], \
        "markdown off reproduces the legacy rendering exactly"

    ns["pi_input_cb"]("", "buffer", "!markdown on")
    pump_and_drain(client, 0.2)
    assert stub.config_get_plugin("markdown") == "on", "!markdown on restores"

    # ==================================================================
    # Phase B3 — markdown block lifecycle (pi_bridge.markdown buffering)
    # ==================================================================

    def md_fresh(n0):
        return [t for k, t in stub.prints[n0:] if k in ("PRINT", "PRINTF")]

    # --- inline spans: emphasis, code, escapes (unit level)
    mdi = ns["_md_inline"]
    BOLD, ITAL, CODE = ns["A_BOLD"], ns["A_ITALIC"], ns["C_CODE"]
    assert mdi("plain text") == ("plain text", None), "no markers, no codes"
    assert mdi("**bold**") == (BOLD + "bold", None)
    assert mdi("__bold__") == (BOLD + "bold", None)
    assert mdi("*italic*") == (ITAL + "italic", None)
    assert mdi("_italic_") == (ITAL + "italic", None)
    nested = mdi("a **bold and *italic* inside** b")
    assert nested == ("a " + BOLD + "bold and " + BOLD + ITAL + "italic" + BOLD +
                      " inside b", None), "nested emphasis re-applies the outer style"
    assert mdi("run `git status` now") == ("run " + CODE + "git status now", None), \
        "inline code: backticks stripped, colored"
    assert mdi("**bold `code` tail**") == (BOLD + "bold " + CODE + "code" + BOLD + " tail",
                                           None), "code inside bold restores bold after it"
    assert mdi("`code with **stars** inside`") == (CODE + "code with **stars** inside", None), \
        "no emphasis inside a code span"
    assert mdi("snake_case_words and a*b*c") == ("snake_case_words and a*b*c", None), \
        "emphasis inside words stays literal"
    assert mdi("**unclosed") == ("**unclosed", None), "unclosed marker stays literal"
    assert mdi("~~strike~~") == ("~~strike~~", None), \
        "WeeChat has no strikethrough attribute: left alone"
    assert mdi("a \\*b \\_c \\`d") == ("a *b _c `d", None), "backslash escapes the marker"

    # emphasis that wraps across the lines of one paragraph
    assert mdi("starts **bold that", "", None, "continues here**") == (
        "starts " + BOLD + "bold that", "**"), "line 1 leaves the span open"
    assert mdi("continues here**", "", "**") == (BOLD + "continues here", None), \
        "line 2 closes it and restores the base style"

    # --- headings (unit level): tiered attributes, markers stripped
    rmb = BRIDGE._render_md_block
    HEAD, UNDER, DIMA = ns["C_HEADING"], ns["A_UNDERLINE"], ns["A_DIM"]
    assert rmb(["# Title"], "heading", True) == [HEAD + BOLD + UNDER + "Title" + RS]
    assert rmb(["## Second"], "heading", True) == [HEAD + BOLD + "Second" + RS]
    assert rmb(["### Third"], "heading", True) == [HEAD + BOLD + DIMA + "Third" + RS]
    assert rmb(["#### Fourth"], "heading", True) == [HEAD + DIMA + "Fourth" + RS]
    assert rmb(["##### Fifth"], "heading", True) == [HEAD + DIMA + ITAL + "Fifth" + RS]
    assert rmb(["###### Sixth"], "heading", True) == [HEAD + DIMA + ITAL + "Sixth" + RS]
    assert ns["_md_block_kind"]("####### Seventh") == "para", \
        "seven hashes is not a heading (CommonMark stops at six)"
    assert ns["_md_block_kind"]("#Heading") == "para", \
        "no space after the hashes: prose, not a heading"
    assert rmb(["## Heading ##"], "heading", True) == [HEAD + BOLD + "Heading" + RS], \
        "the closing run of # is dropped"
    assert rmb(["## Use **care**"], "heading", True) == [
        HEAD + BOLD + "Use " + HEAD + BOLD + BOLD + "care" + HEAD + BOLD + RS], \
        "inline emphasis inside a heading restores the heading style after it"
    assert rmb(["## `code` head"], "heading", True) == [
        HEAD + BOLD + CODE + "code" + HEAD + BOLD + " head" + RS], \
        "inline code inside a heading restores the heading color afterwards"
    # setext: the underline sets the level and is never printed
    assert rmb(["Title", "==="], "setext", True) == [HEAD + BOLD + UNDER + "Title" + RS]
    assert rmb(["Sub title", "---"], "setext", True) == [HEAD + BOLD + "Sub title" + RS]
    assert rmb(["Two", "lines", "==="], "setext", True) == [
        HEAD + BOLD + UNDER + "Two" + RS, HEAD + BOLD + UNDER + "lines" + RS], \
        "a wrapped setext heading keeps its style on every line"
    assert ns["_md_block_kind"]("---") == "hr", \
        "--- with nothing before it is a rule, not an underline"

    # the same rendering reaches the buffer through the wire
    n0 = len(stub.prints)
    for text in ("Use **care** with `rm -rf`:", "", "wrapped **emphasis that",
                 "spans the paragraph**"):
        send({"type": "assistant_line", "msgId": 69, "text": text})
    send({"type": "assistant_flush", "msgId": 69})
    pump_and_drain(client)
    assert md_fresh(n0) == [
        "Use " + BOLD + "care" + " with " + CODE + "rm -rf:" + RS,
        "",
        "wrapped " + BOLD + "emphasis that" + RS,
        BOLD + "spans the paragraph" + RS], \
        "inline spans render in the buffer; wrapped emphasis carries across lines"

    # markdown off: the same text keeps its markers and gets no codes
    ns["pi_input_cb"]("", "buffer", "!markdown off")
    pump_and_drain(client, 0.2)
    n0 = len(stub.prints)
    send({"type": "assistant_line", "msgId": 70, "text": "Use **care** with `rm -rf`:"})
    send({"type": "assistant_flush", "msgId": 70})
    pump_and_drain(client)
    assert md_fresh(n0) == ["Use **care** with `rm -rf`:" + RS], \
        "markdown off prints what pi wrote, markers included"
    ns["pi_input_cb"]("", "buffer", "!markdown on")
    pump_and_drain(client, 0.2)

    # a paragraph is buffered until its blank line, then printed whole
    n0 = len(stub.prints)
    send({"type": "assistant_line", "msgId": 60, "text": "para line one"})
    send({"type": "assistant_line", "msgId": 60, "text": "para line two"})
    pump_and_drain(client)
    assert md_fresh(n0) == [], "an open paragraph is buffered, not printed line by line"
    send({"type": "assistant_line", "msgId": 60, "text": ""})
    pump_and_drain(client)
    assert md_fresh(n0) == ["para line one" + RS, "para line two" + RS], \
        "a blank line completes the block"

    # separators are deferred: the blank prints before the NEXT block, and
    # consecutive blanks collapse into one
    n0 = len(stub.prints)
    send({"type": "assistant_line", "msgId": 61, "text": ""})   # second blank in a row
    send({"type": "assistant_line", "msgId": 61, "text": "after two blanks"})
    send({"type": "assistant_flush", "msgId": 61})
    pump_and_drain(client)
    assert md_fresh(n0) == ["", "after two blanks" + RS], \
        "one deferred separator, none trailing"

    # a tool line while a paragraph is pending: the paragraph keeps its place
    n0 = len(stub.prints)
    send({"type": "assistant_line", "msgId": 62, "text": "text before the tool"})
    send({"type": "tool_start", "toolCallId": "t20", "toolName": "bash",
          "args": {"command": "ls"}})
    pump_and_drain(client)
    fresh = md_fresh(n0)
    assert "text before the tool" in fresh[0] and "⚙" in fresh[1], \
        "pending assistant text prints ahead of the tool line that follows it"

    # a list stays one block, lazy continuation included
    n0 = len(stub.prints)
    for text in ("- one", "- two", "  wrapped continuation", ""):
        send({"type": "assistant_line", "msgId": 63, "text": text})
    pump_and_drain(client)
    assert md_fresh(n0) == ["\u2022 one" + RS, "\u2022 two" + RS,
                            "  wrapped continuation" + RS], \
        "list items and their continuation flush together, without a trailing blank"

    # a heading stands alone, and an opening fence flushes what came before it
    n0 = len(stub.prints)
    for text in ("## Heading", "para before fence", "```cobol", "x = 1", "```"):
        send({"type": "assistant_line", "msgId": 64, "text": text})
    pump_and_drain(client)
    fresh = md_fresh(n0)
    assert fresh[0] == "" and fresh[1] == HEAD + BOLD + "Heading" + RS, \
        "the owed separator precedes the heading, which prints at once"
    assert fresh[2] == "para before fence" + RS
    assert fresh[3] == DIM + "```cobol" + RS, \
        "the paragraph is printed before the fence that ends it"
    assert fresh[4] == "  x = 1", "fence body still streams line by line"

    # assistant_flush is idempotent: a second one prints nothing more
    n0 = len(stub.prints)
    send({"type": "assistant_line", "msgId": 65, "text": "flushed once"})
    send({"type": "assistant_flush", "msgId": 65})
    send({"type": "assistant_flush", "msgId": 65})
    pump_and_drain(client)
    assert md_fresh(n0) == ["flushed once" + RS], \
        "flush is idempotent (no duplicate, no stray separator)"

    # mode switch mid-block: the pending block keeps the mode it started under
    n0 = len(stub.prints)
    send({"type": "assistant_line", "msgId": 66, "text": "pending under on"})
    pump_and_drain(client, 0.2)   # let the bridge read it before the command runs
    ns["pi_input_cb"]("", "buffer", "!markdown off")
    pump_and_drain(client, 0.2)
    fresh = md_fresh(n0)
    assert fresh[0] == "pending under on" + RS, \
        "the pending block prints before the command answer that triggered it"
    assert "markdown rendering: off" in fresh[1]
    ns["pi_input_cb"]("", "buffer", "!markdown on")
    pump_and_drain(client, 0.2)

    # mode switch while a fence is open: fence tracking is untouched
    n0 = len(stub.prints)
    send({"type": "assistant_line", "msgId": 68, "text": "```bash"})
    send({"type": "assistant_line", "msgId": 68, "text": "ls # y"})
    pump_and_drain(client)
    ns["pi_input_cb"]("", "buffer", "!markdown off")
    pump_and_drain(client, 0.2)
    send({"type": "assistant_line", "msgId": 68, "text": "ls # z"})
    send({"type": "assistant_line", "msgId": 68, "text": "```"})
    pump_and_drain(client)
    fresh = md_fresh(n0)
    assert fresh[0] == DIM + "```bash" + RS
    assert fresh[1] == "  ls " + COM + "# y" + RS, "fence body is highlighted as before"
    assert "markdown rendering: off" in fresh[2]
    assert fresh[3] == "  ls " + COM + "# z" + RS, \
        "fence body keeps streaming (and highlighting) after the mode switch"
    assert fresh[4] == DIM + "```" + RS, "the open fence survives a mode switch"
    ns["pi_input_cb"]("", "buffer", "!markdown on")
    pump_and_drain(client, 0.2)

    # ==================================================================
    # Phase C — user_input rate limit (buffer → pi is the LLM-spend path)
    # ==================================================================

    stub.pump(1.1)  # let the previous inputs age out of the 1 s window
    for i in range(6):
        ns["pi_input_cb"]("", "buffer", "rl line %d" % i)
    pump_and_drain(client, 0.5)
    wire = [json.loads(l) for l in recv_lines]
    inputs = [m for m in wire if m.get("type") == "user_input"]
    assert len(inputs) == 5, "max 5 user_input per second, got %d" % len(inputs)
    assert any(m.get("type") == "error" and m.get("code") == "rate_limited"
               for m in wire), "6th input answered with rate_limited"
    assert "input rate limited" in buffer_text(stub), "buffer line for the drop"

    # setext underline vs horizontal rule: decided by what came before
    n0 = len(stub.prints)
    for text in ("Sub title", "---"):
        send({"type": "assistant_line", "msgId": 71, "text": text})
    send({"type": "assistant_flush", "msgId": 71})
    pump_and_drain(client)
    assert md_fresh(n0) == [HEAD + BOLD + "Sub title" + RS], \
        "a --- directly under a paragraph is a setext underline, not a rule"

    n0 = len(stub.prints)
    for text in ("after a blank", "", "---"):
        send({"type": "assistant_line", "msgId": 72, "text": text})
    send({"type": "assistant_flush", "msgId": 72})
    pump_and_drain(client)
    assert md_fresh(n0) == ["after a blank" + RS, "", DIMA + ns["MD_HR"] + RS], \
        "--- after a blank line is a rule, and the paragraph is not a heading"

    # a heading prints at once, and a tool line keeps its place after it
    n0 = len(stub.prints)
    send({"type": "assistant_line", "msgId": 73, "text": "# Next step"})
    send({"type": "tool_start", "toolCallId": "t21", "toolName": "bash",
          "args": {"command": "make"}})
    pump_and_drain(client)
    fresh = md_fresh(n0)
    at = fresh.index(HEAD + BOLD + UNDER + "Next step" + RS)
    assert "⚙" in fresh[at + 1], "the tool line follows the heading, never before it"

    # --- lists (unit level): markers, nesting, hanging continuation
    assert rmb(["- one", "* two", "+ three"], "list", True) == [
        "\u2022 one" + RS, "\u2022 two" + RS, "\u2022 three" + RS], \
        "all three unordered markers become bullets"
    assert rmb(["1. first", "2) second", "10. tenth"], "list", True) == [
        "1. first" + RS, "2) second" + RS, "10. tenth" + RS], \
        "ordered items keep their number and delimiter"
    assert rmb(["- top", "  - nested", "    - deep", "      - deeper"], "list", True) == [
        "\u2022 top" + RS, "  \u25e6 nested" + RS, "    \u25aa deep" + RS,
        "      \u25aa deeper" + RS], "bullets deepen with nesting, then hold"
    assert rmb(["- item", "  wrapped text"], "list", True) == [
        "\u2022 item" + RS, "  wrapped text" + RS], \
        "a marker-less continuation hangs under the item text"
    assert rmb(["10. item", "    wrapped text"], "list", True) == [
        "10. item" + RS, "    wrapped text" + RS], \
        "the hanging indent follows the width of the number column"
    assert rmb(["  - nested", "    wrapped"], "list", True) == [
        "  \u25e6 nested" + RS, "    wrapped" + RS], \
        "continuation keeps the nested item's own indent"
    assert rmb(["- use **care** here"], "list", True) == [
        "\u2022 use " + BOLD + "care" + " here" + RS], \
        "inline emphasis renders inside a list item"
    assert rmb(["- starts **bold that", "  continues here**"], "list", True) == [
        "\u2022 starts " + BOLD + "bold that" + RS,
        "  " + BOLD + "continues here" + RS], \
        "emphasis opened in an item carries to its continuation line"

    # a list ends at a fence, and a tool line keeps its place after it
    n0 = len(stub.prints)
    for text in ("- first item", "```cobol", "x = 1", "```"):
        send({"type": "assistant_line", "msgId": 74, "text": text})
    pump_and_drain(client)
    fresh = md_fresh(n0)
    assert fresh[0] == "\u2022 first item" + RS
    assert fresh[1] == DIM + "```cobol" + RS, "the list flushes before the fence"

    n0 = len(stub.prints)
    send({"type": "assistant_line", "msgId": 75, "text": "- last item"})
    send({"type": "tool_start", "toolCallId": "t22", "toolName": "bash",
          "args": {"command": "ls"}})
    pump_and_drain(client)
    fresh = md_fresh(n0)
    assert fresh[0] == "\u2022 last item" + RS and "⚙" in fresh[1], \
        "a pending list prints before the tool line that interrupts it"

    # a pending block flushes when the user interrupts it (e.g. !abort)
    n0 = len(stub.prints)
    send({"type": "assistant_line", "msgId": 79, "text": "long paragraph that is"})
    pump_and_drain(client)
    assert md_fresh(n0) == [], "the paragraph is still buffered"
    ns["pi_input_cb"]("", "buffer", "!abort")
    pump_and_drain(client)
    fresh = md_fresh(n0)
    assert fresh[0] == "long paragraph that is" + RS, \
        "the pending paragraph prints before the interruption echo"
    assert any("!abort" in x for x in fresh), "the abort echo follows it"

    # and when the session reports an error mid-block
    n0 = len(stub.prints)
    send({"type": "assistant_line", "msgId": 80, "text": "text before the error"})
    send({"type": "error", "code": "boom", "message": "exploded"})
    pump_and_drain(client)
    fresh = md_fresh(n0)
    assert fresh[0] == "text before the error" + RS
    assert any("boom" in x for x in fresh), "the error line follows the flushed text"

    # --- blockquotes and horizontal rules (unit level)
    BAR, QS = ns["MD_QUOTE_BAR"], ns["MD_QUOTE_STYLE"]
    HR = ns["MD_HR"]
    assert rmb(["> quoted"], "quote", True) == [BAR + QS + "quoted" + RS]
    assert rmb(["> one", "> two"], "quote", True) == [
        BAR + QS + "one" + RS, BAR + QS + "two" + RS], "every quoted line keeps its bar"
    assert rmb(["> outer", ">> inner"], "quote", True) == [
        BAR + QS + "outer" + RS, BAR + BAR + QS + "inner" + RS], \
        "nested quotes print one bar per level"
    assert rmb(["> quoted", "still quoted"], "quote", True) == [
        BAR + QS + "quoted" + RS, BAR + QS + "still quoted" + RS], \
        "an unmarked line continues the quote (CommonMark lazy continuation)"
    assert rmb(["> quoted", "", "> second part"], "quote", True) == [
        BAR + QS + "quoted" + RS, "", BAR + QS + "second part" + RS], \
        "a quote survives the blank between its paragraphs, and never ends with one"
    assert rmb(["> use **care**"], "quote", True) == [
        BAR + QS + "use " + QS + BOLD + "care" + QS + RS], \
        "emphasis inside a quote restores the quote style after it"
    assert rmb(["> - item"], "quote", True) == [BAR + QS + "\u2022 item" + RS], \
        "a list marker inside a quote still renders as a list item"
    for rule in ("---", "***", "___", "- - -"):
        assert rmb([rule], "hr", True) == [DIMA + HR + RS], \
            "%s is a rule of the same fixed length" % rule
        assert ns["_md_block_kind"](rule) == "hr"
    assert len(HR) == 20, "rules are a fixed length: no width is knowable here"

    # a quote ends where a paragraph starts, and rules stand alone
    n0 = len(stub.prints)
    for text in ("> quoted line", "", "plain paragraph"):
        send({"type": "assistant_line", "msgId": 76, "text": text})
    send({"type": "assistant_flush", "msgId": 76})
    pump_and_drain(client)
    assert md_fresh(n0) == [BAR + QS + "quoted line" + RS, "", "plain paragraph" + RS], \
        "after a blank, unquoted prose is a new paragraph, not part of the quote"

    n0 = len(stub.prints)
    for text in ("---", "between", "---"):
        send({"type": "assistant_line", "msgId": 77, "text": text})
    send({"type": "assistant_flush", "msgId": 77})
    pump_and_drain(client)
    assert md_fresh(n0) == [DIMA + HR + RS, HEAD + BOLD + "between" + RS], \
        "the first --- is a rule; between + --- is a setext heading, not a rule"

    n0 = len(stub.prints)
    for text in ("a paragraph", "", "---"):
        send({"type": "assistant_line", "msgId": 78, "text": text})
    send({"type": "assistant_flush", "msgId": 78})
    pump_and_drain(client)
    assert md_fresh(n0) == ["a paragraph" + RS, "", DIMA + HR + RS], \
        "--- after a blank is a rule, at the end of a message as much as at its start"

    # client disconnect → title back to waiting (unix client is done)
    # an unfinished call leaves an entry in the toolCallId→nick map; a new
    # connection can never complete it, so disconnect must drop the whole map
    send({"type": "tool_start", "toolCallId": "t12", "toolName": "bash",
          "args": {"command": "sleep 60"}})
    pump_and_drain(client, 0.2)
    assert BRIDGE.tool_nicks.get("t12") == "bash", "open call remembered by id"

    send({"type": "assistant_line", "msgId": 67, "text": "stranded by disconnect"})
    pump_and_drain(client, 0.2)
    assert not any("stranded by disconnect" in t for k, t in stub.prints), \
        "the block is still pending while the connection is up"

    client.close()
    stub.pump(0.3)
    assert "waiting for pi" in (stub.title or ""), stub.title
    assert BRIDGE.client is None
    assert any("stranded by disconnect" in t for k, t in stub.prints), \
        "disconnect flushes a half-built block instead of losing it"
    assert BRIDGE._md_block == [] and BRIDGE._md_block_type is None, \
        "disconnect resets the block accumulator"
    assert BRIDGE.tool_nicks == {}, "disconnect must clear the toolCallId→nick map"
    assert BRIDGE.timing_snapshot is None, "disconnect must discard the stale snapshot"

    # ==================================================================
    # Phase D — TCP listener
    # ==================================================================

    port1 = free_port()
    port2 = free_port()
    recv_lines = []  # fresh wire log for the TCP phase

    # start the listener via the live config path (no /python reload)
    stub.config_set_plugin("tcp_listen", "127.0.0.1:%d" % port1)
    stub.pump(0.1)
    text = buffer_text(stub)
    assert "listening on tcp 127.0.0.1:%d" % port1 in text, \
        "listening line from the real bound address"
    assert "WITHOUT authentication" in text, \
        "warning: tcp_listen set with an empty token"
    assert "pi_tcp_listen_cb" in stub.fd_hooks, "tcp listen fd hooked"

    # --- anonymous TCP handshake (no token configured)
    tc = tcp_connect(port1)
    send_line(tc, {"type": "hello", "protocol": ns["PROTOCOL"], "name": "pi"})
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    hello = json.loads(lines[0])
    assert hello["type"] == "hello" and hello["protocol"] == ns["PROTOCOL"], \
        "anonymous hello accepted without a challenge"
    tc.close()
    stub.pump(0.3)
    assert BRIDGE.client is None, "tcp client dropped on close"

    # --- challenge handshake with a token
    TOKEN = "test-token-123"
    stub.config_set_plugin("token", TOKEN)
    stub.pump(0.1)

    tc = tcp_connect(port1)
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    assert lines, "server must speak first when a token is set"
    challenge = json.loads(lines[0])
    assert challenge["type"] == "challenge" and len(challenge["nonce"]) == 64, \
        "first server message is a challenge with a 256-bit nonce"
    send_line(tc, {"type": "hello", "protocol": ns["PROTOCOL"], "name": "pi",
                   "proof": proof_for(TOKEN, challenge["nonce"])})
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    hello = json.loads(lines[0])
    assert hello["type"] == "hello" and hello["protocol"] == ns["PROTOCOL"], \
        "server hello only after a valid proof"
    assert "pi connected from 127.0.0.1" in buffer_text(stub), \
        "connect line names the TCP peer IP"

    # ping/pong proves the dispatch path is live over TCP
    send_line(tc, {"type": "ping", "ts": 7})
    pump_and_drain(tc, 0.3)
    assert json.loads(recv_lines.pop(0)) == {"type": "pong", "ts": 7}

    # --- per-event read cap: a >256 KiB burst is split across events
    blob_lines = 0
    blob = b""
    while len(blob) < 600 * 1024:
        blob += (json.dumps({"type": "assistant_line", "msgId": blob_lines,
                             "text": "burst-" + "b" * 190}) + "\n").encode()
        blob_lines += 1
    sent = 0
    while sent < len(blob):
        try:
            sent += tc.send(blob[sent:sent + 65536])
        except BlockingIOError:
            pass
        stub.pump(0.02)
    send_line(tc, {"type": "assistant_flush", "msgId": blob_lines})  # settle the last block
    stub.pump(1.5)
    rendered = sum(1 for k, t in stub.prints if "burst-" in t)
    assert rendered == blob_lines, \
        "burst split across events, all %d lines rendered (got %d)" % (
            blob_lines, rendered)
    send_line(tc, {"type": "ping", "ts": 8})
    pump_and_drain(tc, 0.3)
    assert json.loads(recv_lines.pop(0)) == {"type": "pong", "ts": 8}, \
        "connection alive after the burst"
    tc.close()
    stub.pump(0.3)

    # --- slow hello within the deadline still connects
    ns["AUTH_TIMEOUT_S"] = 2.0
    tc = tcp_connect(port1)
    stub.pump(0.4)  # challenge arrives, but no hello yet
    lines = []
    drain(tc, lines)
    assert lines and json.loads(lines[0])["type"] == "challenge"
    send_line(tc, {"type": "hello", "protocol": ns["PROTOCOL"], "name": "pi",
                   "proof": proof_for(TOKEN, json.loads(lines[0])["nonce"])})
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    assert lines and json.loads(lines[0])["type"] == "hello", \
        "slow (but in-deadline) hello must authenticate"
    tc.close()
    stub.pump(0.3)
    ns["AUTH_TIMEOUT_S"] = 10

    # --- wrong token ⇒ auth_failed + drop + red buffer line
    tc = tcp_connect(port1)
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    challenge = json.loads(lines[0])
    send_line(tc, {"type": "hello", "protocol": ns["PROTOCOL"], "name": "pi",
                   "proof": proof_for("wrong-token", challenge["nonce"])})
    pump_and_drain(tc, 0.4)
    assert json.loads(recv_lines[-1]) == {"type": "error", "code": "auth_failed"}
    assert "auth failed from" in buffer_text(stub), "red auth_failed line"
    stub.pump(0.3)
    assert BRIDGE.client is None and BRIDGE.pending == []

    # --- missing proof ⇒ auth_failed too
    tc = tcp_connect(port1)
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    send_line(tc, {"type": "hello", "protocol": ns["PROTOCOL"], "name": "pi"})
    pump_and_drain(tc, 0.4)
    assert json.loads(recv_lines[-1]) == {"type": "error", "code": "auth_failed"}
    tc.close()
    stub.pump(0.3)

    # --- non-hello before auth is ignored (drop silently, no crash)
    tc = tcp_connect(port1)
    send_line(tc, {"type": "user_input", "text": "pre-auth injection"})
    send_line(tc, {"type": "assistant_line", "msgId": 99, "text": "pre-auth junk"})
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    assert lines and json.loads(lines[0])["type"] == "challenge", \
        "challenge still first, pre-auth bytes ignored"
    send_line(tc, {"type": "hello", "protocol": ns["PROTOCOL"], "name": "pi",
                   "proof": proof_for(TOKEN, json.loads(lines[0])["nonce"])})
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    assert lines and json.loads(lines[0])["type"] == "hello", \
        "handshake succeeds after pre-auth garbage"
    text = buffer_text(stub)
    assert "pre-auth junk" not in text, "pre-auth messages must not render"
    tc.close()
    stub.pump(0.3)

    # --- no token configured ⇒ anonymous hello accepted (TCP too)
    stub.config_set_plugin("token", "")
    stub.pump(0.1)
    tc = tcp_connect(port1)
    send_line(tc, {"type": "hello", "protocol": ns["PROTOCOL"], "name": "pi"})
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    assert lines and json.loads(lines[0])["type"] == "hello", \
        "no challenge when the token is empty"
    tc.close()
    stub.pump(0.3)

    # --- broken sec reference ⇒ loud warning, no crash
    stub.config_set_plugin("token", "${sec.data.pi_weechat_token}")
    stub.pump(0.1)
    text = buffer_text(stub)
    assert "${sec.data.pi_weechat_token}" in text and "not expanded" in text, \
        "warning for the unexpanded sec reference"

    # --- allowed_ips gate (independent of the token)
    stub.config_set_plugin("allowed_ips", "^9\\.9\\.9\\.")
    stub.pump(0.1)
    tc = tcp_connect(port1)
    stub.pump(0.4)
    assert drain(tc, []) is False, "non-matching peer IP closed before handshake"
    tc = tcp_connect(port1)  # also: zero bytes on the wire (no challenge)
    stub.pump(0.4)
    assert drain(tc, []) is False
    stub.config_set_plugin("allowed_ips", "^127\\.")
    stub.pump(0.1)
    tc = tcp_connect(port1)
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    assert lines and json.loads(lines[0])["type"] == "challenge", \
        "matching peer IP passes the gate"
    # the broken-reference value is enforced as a literal token
    send_line(tc, {"type": "hello", "protocol": ns["PROTOCOL"], "name": "pi",
                   "proof": proof_for("${sec.data.pi_weechat_token}",
                                      json.loads(lines[0])["nonce"])})
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    assert lines and json.loads(lines[0])["type"] == "hello"
    tc.close()
    stub.pump(0.3)
    stub.config_set_plugin("allowed_ips", "")
    stub.pump(0.1)

    # --- live rebind via pi_config_cb: old listener gone, new one bound
    stub.config_set_plugin("tcp_listen", "127.0.0.1:%d" % port2)
    stub.pump(0.2)
    text = buffer_text(stub)
    assert "listening on tcp 127.0.0.1:%d" % port2 in text, "new listening line"
    try:
        old = tcp_connect(port1)
        old.close()
        raise AssertionError("old port must be closed after rebind")
    except (ConnectionRefusedError, OSError):
        pass
    tc = tcp_connect(port2)
    stub.pump(0.3)
    lines = []
    drain(tc, lines)
    assert lines and json.loads(lines[0])["type"] == "challenge"
    tc.close()
    stub.pump(0.3)

    # --- per-IP failure lockout (silent: no oracle for scanners)
    ns["FAIL_MAX"] = 2
    BRIDGE.ip_failures.clear()  # the negative tests above already logged some
    BRIDGE.ip_lockouts.clear()
    failed_line_count = lambda: sum(
        1 for k, t in stub.prints if "auth failed from" in t)
    failed_before = failed_line_count()
    stub.config_set_plugin("token", "tok-3")
    stub.pump(0.1)
    for i in range(3):  # FAIL_MAX + 1 failures from 127.0.0.1
        tc = tcp_connect(port2)
        stub.pump(0.3)
        lines = []
        drain(tc, lines)
        challenge = json.loads(lines[0])
        send_line(tc, {"type": "hello", "protocol": ns["PROTOCOL"], "name": "pi",
                       "proof": proof_for("bad", challenge["nonce"])})
        pump_and_drain(tc, 0.3)
        assert json.loads(recv_lines[-1]) == {"type": "error",
                                              "code": "auth_failed"}
        tc.close()
        stub.pump(0.2)
    assert failed_line_count() == failed_before + 3, \
        "3 failures, 3 new lines (got %d)" % (failed_line_count() - failed_before)
    tc = tcp_connect(port2)
    stub.pump(0.5)
    assert drain(tc, []) is False, "locked-out IP closed silently"
    stub.pump(0.2)
    assert failed_line_count() == failed_before + 3, \
        "lockout is silent: no new buffer line, no bytes"

    # --- stop the listener
    stub.config_set_plugin("tcp_listen", "")
    stub.pump(0.1)
    assert "tcp listener stopped" in buffer_text(stub)
    assert "pi_tcp_listen_cb" not in stub.fd_hooks, "tcp listen fd unhooked"

    print("smoke weechat: OK (%d buffer lines rendered)" % len(stub.prints))


if __name__ == "__main__":
    main()
