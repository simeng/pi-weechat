#!/usr/bin/env python3
"""Stdin/stdout driver for the cross-language integration test.

Loads the REAL weechat/pi_bridge.py against a stub weechat module, pumps its
event loop, and talks to the node test over stdin/stdout:

  stdin  lines:  {"op":"input","text":...}   simulate user typing in buffer
                 {"op":"quit"}               shut down
  stdout lines:  {"type":"print","text":...} every line rendered in the buffer
                 {"type":"title","text":...} buffer title changes
                 {"type":"ready","socket":...}
"""
import json
import os
import select
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from smoke_weechat import WeechatStub  # noqa: E402


def main():
    stub = WeechatStub()
    ns = {}
    stub.ns = ns
    sys.modules["weechat"] = stub

    real_prnt = stub.prnt
    def prnt(buf, msg):
        real_prnt(buf, msg)
        sys.stdout.write(json.dumps({"type": "print", "text": msg}) + "\n")
        sys.stdout.flush()
        return 1
    stub.prnt = prnt

    real_set = stub.buffer_set
    def buffer_set(b, prop, value):
        r = real_set(b, prop, value)
        if prop == "title":
            sys.stdout.write(json.dumps({"type": "title", "text": value}) + "\n")
            sys.stdout.flush()
        return r
    stub.buffer_set = buffer_set

    with open(os.path.join(HERE, "..", "weechat", "pi_bridge.py")) as f:
        code = f.read()
    exec(compile(code, "pi_bridge.py", "exec"), ns)

    sys.stdout.write(json.dumps({"type": "ready", "socket": BRIDGE_PATH(ns)}) + "\n")
    sys.stdout.flush()

    # interleaved pump: socket fds + stdin
    while True:
        extra = [0] if True else []
        try:
            r, _, _ = select.select([0], [], [], 0.05)
        except (OSError, ValueError):
            break
        if 0 in r:
            line = sys.stdin.readline()
            if not line:
                break
            try:
                op = json.loads(line)
            except ValueError:
                continue
            if op.get("op") == "input":
                ns["pi_input_cb"]("", "buffer", op.get("text", ""))
            elif op.get("op") == "quit":
                break
        stub.pump(0.05)

    BRIDGE = ns.get("BRIDGE")
    if BRIDGE is not None:
        BRIDGE.cleanup()


def BRIDGE_PATH(ns):
    return ns["BRIDGE"].sock_path


if __name__ == "__main__":
    main()
