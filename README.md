# pi-weechat

Chat with a [pi](https://pi.dev) coding agent from inside WeeChat.

- **WeeChat script** (`weechat/pi_bridge.py`) — creates a `pi` buffer, acts as
  the Unix-socket server.
- **pi package** (`extensions/weechat-bridge.ts`) — connects to that socket,
  mirrors pi's streaming output/tool calls/status into the buffer, and sends
  lines typed in the buffer back to pi as user input.

See [PLAN.md](PLAN.md) for the full architecture and protocol design.

## Status

Planned — implementation tracked in PLAN.md §8 (M0–M4).

## Install (planned)

```bash
# weechat script
cp weechat/pi_bridge.py ~/.local/share/weechat/python/   # then /reload or restart weechat

# pi package
pi install /path/to/pi-weechat
```
