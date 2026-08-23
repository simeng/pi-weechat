#!/usr/bin/env python3
"""Stdin/stdout driver for the cross-language integration test.

Loads the REAL weechat/pi_bridge.py against a stub weechat module, pumps its
event loop, and talks to the node test over stdin/stdout:

  stdin  lines:  {"op":"input","text":...}   simulate user typing in buffer
                 {"op":"set","name":...,"value":...}  set a pi_bridge option
                 {"op":"quit"}               shut down
  stdout lines:  {"type":"print","text":...,"tags":...}  every line rendered in the buffer
                 {"type":"title","text":...} buffer title changes
                 {"type":"ready","socket":...}
"""
import json
import os
import select
import sys
import traceback

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

    real_pdt = stub.prnt_date_tags
    def prnt_date_tags(buf, date, tags, message):
        r = real_pdt(buf, date, tags, message)
        _, prefix, body = stub.printf_tags[-1]
        sys.stdout.write(json.dumps({"type": "print", "text": body,
                                     "tags": tags, "prefix": prefix}) + "\n")
        sys.stdout.flush()
        return r
    stub.prnt_date_tags = prnt_date_tags

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

    # interleaved pump: socket fds + stdin.
    # NOTE: stdin is read with os.read (unbuffered). Mixing select() with
    # sys.stdin.readline() loses lines: readline() reads a whole chunk from
    # the pipe into Python's userspace buffer, so a second op that arrived
    # in the same chunk is invisible to select() and never gets processed.
    stdin_buf = b""
    quit = False
    while not quit:
        try:
            r, _, _ = select.select([0], [], [], 0.05)
        except (OSError, ValueError):
            break
        if 0 in r:
            try:
                data = os.read(0, 65536)
            except OSError:
                data = b""
            if not data:  # EOF
                break
            stdin_buf += data
            while b"\n" in stdin_buf:
                raw, stdin_buf = stdin_buf.split(b"\n", 1)
                try:
                    op = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if op.get("op") == "input":
                    ns["pi_input_cb"]("", "buffer", op.get("text", ""))
                elif op.get("op") == "set":
                    # config_set_plugin fires the hook_config callbacks, as
                    # in real WeeChat (this is how tests start the TCP listener)
                    stub.config_set_plugin(op["name"], op.get("value", ""))
                elif op.get("op") == "quit":
                    quit = True
                    break
        try:
            stub.pump(0.05)
        except Exception:
            sys.stderr.write("py_driver: pump exception:\n" + traceback.format_exc())
            sys.stderr.flush()
            raise

    BRIDGE = ns.get("BRIDGE")
    if BRIDGE is not None:
        BRIDGE.cleanup()


def BRIDGE_PATH(ns):
    return ns["BRIDGE"].sock_path


if __name__ == "__main__":
    main()
