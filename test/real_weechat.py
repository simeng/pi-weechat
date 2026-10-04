#!/usr/bin/env python3
"""End-to-end nick rendering check inside a REAL WeeChat.

smoke_weechat.py drives pi_bridge.py through a stubbed `weechat` module; this
drives it through an actual `weechat-headless` process and asserts on the lines
WeeChat itself stored in the buffer — tags, the nick column, and the per-nick
colors. Two runs:

  1. default options (thinking on)  -> auto mode: tool lines under the tool's own nick
  2. plugins.conf with pi_bridge.nicks = pi, thinking = on
                                     -> legacy mode: every pi-side line under `pi`

Skips (exit 0) when weechat-headless is not installed.

  python3 test/real_weechat.py
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(REPO, "weechat", "pi_bridge.py")

# Loaded by WeeChat (autoload), not by us: it records every line WeeChat prints
# — tags, the raw prefix (with its color escapes) and the raw message — and
# reports the nick colors WeeChat would assign.
CAPTURE_SHIM = '''
import json, os, time
import weechat

CAPTURE = os.environ["PI_WEECHAT_CAPTURE"]
weechat.register("nick_capture", "tor", "1.0", "MIT", "capture", "", "")


def on_print(*args):
    """WeeChat 4.10 hands hook_print eight arguments:
    (pointer, buffer, date, tags, notification, ?, prefix, message)."""
    try:
        buffer = weechat.buffer_get_string(args[1], "full_name")
    except Exception:
        buffer = "?"
    try:
        with open(CAPTURE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "buffer": buffer,
                "tags": args[3] or "",
                "prefix": weechat.string_remove_color(args[6], ""),
                "prefix_raw": args[6] or "",
                "message": weechat.string_remove_color(args[7], ""),
                "message_raw": args[7] or "",
            }) + "\\n")
    except OSError:
        pass
    return weechat.WEECHAT_RC_OK


weechat.hook_print("", "", "", 0, "on_print", "")


# Read-only lookup, after register(): the colors WeeChat would give these nicks.
with open(CAPTURE + ".colors", "w", encoding="utf-8") as f:
    json.dump({nick: weechat.info_get("nick_color_name", nick)
               for nick in ("pi", "read", "bash", "think", "memory_search")}, f)


'''


def mkroot(name: str) -> str:
    """A plain /tmp path: WeeChat does not load its autoload scripts when HOME
    is a macOS /var/folders symlink."""
    root = os.path.join("/tmp", "pi-weechat-real-" + name)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    return os.path.realpath(root)


def run_weechat(root: str, seed_conf: str | None,
                mid: callable | None = None,
                script: callable | None = None) -> tuple[list[dict], dict]:
    """Boot a real headless WeeChat with pi_bridge autoloaded, drive it as a
    client would, and return the lines WeeChat printed plus its nick colors."""
    home = os.path.join(root, "home")
    sock = os.path.join(root, "pi.sock")
    capture = os.path.join(root, "capture.jsonl")
    autoload = os.path.join(home, "python", "autoload")
    os.makedirs(autoload, exist_ok=True)
    shutil.copy(BRIDGE, os.path.join(autoload, "pi_bridge.py"))
    with open(os.path.join(autoload, "nick_capture.py"), "w", encoding="utf-8") as f:
        f.write(CAPTURE_SHIM)
    if seed_conf:
        with open(os.path.join(home, "plugins.conf"), "w", encoding="utf-8") as f:
            f.write(seed_conf)

    env = dict(os.environ, HOME=home, PI_WEECHAT_SOCK=sock, PI_WEECHAT_CAPTURE=capture)
    proc = subprocess.Popen(["weechat-headless", "-d", home], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        if os.path.exists(sock):
            break
        time.sleep(0.25)
    else:
        proc.kill()
        raise RuntimeError("pi_bridge never created its socket")

    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(5)
    c.connect(sock)
    c.sendall(json.dumps({"type": "hello", "protocol": 3}).encode() + b"\n")
    c.recv(4096)

    def send(msg: dict) -> None:
        c.sendall(json.dumps(msg).encode() + b"\n")

    if script:
        script(send, capture)
        if mid:
            # toggle markdown off from the WeeChat side and print one more line
            fifo = next(f for f in os.listdir(home) if f.startswith("weechat_fifo_"))
            mid(os.path.join(home, fifo))
            send({"type": "assistant_line", "msgId": 5,
                  "text": "## after markdown was switched off"})
            time.sleep(1.0)
        send({"type": "assistant_line", "msgId": 4, "text": "Bye."})
        time.sleep(1.5)
        c.close()
        os.kill(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait()
        rows = [json.loads(line) for line in open(capture, encoding="utf-8")]
        return [r for r in rows if r["buffer"] == "python.pi"], {}

    send({"type": "session_info", "sessionId": "s1", "sessionFile": "/tmp/s.jsonl",
          "model": "opencode/gpt-5", "cwd": "/tmp/proj", "contextWindow": 200000,
          "tools": ["read", "bash"]})
    send({"type": "assistant_line", "msgId": 1, "text": "Reading the hosts file now."})
    send({"type": "thinking_line", "msgId": 1, "text": "thinking about hosts"})
    send({"type": "tool_start", "toolCallId": "c1", "toolName": "read",
          "args": {"path": "/etc/hosts"}})
    send({"type": "tool_end", "toolCallId": "c1", "isError": False,
          "output": "  127.0.0.1 localhost\n  ::1 ip6-localhost"})
    send({"type": "tool_start", "toolCallId": "c2", "toolName": "bash",
          "args": {"command": "uname -sr"}})
    send({"type": "tool_end", "toolCallId": "c2", "isError": True, "output": "exit 1"})
    send({"type": "tool_start", "toolCallId": "c3", "toolName": "memory_search",
          "args": {"query": "nick"}})
    send({"type": "tool_end", "toolCallId": "c3", "isError": False, "output": "1 match"})
    send({"type": "assistant_line", "msgId": 2, "text": "Done."})
    time.sleep(1.0)

    if mid:
        # Change the mode from the WeeChat side, exactly as a user would: a
        # /set on the fifo. Everything after this must render the new way.
        fifo = next(f for f in os.listdir(home) if f.startswith("weechat_fifo_"))
        mid(os.path.join(home, fifo))
        send({"type": "assistant_line", "msgId": 3, "text": "after the switch"})
        send({"type": "tool_start", "toolCallId": "c4", "toolName": "grep",
              "args": {"pattern": "nick"}})
        send({"type": "tool_end", "toolCallId": "c4", "isError": False, "output": "2 matches"})
        time.sleep(1.0)

    send({"type": "assistant_line", "msgId": 4, "text": "Bye."})
    time.sleep(1.5)
    c.close()

    os.kill(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()

    rows = [json.loads(line) for line in open(capture, encoding="utf-8")]
    colors_file = capture + ".colors"
    colors = json.load(open(colors_file, encoding="utf-8")) if os.path.exists(colors_file) else {}
    return [r for r in rows if r["buffer"] == "python.pi"], colors


def has_nick(row: dict, nick: str) -> bool:
    return "nick_" + nick in row["tags"].split(",")


def check(label: str, ok: bool) -> bool:
    print(("PASS  " if ok else "FAIL  ") + label)
    return ok


def dump(pi: list[dict]) -> None:
    for r in pi:
        print("  tags=%-42s prefix=%-14s body=%r" % (r["tags"], r["prefix"], r["message"][:48]))


def main() -> int:
    if not shutil.which("weechat-headless"):
        print("SKIP: weechat-headless not installed")
        return 0

    results: list[bool] = []

    # --- auto mode (default) -------------------------------------------------
    root = mkroot("auto")
    try:
        pi, colors = run_weechat(root, '[var]\npython.pi_bridge.thinking = "on"\n')
        print("=== auto mode ===")
        dump(pi)
        read = [r for r in pi if has_nick(r, "read")]
        bash = [r for r in pi if has_nick(r, "bash")]
        mem = [r for r in pi if has_nick(r, "memory_search")]
        think = [r for r in pi if has_nick(r, "think")]
        results.append(check("assistant line under nick pi", any(has_nick(r, "pi") for r in pi)))
        results.append(check("read call, result and output all under nick read", len(read) >= 3))
        results.append(check("bash call under nick bash", any("uname" in r["message"] for r in bash)))
        results.append(check("tool_end reuses the tool's nick (toolName is not on the wire)",
                             any("exit 1" in r["message"] for r in bash)))
        results.append(check("custom tool gets its own nick", len(mem) >= 2))
        results.append(check("thinking line under its own nick think",
                             any("thinking about hosts" in r["message"] for r in think)))
        results.append(check("auto mode drops the tool name from the body",
                             not any("⚙ read" in r["message"] or "⚙ bash" in r["message"] for r in pi)))
        results.append(check("status lines keep no nick",
                             any(r["prefix"] == "" and "connected" in r["message"] for r in pi)))
        results.append(check("nick tags are nick_<name> + prefix_nick_chat_nick",
                             all(r["tags"] == "notify_none,nick_read,prefix_nick_chat_nick" for r in read)))
        results.append(check("nick column carries an inline color (tags alone do not color it)",
                             all("\x19" in r["prefix_raw"] for r in read)))
        results.append(check("different tools get different nick colors",
                             read[0]["prefix_raw"] != bash[0]["prefix_raw"]))
        results.append(check("WeeChat resolves a color for every nick we print",
                             all(colors.get(n) for n in ("pi", "read", "bash", "think", "memory_search"))))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- legacy mode: pi_bridge.nicks = pi -----------------------------------
    root = mkroot("legacy")
    try:
        pi, _ = run_weechat(root, '[var]\npython.pi_bridge.nicks = "pi"\npython.pi_bridge.thinking = "on"\n')
        print("\n=== legacy mode (pi_bridge.nicks = pi) ===")
        dump(pi)
        results.append(check("no per-tool nick tags",
                             not any(has_nick(r, "read") or has_nick(r, "bash") for r in pi)))
        results.append(check("every pi-side line prefixed with the single nick pi",
                             sum("pi" in r["prefix_raw"] for r in pi) >= 5))
        results.append(check("legacy tags are exactly notify_none,prefix_nick_chat_nick",
                             all(r["tags"] == "notify_none,prefix_nick_chat_nick"
                                 for r in pi if "pi" in r["prefix_raw"])))
        results.append(check("tool name stays in the body", any("⚙ bash" in r["message"] for r in pi)))
        results.append(check("thinking renders under the pi nick",
                             any("thinking about hosts" in r["message"] and "pi" in r["prefix_raw"] for r in pi)))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- switching modes live, from WeeChat, with /set -----------------------
    root = mkroot("switch")

    def switch(fifo: str) -> None:
        with open(fifo, "w") as f:
            f.write("core.weechat */set plugins.var.python.pi_bridge.nicks pi\n")

    try:
        pi, _ = run_weechat(root, '[var]\npython.pi_bridge.nicks = "auto"\n', mid=switch)
        print("\n=== /set plugins.var.python.pi_bridge.nicks pi mid-session ===")
        dump(pi)
        cut = next(i for i, r in enumerate(pi) if "after the switch" in r["message"])
        before, after = pi[:cut], pi[cut:]
        results.append(check("before the switch: tool lines under the tool nick",
                             any(has_nick(r, "read") for r in before)))
        results.append(check("after the switch: no per-tool nick tags",
                             not any(has_nick(r, "grep") or has_nick(r, "read") for r in after)))
        results.append(check("after the switch: everything under the single nick pi",
                             all("pi" in r["prefix_raw"] for r in after if r["prefix_raw"])))
        results.append(check("after the switch: tool name back in the body",
                             any("⚙ grep" in r["message"] for r in after)))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- markdown rendering (pi_bridge.markdown = on by default) -----------
    root = mkroot("markdown")

    def md_script(send, capture_path) -> None:
        for text in [
            "# Markdown heading",
            "Some **bold**, some *italic*, and `inline code` together.",
            "One ~~struck~~ line.",
            "",
            "- first item",
            "  lazy continuation of the first item",
            "- second item",
            "",
            "> quoted line",
            ">> nested quote",
            "",
            "---",
            "## Heading with `code` and **bold**",
            "A long paragraph line " * 12,
        ]:
            send({"type": "assistant_line", "msgId": 10, "text": text})
        send({"type": "assistant_flush", "msgId": 10})
        # bounded wait for the bridge to render the block before the /set
        # below: the FIFO command is processed immediately, so a fixed sleep
        # here is a race, not a guarantee
        deadline = time.time() + 10
        while time.time() < deadline:
            if os.path.exists(capture_path):
                data = open(capture_path, encoding="utf-8").read()
                if "Markdown heading" in data and "# Markdown" not in data:
                    return
            time.sleep(0.2)

    def markdown_off(fifo: str) -> None:
        with open(fifo, "w") as f:
            f.write("core.weechat */set plugins.var.python.pi_bridge.markdown off\n")

    try:
        pi, _ = run_weechat(root, '[var]\npython.pi_bridge.markdown = "on"\n',
                            mid=markdown_off, script=md_script)
        print("\n=== markdown rendering ===")
        dump(pi)

        def find(sub):
            return [r for r in pi if sub in r["message"]]

        head = find("Markdown heading")
        emph = find("Some bold, some italic, and inline code together.")
        quote = find("quoted line")
        nested = find("nested quote")
        rules = [r for r in pi if r["message"].startswith("\u2500\u2500")]
        long = [r for r in pi if r["message"].startswith("A long paragraph line")]
        raw_after_off = find("after markdown was switched off")
        results.append(check("ATX heading: markers stripped, bold + underline + color stored",
                             len(head) == 1 and "#" not in head[0]["message"]
                             and "\x1a\x01" in head[0]["message_raw"]
                             and "\x1a\x04" in head[0]["message_raw"]
                             and "\x19" in head[0]["message_raw"]))
        results.append(check("emphasis and inline code render, their markers are gone",
                             len(emph) == 1 and "**" not in emph[0]["message"]
                             and "`" not in emph[0]["message"]))
        results.append(check("bold, italic and color all survive in one stored line",
                             len(emph) == 1 and "\x1a\x01" in emph[0]["message_raw"]
                             and "\x1a\x03" in emph[0]["message_raw"]
                             and "\x19" in emph[0]["message_raw"]))
        struck = [r for r in pi if "\u0336" in r["message"]]
        results.append(check("strikethrough renders as the combining stroke overlay",
                             len(struck) == 1
                             and struck[0]["message"].startswith("One ")
                             and "~~" not in struck[0]["message"]))
        results.append(check("list markers become bullets and the continuation hangs under them",
                             any(r["message"].startswith("\u2022 first item") for r in pi)
                             and any(r["message"].startswith("  lazy continuation") for r in pi)))
        results.append(check("blockquote gets a bar, a nested quote gets two",
                             len(quote) == 1 and quote[0]["message"].startswith("\u2502 ")
                             and len(nested) == 1 and nested[0]["message"].startswith("\u2502 \u2502 ")))
        results.append(check("horizontal rule renders as a box-drawing line", len(rules) == 1))
        results.append(check("a long line stays one buffer line (client wraps it, not the bridge)",
                             len(long) == 1 and len(long[0]["message"]) > 200))
        results.append(check("markdown lines keep the pi nick prefix",
                             all(has_nick(r, "pi") for r in head + emph + quote + nested)))
        results.append(check("/set plugins.var.python.pi_bridge.markdown off switches rendering live",
                             len(raw_after_off) == 1
                             and "## after markdown was switched off" in raw_after_off[0]["message"]))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n%s (%d/%d checks)" % ("OK" if all(results) else "FAIL",
                                   sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
