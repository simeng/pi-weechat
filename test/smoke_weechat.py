#!/usr/bin/env python3
"""In-process smoke test for weechat/pi_bridge.py.

Runs the real script against a stub `weechat` module and drives it with a
real (in-process) Unix-socket client playing the role of the pi extension.
Verifies: handshake, output rendering into the buffer, title updates, and
user input being sent on the wire in the right message shape.

Run: python3 test/smoke_weechat.py   (no dependencies beyond stdlib)
"""
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
        self.fd_hooks = {}        # callback name -> [fd, read, write]
        self.buffer_name = None
        self.registered = None
        self.plugin_opts = {}     # config_*_plugin storage

    # -- colors / plugin options -----------------------------------------
    def color(self, name):
        # tests run without a WeeChat display: binary codes are empty strings
        return ""

    def config_is_set_plugin(self, name):
        return name in self.plugin_opts

    def config_get_plugin(self, name):
        return self.plugin_opts.get(name, "")

    def config_set_plugin(self, name, value):
        self.plugin_opts[name] = value
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
        return 1

    def buffer_get_string(self, buf, prop):
        return ""

    def prnt(self, buf, msg):
        self.prints.append(("PRINT", msg))
        return 1

    # -- hooks -----------------------------------------------------------
    def hook_fd(self, fd, fr, fw, fe, cb, data):
        self.fd_hooks[cb] = [fd, fr, fw]
        return "hook:" + cb

    def unhook(self, hook):
        cb = hook.split(":", 1)[1]
        self.fd_hooks.pop(cb, None)
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
            live = {n: h for n, h in self.fd_hooks.items()
                    if h[1] and h[0] >= 0 and self._fd_open(h[0])}
            # a hooked fd that was closed underneath → weechat calls the cb
            # once with fd == -1, then unhook; emulate that
            for name, hook in list(self.fd_hooks.items()):
                if hook[1] and hook[0] >= 0 and not self._fd_open(hook[0]):
                    ns[name]("", -1)
                    self.unhook("hook:" + name)
            read_fds = [h[0] for h in live.values()]
            r, _, _ = select.select(read_fds, [], [], 0.02) if read_fds else ([], [], [])
            progressed = False
            for fd in r:
                for name, hook in list(self.fd_hooks.items()):
                    if hook[0] == fd and hook[1]:
                        ns[name]("", fd)
                        progressed = True
                        break
            for name, hook in list(self.fd_hooks.items()):
                if hook[2] and hook[0] >= 0:
                    ns[name]("", hook[0])
                    progressed = True
            if not progressed:
                time.sleep(0.01)


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

    assert stub.buffer_name == "pi", "buffer 'pi' must be created on load"
    assert os.path.exists(sock_path), "socket must be listening"
    assert oct(os.stat(sock_path).st_mode & 0o777) == "0o700", "socket perms 0700"

    # ------------------------------------------------- fake pi client side
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(sock_path)
    client.setblocking(False)
    send = lambda obj: client.sendall((json.dumps(obj) + "\n").encode())
    recv_lines = []

    def pump_and_drain(seconds=0.3):
        stub.pump(seconds)
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    break
                recv_lines.extend(l for l in data.decode().split("\n") if l.strip())
        except BlockingIOError:
            pass

    pump_and_drain()
    hello = json.loads(recv_lines.pop(0))
    assert hello["type"] == "hello" and hello["protocol"] == 1, hello

    # pi → weechat: mirror output into the buffer
    send({"type": "hello", "protocol": 1, "name": "pi"})
    send({"type": "session_info", "cwd": "/home/x/proj", "model": "prov/model-a"})
    send({"type": "status", "state": "thinking"})
    send({"type": "assistant_line", "msgId": 1, "text": "Hello from pi"})
    send({"type": "tool_start", "toolCallId": "t1", "toolName": "bash",
          "args": {"command": "ls"}})
    send({"type": "tool_end", "toolCallId": "t1", "isError": False,
          "output": "a.txt\nb.txt"})
    pump_and_drain()

    def plain():
        # literal replacement (a regex like color:[a-z]+ would eat the word
        # right after an untagged boundary, e.g. "color:cansession:")
        text = "\n".join(t for k, t in stub.prints)
        for tag in ("white", "default", "blue", "cyan", "red", "green",
                    "gray", "reset"):
            text = text.replace("color:" + tag, "")
        return text

    text = plain()
    assert "session: /home/x/proj prov/model-a" in text, "session line missing"
    assert "Hello from pi" in text, "assistant line missing"
    assert "bash" in text and "ls" in text, "tool start missing"
    assert "a.txt" in text and "b.txt" in text, "tool output missing"
    assert stub.title == "pi: (thinking…)", stub.title

    # weechat → pi: user input over the wire
    ns["pi_input_cb"]("", "buffer", "hello from weechat")
    pump_and_drain(0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "user_input", "text": "hello from weechat"}, msg

    # steer prefix maps to deliverAs
    ns["pi_input_cb"]("", "buffer", "!s do this instead")
    pump_and_drain(0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "user_input", "text": "do this instead",
                   "deliverAs": "steer"}, msg

    # control command routing
    ns["pi_input_cb"]("", "buffer", "!new")
    pump_and_drain(0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "command", "name": "new_session"}, msg

    # echo of typed lines appears in the buffer
    echo_text = plain()
    assert "> hello from weechat" in echo_text
    assert "> !s do this instead" in echo_text

    # ping → pong
    send({"type": "ping", "ts": 123})
    pump_and_drain(0.4)
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
    pump_and_drain(0.5)
    assert any("still alive" in t for k, t in stub.prints), "connection lost after oversize"

    # tool output filtering: 'off' hides the body, 'full' shows it again
    ns["pi_input_cb"]("", "buffer", "!tools off")
    pump_and_drain(0.2)
    assert recv_lines == [], "!tools must not send anything to pi"
    send({"type": "tool_end", "toolCallId": "t2", "isError": False,
          "output": "hidden line"})
    pump_and_drain(0.3)
    text = plain()
    assert "hidden line" not in text, "tool_output=off must hide the body"
    ns["pi_input_cb"]("", "buffer", "!tools summary")
    pump_and_drain(0.2)
    many = "\n".join("l%d" % i for i in range(10))
    send({"type": "tool_end", "toolCallId": "t3", "isError": False,
          "output": many})
    pump_and_drain(0.3)
    text = plain()
    assert "l0" in text and "l9" in text, "summary keeps first/last lines"
    assert "more lines" in text, "summary elides the middle"

    # thinking lines are hidden by default; !think on enables them
    send({"type": "thinking_line", "msgId": 3, "text": "hidden thought"})
    pump_and_drain(0.3)
    text = plain()
    assert "hidden thought" not in text, "thinking must be hidden by default"
    ns["pi_input_cb"]("", "buffer", "!think on")
    pump_and_drain(0.2)
    assert recv_lines == [], "!think must not send anything to pi"
    text = plain()
    assert "thinking: on" in text, "!think on confirms the mode"
    send({"type": "thinking_line", "msgId": 3, "text": "visible thought"})
    pump_and_drain(0.3)
    text = plain()
    assert "visible thought" in text, "!think on must render thinking lines"

    # !help is answered locally (nothing hits the wire)
    ns["pi_input_cb"]("", "buffer", "!help")
    pump_and_drain(0.3)
    assert recv_lines == [], "!help must not send anything to pi"
    text = plain()
    assert "steer" in text and "!tools" in text and "!think" in text, \
        "!help prints the command list"

    # !model <provider/id> → command with arg
    ns["pi_input_cb"]("", "buffer", "!model prov/model-b")
    pump_and_drain(0.4)
    msg = json.loads(recv_lines.pop(0))
    assert msg == {"type": "command", "name": "model", "arg": "prov/model-b"}, msg

    # client disconnect → title back to waiting
    client.close()
    stub.pump(0.3)
    assert "waiting for pi" in (stub.title or ""), stub.title

    print("smoke weechat: OK (%d buffer lines rendered)" % len(stub.prints))


if __name__ == "__main__":
    main()
