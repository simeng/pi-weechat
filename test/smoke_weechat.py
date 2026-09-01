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
        self.timers = {}          # handle -> {cb, data, deadline, timeout, remain}
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
                "lightblue": "L", "lightgreen": "g"}.get(name, "")

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

    def hook_timer(self, timeout, remain, synchro, cb, data):
        tid = "timer-%d" % (len(self.timers) + 1)
        self.timers[tid] = {
            "cb": cb, "data": data,
            "deadline": time.time() + max(timeout, 1) / 1000.0,
            "timeout": max(timeout, 1) / 1000.0,
            "remain": remain,
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
                    if t["remain"] == 1:
                        del self.timers[tid]
                    else:
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
    assert "bash" in text and "ls" in text, "tool start missing"
    assert "a.txt" in text and "b.txt" in text, "tool output missing"
    assert "long_term remembered fact" in text, \
        "memory_write args must show target + content"
    assert "…(+105)" in text, \
        "long bash commands must be clipped (405 - 300 = 105 more chars)"
    assert re.match(r"^π: %s \(thinking… \d+s\)$" % re.escape(proj), stub.title), stub.title
    # turn settle (busy → idle) ⇒ one extra highlight line below the last
    # message line (left untouched); idle → idle is not a settle
    send({"type": "status", "state": "idle"})
    pump_and_drain(client, 0.2)
    assert re.match(r"^π: %s \(idle \d+s\)$" % re.escape(proj), stub.title), stub.title
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
    # Phase B3 — buffer-title turn counter (live while busy, frozen on settle)
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

    # live counter: with request_at set, the 1s tick refreshes the title in
    # both the thinking and tool states
    BRIDGE.request_at = time.time() - 90
    BRIDGE.state = "thinking"
    ns["pi_tick_cb"]("", 0)
    m = re.search(r"\(thinking… (\d+)s\)", stub.title)
    assert m and 90 <= int(m.group(1)) <= 91, stub.title
    BRIDGE.state = "tool:bash"
    ns["pi_tick_cb"]("", 0)
    m = re.search(r"\(tool: bash (\d+)s\)", stub.title)
    assert m and 90 <= int(m.group(1)) <= 91, stub.title

    # settle via the wire: thinking → idle freezes the counter; it persists
    # across further ticks (no live clock) and a new request restarts it
    BRIDGE.request_at = time.time() - 3
    BRIDGE.state = "thinking"
    send({"type": "status", "state": "idle"})
    pump_and_drain(client, 0.2)
    m = re.search(r"\(idle (\d+)s\)", stub.title)
    assert m and 3 <= int(m.group(1)) <= 5, stub.title
    frozen_title = stub.title
    ns["pi_tick_cb"]("", 0)
    ns["pi_tick_cb"]("", 0)
    assert stub.title == frozen_title, "frozen counter must persist across ticks"
    # a new request restarts the live clock from 0
    BRIDGE._mark_request()
    BRIDGE.state = "thinking"
    ns["pi_tick_cb"]("", 0)
    assert re.search(r"\(thinking… 0s\)", stub.title), stub.title

    # no request ever → ticks leave the title counter-free
    BRIDGE.request_at = None
    BRIDGE.frozen = None
    BRIDGE.state = "idle"
    BRIDGE.set_state("idle")
    assert stub.title == "π: %s (idle)" % proj, stub.title
    ns["pi_tick_cb"]("", 0)
    assert stub.title == "π: %s (idle)" % proj, stub.title

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
    assert re.match(r"^π: %s \(idle \d+s\) — awaiting !pick$" % re.escape(proj), stub.title), stub.title

    # !pick by number → ui_response with the option text; title hint clears
    ns["pi_input_cb"]("", "buffer", "!pick 2")
    pump_and_drain(client, 0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "ui_response", "id": 7,
                   "value": "/opt/beta"}, msg
    assert BRIDGE.pending_ui is None
    assert re.match(r"^π: %s \(idle \d+s\)$" % re.escape(proj), stub.title), stub.title

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
    # Nick prefixes — role-based rendering (irc.server_default.nicks)
    # ==================================================================

    RS = "0"  # the stub's reset marker (real WeeChat: color:reset)
    # the buffer localvar carries the user nick (first config entry);
    # user lines render in the prefix column under the real nick
    assert stub.localvars.get("nick") == "alice", \
        "buffer localvar nick = first irc.server_default.nicks entry"

    # pi-originated lines render under the 'pi' nick (prnt_date_tags,
    # tag prefix_nick_chat_nick, `pi` before the TAB): assistant prose,
    # fences, tool lines, tool output body
    pi_rows = [(p, b) for tags, p, b in stub.printf_tags if tags == "notify_none,prefix_nick_chat_nick"]
    assert all("pi" in p for p, _ in pi_rows), \
        "pi lines carry `pi` as the line prefix"
    pi_lines = [b for _, b in pi_rows]
    assert "Hello from pi" + RS in pi_lines, \
        "assistant prose via prnt_date_tags with the pi prefix"
    assert any(t.startswith("M⚙ bash") for t in pi_lines), \
        "tool_start under the pi nick"
    assert any(t.startswith("G✔") for t in pi_lines), \
        "tool_end under the pi nick"
    assert "B  a.txt" + RS in pi_lines, "tool output body under the pi nick"
    assert any(t.startswith("C\U0001F4AD ") for t in pi_lines), \
        "thinking lines under the pi nick"

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

    # client disconnect → title back to waiting (unix client is done)
    client.close()
    stub.pump(0.3)
    assert "waiting for pi" in (stub.title or ""), stub.title
    assert BRIDGE.client is None

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
