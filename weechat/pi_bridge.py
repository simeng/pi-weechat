# -*- coding: utf-8 -*-
###
# pi_bridge.py — mirror a pi coding agent session through a WeeChat buffer.
#
# Creates a "pi" buffer that acts as both display and input for a pi session.
# This script is the SERVER; the pi extension (see
# extensions/weechat-bridge.ts) connects to it. Wire format: NDJSON, see
# PLAN.md §2.

# Rendering is role-based: pi-originated lines render under a nick that names
# *who* spoke — the tool name (`read`, `bash`, …) on tool lines, `think` on
# thinking lines, `pi` on assistant text (pi_bridge.nicks = auto; `pi` restores
# the single `pi` nick) — each colored with WeeChat's own per-nick color.
# User lines (buffer input, pi-terminal echoes) render under the user's IRC
# nick — first entry of irc.server_default.nicks, re-applied live via
# hook_config; system lines stay prefix-less (channel-notice style).
#
# Transports (one client at a time, across both):
#   * Unix socket — always on. Path: $PI_WEECHAT_SOCK, else
#     $XDG_RUNTIME_DIR/pi-weechat.sock, else ~/.local/state/pi-weechat/.
#   * TCP — opt-in via `/set plugins.var.python.pi_bridge.tcp_listen host:port` (live rebind,
#     no reload). For remote pi; the TCP listener mirrors the design of
#     WeeChat's own urlserver.py (blocking listen fd, one accept() per
#     event, SO_REUSEADDR, listen(5)).
#
# Auth (protocol 3, shared secret — the token never crosses the wire):
#   when `pi_bridge.token` is set, EVERY client (TCP and Unix) is first
#   sent {"type":"challenge","nonce":<random hex>} and must answer its
#   hello with proof = hex(HMAC-SHA256(key=token, msg=nonce)). Wrong or
#   missing proof → error{auth_failed} + drop. Empty token ⇒ anonymous
#   (current behavior). Recommended: store the secret in sec.conf
#   (/secure set pi_weechat_token …) and set the option to
#   ${sec.data.pi_weechat_token}.
#
# Interactive prompts (protocol 3): pi can ask the buffer for input with
#   ui_request {id, method: "select"|"input", title, options?/placeholder?};
# the user answers with `!pick <n>` (comma list / exact text) or
# `!pick <text>` (input) or `!pick cancel`; this script sends back
#   ui_response {id, value | cancelled}.
#
# Install: copy into ~/.local/share/weechat/python/ and start WeeChat, or
#   /python load pi_bridge
# Reload after changes: /python reload pi_bridge
###

import hashlib
import hmac
import json
import os
import re
import secrets
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

PROTOCOL = 3
MAX_LINE = 1024 * 1024  # must match MAX_LINE_BYTES in lib/codec.mjs

# ------------------------------------------------- abuse-resistance thresholds
# Module constants so tests can override them; a production install keeps the
# defaults. Rationale in README ("Threat model") and PLAN §6.
AUTH_TIMEOUT_S = 10            # no valid hello within 10 s of accept → drop
MAX_PENDING_UNAUTH = 3         # max accepted-but-not-authed connections
FAIL_MAX = 5                   # auth failures per source IP within the window…
FAIL_WINDOW_S = 60             # …that trigger a lockout
LOCKOUT_S = 600                # …for this long (silent drop, no oracle)
MAX_BYTES_PER_EVENT = 256 * 1024  # read budget per client fd event
USER_INPUT_MAX_PER_S = 5       # buffer lines forwarded to pi per sliding 1 s


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
C_NICK = _theme("chat_nick", "white")                 # the `pi` nick in the prefix column
C_PI = _theme("chat", "")                           # assistant text (default fg)
C_TOOL = _theme("chat_prefix_network", "magenta")   # tool activity lines
C_STATUS = _theme("chat_value", "cyan")             # info lines (session, modes)
C_ERR = _theme("chat_prefix_error", "yellow")       # errors
C_OK = _theme("chat_status_enabled", "green")       # successes
C_DIM = _theme("chat_host", "cyan")                  # hints / 💭 thinking lines
C_REJECT = _color("red")                             # auth failures / security
# Tool *output* body: own color, deliberately a fixed palette color (no
# canonical WeeChat [color] slot for this role; theme-following risks
# collapsing back onto chat_host/cyan and looking identical to thinking).
C_TOOL_OUT = _color("blue")
R = _color("reset")

# Text attributes (WeeChat's own codes; verified on 4.10.1: bold \x1a\x01,
# italic \x1a\x03, underline \x1a\x04, reverse \x1a\x02, dim \x1a\x06). They
# survive the relay and Glowing Bear maps them to .a-b/.a-i/.a-u/.a-r/.a-d.
# A \x19 color code RESETS attributes in WeeChat's renderer, so a style is
# always built as color-then-attributes and re-applied in full after a span.
A_BOLD = _color("bold") or "\x1a\x01"
A_ITALIC = _color("italic") or "\x1a\x03"
A_UNDERLINE = _color("underline") or "\x1a\x04"
A_DIM = _color("dim") or "\x1a\x06"
A_REVERSE = _color("reverse") or "\x1a\x02"
# Inline code: fixed palette yellow (spike decision — no chat_code slot exists
# in 4.10.1, and yellow collides with neither cyan thinking nor blue output).
C_CODE = _color("yellow")
# Headings: WeeChat has no chat_heading color slot, so headings use one fixed
# accent (magenta — the same structural accent as tool activity, never prose),
# tiered by level: deeper levels get fewer attributes.
C_HEADING = _color("magenta")
# Color first, attributes after: a \x19 color code resets attributes, so every
# style prefix is built in this order and re-applied in full after a span.
MD_HEADING_LEVELS = {
    1: A_BOLD + A_UNDERLINE,
    2: A_BOLD,
    3: A_BOLD + A_DIM,
    4: A_DIM,
    5: A_DIM + A_ITALIC,
    6: A_DIM + A_ITALIC,
}


def _nick_color(nick):
    """Inline color for a nick prefix: WeeChat's own per-nick assignment.

    info_get("nick_color_name", nick) hashes the nick against
    weechat.color.chat_nick_colors (honoring look.nick_color_hash and
    look.nick_color_force). The name is turned into a color code because tags
    alone do NOT color a nick printed by a script — the color has to ride in
    the text. Falls back to C_NICK (theme chat_nick) when the lookup is
    unavailable (older WeeChat, or the test stub).
    """
    if weechat is None:
        return C_NICK
    try:
        name = weechat.info_get("nick_color_name", nick) or ""
    except Exception:
        name = ""
    return _color(name) or C_NICK

# --------------------------------------------- syntax highlighting (fences)

# Fenced code blocks in assistant markdown are tokenized line by line and
# colored inline with WeeChat binary color codes. Supported languages live in
# HL_LANGS (aliases in HL_ALIAS); unknown info strings fall back to plain
# text — the fence is still tracked so its body stays indented and its
# closer is found. The tokenizer is deliberately simple (ordered regex
# alternation, single-line patterns): it colors pi's output, not IDE-grade
# editing.

# token → fixed palette color. Fixed (not theme-following) on purpose: these
# roles have no canonical weechat.conf [color] slot, and theme colors would
# collapse onto the chat/toolbar palette (same rationale as C_TOOL_OUT).
HL_TOKENS = {
    "kw":   _color("bold magenta"),  # keywords, @-rules, html tags, <?php …
    "str":  _color("green"),         # string literals, attribute values
    "num":  _color("cyan"),          # numbers, hex colors
    "com":  _color("darkgray"),      # comments
    "fn":   _color("lightblue"),     # function calls
    "var":  _color("yellow"),        # $vars, css properties, yaml/json keys, html attrs
    "type": _color("lightgreen"),    # rust primitive types
}

# Per-language rules: ordered (token, pattern) pairs. The FIRST alternative
# matching at a position wins, so comments and strings come before keywords.
# Patterns are single-line; block comments that may span lines are listed in
# HL_BLOCK_COMMENTS and their state is carried per fence in a ctx dict.
HL_LANGS = {
    "bash": (
        ("com",  r"(?:(?<=\s)|(?<=[;|&(])|^)#.*$"),
        ("str",  r'"(?:[^"\\]|\\.)*"?'),
        ("str",  r"'(?:[^'\\]|\\.)*'?"),
        ("var",  r"\$(?:\{[^}]*\}|[A-Za-z0-9_*?#@!]+)"),
        ("kw",   r"\b(?:if|then|else|elif|fi|for|in|do|done|case|esac|function|while|until|select|time)\b"),
        ("var",  r"^[A-Za-z_]\w*(?==)"),
        ("num",  r"\b\d+(?:\.\d+)?\b"),
    ),
    "rust": (
        ("com",  r"//.*$"),
        ("com",  r"/\*(?:.*?\*/|.*$)"),
        ("str",  r'r#*"(?:[^"\\]|\\.)*"?'),
        ("str",  r"'(?:\\.|[^'\\])'"),
        ("kw",   r"\b(?:let|mut|fn|struct|enum|impl|trait|use|mod|pub|match|if|else|for|while|loop|return|const|static|where|move|async|await|dyn|ref|self|Self|super|crate|as|in|break|continue|unsafe|extern|type)\b"),
        ("type", r"\b(?:i8|i16|i32|i64|i128|isize|u8|u16|u32|u64|u128|usize|bool|str|f32|f64|char)\b"),
        ("fn",   r"[A-Za-z_]\w*(?=\s*[(\[!])"),
        ("num",  r"\b(?:0[xXoObB][0-9a-fA-F_]+|\d[\d_]*(?:\.[\d_]+)?)\b"),
    ),
    "css": (
        ("com",  r"/\*(?:.*?\*/|.*$)"),
        ("kw",   r"@[\w-]+"),
        ("str",  r'"[^"\n]*"?|\'[^\'\n]*\'?'),
        ("num",  r"#[0-9a-fA-F]{3,8}\b"),
        ("var",  r"[a-zA-Z-]+(?=\s*:(?![:/]))"),
        ("fn",   r"[a-zA-Z-]+(?=\()"),
        ("num",  r"\b\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|vmin|vmax|s|ms|deg|fr|ch)?"),
    ),
    "html": (
        ("com",  r"<!--(?:.*?-->|.*$)"),
        ("kw",   r"<!\s*(?i:doctype)[^>]*>"),
        ("kw",   r"</?[a-zA-Z][\w:-]*"),
        ("var",  r"[a-zA-Z-]+(?=\s*=)"),
        # double quotes only — single-quote "strings" would eat apostrophes
        # in the visible text between tags
        ("str",  r'"[^"\n]*"?'),
    ),
    "php": (
        ("com",  r"//.*$"),
        ("com",  r"#.*$"),
        ("com",  r"/\*(?:.*?\*/|.*$)"),
        ("kw",   r"<\?(?i:php)?|<\?=|\?>"),
        ("str",  r'"(?:[^"\\]|\\.)*"?'),
        ("str",  r"'[^'\n]*'?"),
        ("var",  r"\$\w+"),
        ("kw",   r"\b(?i:echo|print|function|fn|return|if|else|elseif|endif|for|foreach|while|do|switch|case|break|continue|class|interface|trait|extends|implements|namespace|use|new|as|array|list|public|private|protected|static|const|try|catch|finally|throw|match|global|require|require_once|include|include_once|exit|die|isset|empty|unset|typeof|instanceof|null|true|false|void|int|string|bool|float)\b"),
        ("fn",   r"[a-zA-Z_]\w*(?=\s*\()"),
        ("num",  r"\b\d+(?:\.\d+)?\b"),
    ),
    "python": (
        ("com",  r"#.*$"),
        ("kw",   r"^[ \t]*@[A-Za-z_][\w.]*"),
        ("str",  r'[rbufRBUF]{0,2}""".*?(?:"""|$)'),
        ("str",  r"[rbufRBUF]{0,2}'''.*?(?:'''|$)"),
        ("str",  r"[rbufRBUF]{0,2}'(?:[^'\\]|\\.)*'?"),
        ("str",  r'[rbufRBUF]{0,2}"(?:[^"\\]|\\.)*"?'),
        ("kw",   r"\b(?:def|class|if|elif|else|for|while|return|import|from|as|with|try|except|finally|lambda|pass|break|continue|global|nonlocal|yield|async|await|in|is|not|and|or|None|True|False|raise|assert|del)\b"),
        ("fn",   r"[A-Za-z_]\w*(?=\s*\()"),
        ("num",  r"\b(?:0[xXoObB][0-9a-fA-F_]+|\d[\d_]*(?:\.[\d_]+)?)[jJ]?\b"),
    ),
    "json": (
        ("var",  r'"(?:[^"\\]|\\.)*"(?=\s*:)'),
        ("kw",   r"\b(?:true|false|null)\b"),
        ("num",  r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b"),
        ("str",  r'"(?:[^"\\]|\\.)*"?'),
    ),
    "yaml": (
        ("com",  r"(?:(?<=\s)|(?<=[-#])|^)#.*$"),
        ("var",  r"^[ \t]*[^ \t#][^:#]*(?=\s*:)"),
        ("kw",   r"\b(?:true|false|null|yes|no|on|off)\b|(?:(?<=\s)|^)~(?=\s|$)"),
        ("str",  r'"[^"\n]*"?|\'[^\'\n]*\'?'),
        ("num",  r"-?\b\d+(?:\.\d+)?\b"),
    ),
}

# fence info word → language (lower-cased before lookup)
HL_ALIAS = {
    "bash": "bash", "sh": "bash", "shell": "bash", "zsh": "bash",
    "rust": "rust", "rs": "rust", "rustc": "rust",
    "css": "css",
    "html": "html", "htm": "html", "xml": "html", "svg": "html", "xhtml": "html",
    "php": "php",
    "python": "python", "py": "python", "python3": "python",
    "json": "json",
    "yaml": "yaml", "yml": "yaml",
}

# languages whose block comments may span lines: (open, close) delimiters
HL_BLOCK_COMMENTS = {
    "rust": ("/*", "*/"),
    "css":  ("/*", "*/"),
    "php":  ("/*", "*/"),
    "html": ("<!--", "-->"),
}


def _hl_compile(langs):
    """One combined regex per language: ordered named-group alternation."""
    compiled, groups = {}, {}
    for name, rules in langs.items():
        parts, gmap = [], {}
        for i, (token, pattern) in enumerate(rules):
            gid = "%s%d" % (token, i)
            gmap[gid] = token
            parts.append("(?P<%s>%s)" % (gid, pattern))
        compiled[name] = re.compile("|".join(parts))
        groups[name] = gmap
    return compiled, groups


_HL_COMPILED, _HL_GROUPS = _hl_compile(HL_LANGS)


def highlight_code(line, lang, ctx):
    """Tokenize + color one line of fenced code (embedded binary colors).

    `lang` is the fence info word (e.g. "bash"); unknown languages and empty
    lines pass through unchanged. `ctx` (dict) carries block-comment state
    across lines within one fence and is mutated in place.
    """
    canon = HL_ALIAS.get(lang.lower()) if lang else None
    if canon is None or not line:
        return line
    spec = HL_BLOCK_COMMENTS.get(canon)
    if ctx.get("com"):
        close = spec[1]
        idx = line.find(close)
        if idx < 0:
            return HL_TOKENS["com"] + line + R
        ctx["com"] = False
        head = HL_TOKENS["com"] + line[:idx + len(close)] + R
        rest = line[idx + len(close):]
        return head + (highlight_code(rest, lang, ctx) if rest else "")
    out = []
    pos = 0
    for m in _HL_COMPILED[canon].finditer(line):
        if m.start() > pos:
            out.append(line[pos:m.start()])
        token = _HL_GROUPS[canon][m.lastgroup]
        if (token == "com" and spec
                and m.group(0).startswith(spec[0])
                and spec[1] not in m.group(0)):
            ctx["com"] = True  # block comment continues on the next line
        out.append(HL_TOKENS[token] + m.group(0) + R)
        pos = m.end()
    if pos < len(line):
        out.append(line[pos:])
    return "".join(out)


# Markdown fences: ``` or ~~~, 3+ markers (CommonMark allows up to 3 leading
# spaces; LLMs indent fences deeper inside list items, so be lenient).
FENCE_OPEN_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")


def _fence_open(line):
    """Parse an opening fence line → fence dict (with ctx + close regex), or None."""
    m = FENCE_OPEN_RE.match(line)
    if not m:
        return None
    marker, rest = m.group(2), m.group(3)
    if marker[0] == "`" and "`" in rest:
        return None  # backticks are not allowed in the info string
    words = rest.split()
    return {
        "lang": words[0] if words else "",
        "ctx": {},
        "close": re.compile(r"^\s*%s{%d,}\s*$"
                            % (re.escape(marker[0]), len(marker))),
    }


def _fence_closed(fence, line):
    return bool(fence["close"].match(line))


# Markdown block classification (see pi_bridge.markdown). CommonMark allows up
# to three leading spaces before a block marker; LLM output rarely uses more.
MD_ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:\s|$)")
MD_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)\s*$")
MD_HR_RE = re.compile(r"^ {0,3}(?:(?:[-*_])\s*){3,}$")
MD_LIST_RE = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:\s+\S|\s*$)")
MD_QUOTE_RE = re.compile(r"^ {0,3}>")

MD_BLOCK_PARA = "para"
MD_BLOCK_LIST = "list"
MD_BLOCK_QUOTE = "quote"
MD_BLOCK_HEADING = "heading"
MD_BLOCK_HR = "hr"
MD_BLOCK_SETEXT = "setext"


def _md_block_kind(line):
    """Classify a markdown line: None (blank) | heading | hr | list | quote | para.

    A line that starts with `*` or `_` is emphasis, not a list item or a rule:
    MD_LIST_RE needs whitespace after the marker, MD_HR_RE needs three markers.
    """
    if not line.strip():
        return None
    if MD_ATX_RE.match(line):
        return MD_BLOCK_HEADING
    if MD_HR_RE.match(line):
        return MD_BLOCK_HR
    if MD_LIST_RE.match(line):
        return MD_BLOCK_LIST
    if MD_QUOTE_RE.match(line):
        return MD_BLOCK_QUOTE
    return MD_BLOCK_PARA


def _md_setext_level(line):
    """Setext underline → heading level (=== is 1, --- is 2), or None.

    Only the line right after a paragraph is an underline; the same `---` with
    nothing before it is a horizontal rule (the caller decides which).
    """
    m = MD_SETEXT_RE.match(line)
    if not m:
        return None
    return 1 if m.group(1)[0] == "=" else 2


MD_ATX_TEXT_RE = re.compile(r"^ {0,3}(#{1,6})(?:\s|$)(.*?)\s*#*\s*$")


def _md_atx(line):
    """ATX heading → (level, text), or None when it is not one.

    `#Heading` (no space after the hashes) is not a heading in CommonMark and
    stays prose; the optional closing run of `#` is dropped.
    """
    m = MD_ATX_TEXT_RE.match(line)
    if not m:
        return None
    return len(m.group(1)), m.group(2).strip()


def _md_heading_style(level):
    """Style prefix for a heading level: color codes then attribute codes."""
    return C_HEADING + MD_HEADING_LEVELS.get(level, MD_HEADING_LEVELS[6])


# Lists. Indent depth is read from the marker column (2 spaces per level, as
# CommonMark counts it); bullets deepen • → ◦ → ▪.
MD_LIST_UL_RE = re.compile(r"^( *)([-*+])\s+(.*)$")
MD_LIST_OL_RE = re.compile(r"^( *)(\d{1,9})([.)])\s+(.*)$")
MD_BULLETS = ("\u2022", "\u25e6", "\u25aa")

# Blockquotes: a │ bar per nesting level and a dimmed body. No color on purpose
# — cyan already means thinking and blue already means tool output.
MD_QUOTE_MARK_RE = re.compile(r"^ *((?:> ?)+)(.*)$")
MD_QUOTE_BAR = "\u2502 "
MD_QUOTE_STYLE = A_DIM
# Horizontal rule: a fixed-length light box-drawing line. v1 does not query
# the window width (no usable width source was found in the spike), and
# padding to a guessed width would wrap in a narrower window.
MD_HR = "\u2500" * 20


def _md_list_items(lines):
    """List block → [(prefix, content)] in print order.

    `prefix` is the exact text printed before the item content: a bullet or a
    number for a marker line, and the same width of spaces for a lazy
    continuation line, so an item's wrapped text hangs under its own text.
    """
    items = []
    hang = ""            # marker width of the item being continued
    for line in lines:
        m = MD_LIST_UL_RE.match(line)
        if m:
            spaces, depth = m.group(1), len(m.group(1)) // 2
            bullet = MD_BULLETS[min(depth, len(MD_BULLETS) - 1)]
            hang = spaces + bullet + " "
            items.append((hang, m.group(3)))
            continue
        m = MD_LIST_OL_RE.match(line)
        if m:
            spaces = m.group(1)
            marker = m.group(2) + m.group(3)
            hang = spaces + marker + " "
            items.append((hang, m.group(4)))
            continue
        # no marker: lazy continuation of the item above it (an item whose
        # wrapped text the model put on its own line) — same prefix, so the
        # text lines up under the item's own text
        if hang:
            items.append((" " * len(hang), line.strip()))
        else:
            items.append(("", line))
    return items


# Inline markdown: emphasis, code, strikethrough. Backslash escapes, and
# markers that do not open a span stay literal.
_MD_ESCAPES = set("\\`*_{}[]()#+-.!>~")
_MD_ATTR = {"**": A_BOLD, "__": A_BOLD, "*": A_ITALIC, "_": A_ITALIC}
# Strikethrough: WeeChat has no attribute for it, so ~~text~~ renders each
# character with a combining long stroke overlay (U+0336). Zero width, so
# wrapping and columns are unaffected; rendering depends on the font.
MD_STRIKE = "\u0336"
MD_STRIKE_PENDING = "~~\x00"      # open ~~ carried to the next line


def _md_word_char(ch):
    return ch.isalnum() or ch == "_"


def _md_mid_word(text, i, length):
    """True when a delimiter sits inside a word: snake_case_words, a*b*c.

    Those stay literal — identifiers and glob-ish text are common in agent
    output and italicising them would corrupt what they say.
    """
    prev = text[i - 1] if i > 0 else ""
    nxt = text[i + length] if i + length < len(text) else ""
    return _md_word_char(prev) and _md_word_char(nxt)


def _md_close(text, start, delim):
    """Index of the closing `delim` at or after start, skipping escapes; -1."""
    i = start
    n = len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n and text[i + 1] in _MD_ESCAPES:
            i += 2
            continue
        if text.startswith(delim, i):
            return i
        i += 1
    return -1


def _md_stroke(text, strike):
    """Overlay the combining stroke on every character, or leave the text."""
    return "".join(ch + MD_STRIKE for ch in text) if strike else text


def _md_inline(text, base="", pending=None, lookahead=None, strike=False):
    """Render the inline markdown of one line → (printable line, still_open).

    `base` is the enclosing style (color codes then attribute codes) that is
    re-applied in full after every span, because a color code drops the
    attributes that were active before it. `pending` is emphasis left open by
    the previous line of the same block; the returned `still_open` is what the
    next line inherits. A marker only stays open across a line break when
    `lookahead` (the rest of the block) actually closes it — otherwise it is
    printed literally, which is what markdown does with `**unclosed`.
    `strike` marks text inside an open ~~span~~: every printed character gets
    the combining stroke overlay, style codes never do.
    """
    out = []

    if pending:
        delim = pending.replace("\x00", "")
        strike = strike or "\x00" in pending
        # emphasis keeps its attribute while open; a strike span has none
        # (the overlay rides on the characters themselves)
        style = base if strike else base + _MD_ATTR[delim]
        close = _md_close(text, 0, delim)
        if close < 0:
            # the span still runs on: the whole line belongs to it
            inner = _md_inline(text, style, None, None, strike)[0]
            return style + inner, pending
        inner = _md_inline(text[:close], style, None, None, strike)[0]
        out.append(style + inner + base)
        text = text[close + len(delim):]

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in _MD_ESCAPES:
            out.append(_md_stroke(text[i + 1], strike))
            i += 2
            continue
        if ch == "`":
            j = i
            while j < n and text[j] == "`":
                j += 1
            fence = text[i:j]
            close = text.find(fence, j)
            if close < 0:
                out.append(fence)     # unbalanced backticks stay literal
                i = j
                continue
            # color first, then the outer style again: inline code must not
            # swallow the bold/italic it sits inside
            out.append(C_CODE + _md_stroke(text[j:close], strike) + base)
            i = close + len(fence)
            continue
        two = text[i:i + 2]
        if two == "~~":
            close = _md_close(text, i + 2, "~~")
            if close >= 0:
                inner = _md_inline(text[i + 2:close], base, None, None, True)[0]
                out.append(base + inner + base)
                i = close + 2
                continue
            if lookahead is not None and _md_close(lookahead, 0, "~~") >= 0:
                # the rest of this line is inside the new span: stroke it
                tail = _md_stroke(text[i + 2:], True)
                return "".join(out) + tail, MD_STRIKE_PENDING
            out.append(two)           # literal: consume both tildes
            i += 2
            continue
        if two in ("**", "__") and not _md_mid_word(text, i, 2):
            close = _md_close(text, i + 2, two)
            if close >= 0:
                inner = _md_inline(text[i + 2:close], base + _MD_ATTR[two],
                                   None, None, strike)[0]
                out.append(base + _MD_ATTR[two] + inner + base)
                i = close + 2
                continue
            if lookahead is not None and _md_close(lookahead, 0, two) >= 0:
                return ("".join(out) + base + _MD_ATTR[two] + text[i + 2:], two)
            # not an emphasis opener: consume BOTH markers, so the second half
            # of `**unclosed` is never re-read as a lone `*`
            out.append(two)
            i += 2
            continue
        if ch in "*_" and not _md_mid_word(text, i, 1):
            close = _md_close(text, i + 1, ch)
            if close >= 0:
                inner = _md_inline(text[i + 1:close], base + _MD_ATTR[ch],
                                   None, None, strike)[0]
                out.append(base + _MD_ATTR[ch] + inner + base)
                i = close + 1
                continue
            if lookahead is not None and _md_close(lookahead, 0, ch) >= 0:
                return ("".join(out) + base + _MD_ATTR[ch] + text[i + 1:], ch)
        out.append(_md_stroke(ch, strike))
        i += 1
    return "".join(out), None


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

# Syntax highlighting of fenced code blocks (pi_bridge.highlight option)
HIGHLIGHT_MODES = ("on", "off")
DEFAULT_HIGHLIGHT = "on"

# Markdown rendering of pi's replies (pi_bridge.markdown option): on = render
# headings/emphasis/lists/quotes into WeeChat color+attribute codes, off = print
# the text exactly as pi wrote it.
MARKDOWN_MODES = ("on", "off")
DEFAULT_MARKDOWN = "on"

# Nick column mode (pi_bridge.nicks option): auto = tool name on tool lines,
# think on thinking lines, pi on replies; pi = every pi-side line under the
# single nick `pi` (the legacy rendering).
NICK_MODES = ("auto", "pi")
DEFAULT_NICKS = "auto"
THINK_NICK = "think"
NICK_MAX_LEN = 32
TOOL_NICK_MAP_MAX = 256  # toolCallId→nick map cap (a turn never needs more)
_NICK_BAD_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _nick_for(raw):
    """Sanitize a nick so it is safe as both prefix field and tag name.

    The nick is the text before the first TAB *and* part of the nick_<name>
    tag, so it must hold no TAB (breaks the prefix split) and no space or
    comma (breaks the tag list). Anything outside [A-Za-z0-9_.-] collapses to
    "_", the result is capped at NICK_MAX_LEN, and blank or non-string input
    (a missing toolName) falls back to `pi`.
    """
    if not isinstance(raw, str):
        return "pi"
    nick = _NICK_BAD_CHARS.sub("_", " ".join(raw.split()).strip())
    return nick[:NICK_MAX_LEN] or "pi"

# Config options: (name, default, description). Defaults are applied on
# first load; descriptions registered via config_set_desc_plugin and shown
# by /help set plugins.var.python.pi_bridge.<name>.
PLUGIN_OPTIONS = (
    ("tcp_listen", "",
     'TCP listener address, "host:port" (e.g. "127.0.0.1:52311"; '
     '"0.0.0.0:port" for LAN/Tailscale). Empty = disabled (Unix socket '
     'only). Change rebinds live, no reload. Requires pi_bridge.token — '
     'without one, TCP clients are accepted unauthenticated.'),
    ("token", "",
     'Shared secret for client auth: every client (TCP and Unix socket) '
     'must answer an HMAC-SHA256 challenge proving knowledge of it. '
     'Empty = anonymous (no auth). Recommended: store the secret with '
     '/secure set pi_weechat_token <secret>, then set this option to '
     '"${sec.data.pi_weechat_token}".'),
    ("allowed_ips", "",
     'Regex that a TCP client\'s source IP must match (search, not '
     'fullmatch); empty = allow all. Not applied to the local Unix '
     'socket. An invalid regex falls back to allow-all.'),
    ("tool_output", DEFAULT_TOOL_OUTPUT,
     'Tool result verbosity: full (all output lines) | summary (first 3 + '
     'last 3 lines, middle elided) | off (hide output).'),
    ("thinking", DEFAULT_THINKING,
     'Show pi\'s thinking lines: on | off.'),
    ("highlight", DEFAULT_HIGHLIGHT,
     'Syntax-highlight fenced code blocks in pi\'s replies: on | off.'),
    ("nicks", DEFAULT_NICKS,
     'Who the nick column names: auto (tool name on tool lines, "think" on '
     'thinking lines, "pi" on replies) | pi (every pi-side line under the '
     'single nick "pi"). Nick colors come from weechat.color.chat_nick_colors.'),
    ("markdown", DEFAULT_MARKDOWN,
     'Render pi’s markdown replies (headings, bold/italic, inline code, lists, '
     'blockquotes, rules) with WeeChat color and attribute codes: on | off. '
     'Blocks are printed when complete, so a paragraph appears at its blank line.'),
)

HELP_TEXT = (
    "!s <text> steer current turn · !q <text> queue follow-up\n"
    "!new new session · !compact compact context · !abort abort current turn\n"
    "!cd <path> switch pi to a project dir (fuzzy match → pick list)\n"
    "!pick <n|text> answer pi’s “?” prompt (!pick cancel aborts)\n"
    "!status refresh session/model info · !model [provider/id] list or set model\n"
    "!tools [full|summary|off] tool output verbosity (default: summary)\n"
    "!think [on|off] show/hide thinking lines (default: off)\n"
    "!highlight [on|off] syntax-highlight fenced code blocks (default: on)\n"
    "!nick [auto|pi] nick column: tool names vs just “pi” (default: auto)\n"
    "!markdown [on|off] render pi's markdown replies (default: on)\n"
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


def _proof(token, nonce):
    """Shared-secret proof: hex(HMAC-SHA256(key=token, msg=nonce)).

    The token is never transmitted — only this one-way derivation, bound to
    a fresh per-connection nonce (a captured proof is useless on the next
    connection). The pi extension computes the same value with node:crypto.
    """
    return hmac.new(token.encode("utf-8"),
                    str(nonce).encode("ascii"),
                    hashlib.sha256).hexdigest()


def _parse_tcp_listen(raw):
    """Parse the pi_bridge.tcp_listen option ("host:port").

    Empty host ⇒ 0.0.0.0. Returns (host, port) or (None, 0) when invalid.
    """
    idx = raw.rfind(":")
    if idx < 0:
        return None, 0
    host = raw[:idx] or "0.0.0.0"
    port_str = raw[idx + 1:]
    if not port_str.isdigit():
        return None, 0
    port = int(port_str)
    if not (1 <= port <= 65535):
        return None, 0
    return host, port


def _local_ip():
    """Best-effort primary local IPv4 address (no packet is actually sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("1.1.1.1", 53))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "?"


def _user_nick():
    """The user's IRC nick: the first non-empty entry of
    irc.server_default.nicks (comma-separated).

    Re-evaluated on every call (no caching) so config changes take effect
    on the next printed line. The option only exists while the IRC plugin
    is loaded — config_get returning "" (plugin not loaded) or an empty
    value is the expected fallback path, not an error.
    """
    if weechat is None:
        return ""
    try:
        opt = weechat.config_get("irc.server_default.nicks")
        if not opt:
            return ""
        raw = weechat.config_string(opt) or ""
    except Exception:
        return ""
    for entry in raw.split(","):
        entry = entry.strip()
        if entry:
            return entry
    return ""


def _short_path(path):
    """Abbreviate a home-directory prefix to ~ (buffer title, session line).

    Paths outside $HOME pass through unchanged; a missing or unexpanded
    HOME (expanduser returning ~ itself) degrades to the raw path.
    """
    if not path:
        return path
    home = os.path.expanduser("~")
    if not home or home == "~":
        return path
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path

def _fmt_elapsed(secs):
    """Compact elapsed-time label for the buffer-title counter.

    0-99 s -> "Ns" (e.g. "42s"); 100-3599 s -> "Nm" (floor of seconds/60,
    e.g. "5m"); >= 3600 s -> "NhMm" (e.g. "1h3m", with the minute part
    dropped when it is 0, e.g. "1h").
    """
    s = int(secs)
    if s < 100:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    h, m = s // 3600, (s % 3600) // 60
    return ("%dh%dm" % (h, m)) if m else ("%dh" % h)

class Session(object):
    """One pi session: a persistent buffer/controller with a replaceable
    connection.

    All per-session rendering and input state (buffer, markdown block state,
    pending prompts, tool nicks, timing, rate-limit window) lives here; the
    Bridge owns the listeners and the connection/transport mechanics. The
    load-time buffer's session takes the first client and keeps the name
    `pi`; each further connection gets its own Session + buffer. A Session
    survives disconnects so a reconnect can reattach to the same buffer.
    """

    def __init__(self, bridge, fixed_name=False):
        self.bridge = bridge
        self.conn = None              # current authed conn dict, or None
        self.fixed_name = fixed_name  # load-time session keeps the name "pi"
        self.buffer = None
        self.alive = False            # buffer still open?
        self.session_id = ""          # pi session id ("" = unknown/absent)
        self.session_cwd = ""         # last cwd from session_info (buffer title)
        self.state = "waiting"        # waiting | idle | thinking | tool:<name>
        self._detail = None           # current title detail hint, kept across ticks
        self._last_title = None       # last title pushed (churn avoidance)
        # user_input (buffer → pi) rate-limit window
        self.ui_times = []
        # markdown fence tracking for streamed assistant lines (per message)
        self._md_msg = None           # msgId of the last assistant_line seen
        self._md_fence = None         # open fence dict (see _fence_open), or None
        # Markdown block accumulator: prose/lists/quotes are printed once the
        # block is complete, so the renderer sees the whole construct. Fenced
        # code keeps streaming (highlighting needs it line by line).
        self._md_block = []           # raw lines of the block being built
        self._md_block_type = None    # MD_BLOCK_* while a block is open
        self._md_block_mode = True    # pi_bridge.markdown state when it started
        self._md_blank_owed = False   # blank separator owed before the NEXT block
        self._md_printing = False     # inside a flush: never flush recursively
        # pending interactive prompt from pi (ui_request); answered via !pick
        self.pending_ui = None        # {id, method, options, multiple}, or None
        # toolCallId → nick, learned from tool_start: tool_end carries no
        # toolName on the wire, so the mapping is what lets the result line
        # (and its output body) keep the same nick as the call that started it
        self.tool_nicks = {}          # bounded by TOOL_NICK_MAP_MAX (see _remember_tool)
        # Timing comes from the pi extension; WeeChat only advances a received
        # active snapshot between lifecycle updates for title redraws.
        self.timing_snapshot = None
        self.timing_received_at = None
    # ---------------------------------------------------------------- buffer
    def make_buffer(self, name="pi"):
        self.buffer = weechat.buffer_new(name, "pi_input_cb", "",
                                         "pi_close_cb", "")
        weechat.buffer_set(self.buffer, "title", "π: (waiting for pi)")
        weechat.buffer_set(self.buffer, "localvar_set_no_log", "1")
        weechat.buffer_set(self.buffer, "localvar_set_type", "private")
        weechat.buffer_set(self.buffer, "localvar_set_server", "pi")
        weechat.buffer_set(self.buffer, "short_name", name)
        self.bridge.session_by_buffer[self.buffer] = self
        self.apply_user_nick()
        self.alive = True
        self._print(C_STATUS + "pi bridge ready — socket %s%s" % (self.bridge.sock_path, R))
        self._print(C_DIM + "type a line to send it to pi; !help lists commands%s" % R)

    def _print(self, text):
        """Print one line, stamped with the current time.

        Lines are printed WITHOUT a leading "\t\t" prefix: that trick
        suppresses the timestamp in the terminal UI, but it also zeroes the
        line's stored date — relay clients (e.g. Glowing Bear over the relay
        websocket) would then show 01.01.1970 or no useful time. With a real
        date, both the TUI and relay clients show proper HH:MM timestamps.
        """
        self._flush_md_block()  # pending assistant text precedes any status line
        if self.alive and self.buffer:
            weechat.prnt(self.buffer, text)

    def _send(self, obj):
        """Send to this session's connection (drop, with a dbg note, if it
        is gone — the line would reach no one)."""
        if self.conn is None:
            dbg("_send %s DROPPED (no conn)" % obj.get("type"))
            return
        self.bridge._send_to(self.conn, obj)

    def apply_user_nick(self):
        """(Re)apply the user-nick buffer localvar from
        irc.server_default.nicks (first entry), so user lines render in the
        prefix column under the user's real nick (chat_nick_self color).
        localvar when the nick is empty — user lines then fall back to the
        '> ' marker.
        """
        if self.buffer is None:
            return
        nick = _user_nick()
        if nick:
            weechat.buffer_set(self.buffer, "localvar_set_nick", nick)
        else:
            weechat.buffer_set(self.buffer, "localvar_unset_nick", "")

    def _print_msg(self, text, role, nick=None):
        """Render one line for a role (message body colors unchanged).

        'pi'   → prnt_date_tags, notify_none, and the nick as the line prefix
                 (the text before the first TAB, the way the IRC plugin emits
                 nicks). `nick` names *who* spoke — a tool name ("read",
                 "bash") or "think" when pi_bridge.nicks is auto; ignored (and
                 `pi` used) when it is `pi`. The nick is colored with WeeChat's
                 own per-nick color (_nick_color); tags carry the identity
                 (nick_<name>) plus today's prefix_nick_chat_nick, which is
                 what applies WeeChat's nick brackets and same-nick handling.
                 notify_none: no line-level notification — only the turn-settle
                 `[x] ready!` banner (notify_highlight) pings the user.
        'user' → prnt_date_tags, tag prefix_nick_chat_nick_self (+ self_msg
                 like the IRC plugin's own echoes), the user's IRC nick as
                 prefix (chat_nick_self color). Empty nick ⇒ legacy
                 fallback: the '> ' marker via plain prnt (today's
                 rendering).
        'sys'  → plain prnt, no prefix (channel-notice style): session
                 info, connect/disconnect notices, errors, command status
                 answers, rate-limit warnings, ready/listening lines.
        """
        # A pending markdown block belongs to the assistant text that came
        # before this line, so it prints first (_md_printing stops the flush's
        # own prints from recursing back through here).
        self._flush_md_block()
        # prnt_date_tags stamps the current time, so lines keep real dates
        # for relay clients (the no-leading-tab rule of _print is untouched).
        if role == "pi":
            if self.alive and self.buffer:
                if self.nicks_mode() == "auto":
                    who = nick or "pi"
                    weechat.prnt_date_tags(
                        self.buffer, int(time.time()),
                        "notify_none,nick_%s,prefix_nick_chat_nick" % who,
                        _nick_color(who) + who + R + "\t" + text)
                else:
                    weechat.prnt_date_tags(
                        self.buffer, int(time.time()),
                        "notify_none,prefix_nick_chat_nick",
                        C_NICK + "pi" + R + "\t" + text)
        elif role == "user":
            nick = _user_nick()
            if nick:
                if self.alive and self.buffer:
                    weechat.prnt_date_tags(
                        self.buffer, int(time.time()),
                        "self_msg,notify_none,no_highlight,"
                        "prefix_nick_chat_nick_self",
                        C_USER + nick + R + "\t" + text)
            else:
                self._print(C_USER + "> " + R + text)
        else:
            self._print(text)

    def set_state(self, state, detail=None):
        self.state = state
        self._detail = detail         # stored so the 1s tick refresh keeps the hint
        prefix = "π:"
        if self.session_cwd:
            prefix += " " + _short_path(self.session_cwd)
        counter = self._timing_text(state)
        base = {
            "waiting": " (disconnected — waiting for pi)",
            "idle": " (idle%s)" % counter,
            "thinking": " (thinking…%s)" % counter,
        }
        if state in base:
            title = prefix + base[state]
        elif state.startswith("tool:"):
            title = prefix + " (tool: %s%s)" % (state[5:], counter)
        else:
            title = None  # unknown state: keep current title
        if title and detail and state != "waiting":
            title += " — " + detail
        # Skip redundant title writes; the 1s tick only changes live counters.
        if self.alive and self.buffer and title and title != self._last_title:
            weechat.buffer_set(self.buffer, "title", title)
            self._last_title = title

    def _timing_text(self, state):
        snapshot = self.timing_snapshot
        if not snapshot or not snapshot["hasRun"]:
            return ""
        now = time.monotonic()
        advance = 0
        if snapshot["runActive"] and not snapshot["runPaused"]:
            advance = max(0, now - self.timing_received_at) * 1000
        run_ms = snapshot["runMs"] + advance
        turn_ms = snapshot["turnMs"]
        if snapshot["turnActive"] and not snapshot["runPaused"]:
            turn_ms += advance
        if snapshot["runActive"]:
            parts = ["run " + _fmt_elapsed(run_ms / 1000)]
            if snapshot["turn"] is not None:
                parts.extend((_fmt_elapsed(turn_ms / 1000),
                              "turn %d" % snapshot["turn"]))
            return " · " + " · ".join(parts)
        if state == "idle":
            turns = snapshot["turns"]
            label = "turn" if turns == 1 else "turns"
            return " · last run %s · %d %s" % (
                _fmt_elapsed(snapshot["runMs"] / 1000), turns, label)
        return ""

    def tick(self):
        """Refresh live elapsed displays; Pi remains authoritative for state."""
        snapshot = self.timing_snapshot
        if not snapshot or not snapshot["runActive"] or snapshot["runPaused"]:
            return
        self.set_state(self.state, self._detail)

    # ---------------------------------------------------------------- options
    def tool_output_mode(self):
        """pi_bridge.tool_output option: full | summary | off."""
        return self._plugin_option("tool_output", TOOL_OUTPUT_MODES, DEFAULT_TOOL_OUTPUT)

    def thinking_enabled(self):
        """pi_bridge.thinking option: on | off."""
        return self._plugin_option("thinking", THINKING_MODES, DEFAULT_THINKING) == "on"

    def highlight_enabled(self):
        """pi_bridge.highlight option: on | off."""
        return self._plugin_option("highlight", HIGHLIGHT_MODES,
                                   DEFAULT_HIGHLIGHT) == "on"

    def nicks_mode(self):
        """pi_bridge.nicks option: auto (tool/think/pi nicks) | pi (legacy)."""
        return self._plugin_option("nicks", NICK_MODES, DEFAULT_NICKS)

    def markdown_enabled(self):
        """pi_bridge.markdown option: on | off."""
        return self._plugin_option("markdown", MARKDOWN_MODES, DEFAULT_MARKDOWN) == "on"

    def _plugin_option(self, name, modes, default):
        if weechat is None:
            return default
        try:
            v = (weechat.config_get_plugin(name) or "").strip().lower()
        except Exception:
            v = ""
        return v if v in modes else default

    @staticmethod
    def _set_plugin_option(name, value):
        if weechat is None:
            return
        try:
            weechat.config_set_plugin(name, value)
        except Exception:
            pass

    def _remember_tool(self, call_id, name):
        """Learn toolCallId → nick so the result line can reuse it.

        tool_end has no toolName on the wire, so the nick of a tool's ✔/✘
        line and of its output body has to come from the tool_start that
        opened the call. The map is capped: an id that never completes (an
        aborted turn) must not grow it without bound.
        """
        nick = _nick_for(name)
        if call_id is not None:
            self.tool_nicks[call_id] = nick
            while len(self.tool_nicks) > TOOL_NICK_MAP_MAX:
                self.tool_nicks.pop(next(iter(self.tool_nicks)))
        return nick

    # ------------------------------------------------ markdown code blocks
    def _print_assistant(self, text, msg_id):
        """Print one streamed assistant line (markdown aware).

        Fenced code streams straight through: it is already rendered line by
        line and highlighting needs that incremental context. Everything else
        is accumulated into a block (paragraph, list, quote) and printed when
        the block is complete, so the renderer sees the whole construct —
        setext headings, list continuations, blank-line separation. A block
        remembers the pi_bridge.markdown mode it started under, so a live
        !markdown toggle never re-renders text that is already on screen.
        """
        if msg_id != self._md_msg:
            self._flush_md_block()   # a new message never extends the old block
            self._md_msg = msg_id
            self._md_fence = None
            self._md_block_done()
        if self._md_fence is not None:
            self._flush_md_block()   # no block may span a fence
            if _fence_closed(self._md_fence, text):
                self._md_fence = None
                self._print_msg(C_DIM + text + R, "pi")
                return
            self._print_fence_body(text)
            return
        fence = _fence_open(text)
        if fence is not None:
            self._flush_md_block()
            self._md_fence = fence
            self._print_msg(C_DIM + text + R, "pi")
            return
        if not self.markdown_enabled():
            self._flush_md_block()   # anything pending belongs to the other mode
            self._print_msg(C_PI + text + R, "pi")
            return

        setext = _md_setext_level(text)
        if setext and self._md_block_type == MD_BLOCK_PARA:
            self._md_block.append(text)
            self._md_block_type = MD_BLOCK_SETEXT
            self._flush_md_block()
            return

        kind = _md_block_kind(text)
        if kind is None:               # blank line: the block is complete
            if self._md_block_type == MD_BLOCK_QUOTE:
                # CommonMark keeps a quote open across blank lines, and we
                # cannot know yet whether the next line is still quoted
                self._md_block.append("")
                return
            self._flush_md_block()
            self._md_blank_owed = True
            return
        if kind in (MD_BLOCK_HEADING, MD_BLOCK_HR):
            self._flush_md_block()     # a heading or a rule stands alone
            self._md_start_block(kind, text)
            self._flush_md_block()
            return

        cur = self._md_block_type
        if cur is None:
            self._md_start_block(kind, text)
            return
        if cur == kind:
            self._md_block.append(text)
            return
        if cur in (MD_BLOCK_LIST, MD_BLOCK_QUOTE) and kind == MD_BLOCK_PARA:
            if cur == MD_BLOCK_QUOTE and self._md_block and \
                    not self._md_block[-1].strip():
                # CommonMark's lazy continuation only applies while the quoted
                # paragraph is still running: after a blank line this prose is
                # a new paragraph, not part of the quote
                self._flush_md_block()
                self._md_blank_owed = True   # the blank that ended the quote
                self._md_start_block(kind, text)
                return
            self._md_block.append(text)  # lazy continuation of the open block
            return
        self._flush_md_block()
        self._md_start_block(kind, text)

    def _print_fence_body(self, text):
        """Body line of an open fence: indented, highlighted when supported."""
        lang = self._md_fence["lang"]
        if self.highlight_enabled() and HL_ALIAS.get(lang.lower()):
            body = highlight_code(text, lang, self._md_fence["ctx"])
        else:
            body = text
        self._print_msg("  " + body, "pi")

    def _md_start_block(self, kind, text):
        self._md_block_type = kind
        self._md_block_mode = self.markdown_enabled()
        self._md_block = [text]

    def _md_block_done(self):
        """Forget the pending block (it has just been flushed)."""
        self._md_block = []
        self._md_block_type = None

    def _flush_md_block(self):
        """Print the pending block. Idempotent, and never re-entrant.

        Blank-line separators are deferred: the blank is printed before the
        NEXT block rather than trailing the one that ended, so a message never
        ends with an empty line and consecutive blanks collapse into one.
        """
        if self._md_printing or not self._md_block:
            return
        lines = self._md_block
        kind = self._md_block_type
        mode = self._md_block_mode
        self._md_block_done()
        self._md_printing = True
        try:
            if self._md_blank_owed:
                self._md_blank_owed = False
                self._print("")   # a plain blank line, no nick on it
            for line in self._render_md_block(lines, kind, mode):
                self._print_msg(line, "pi")
        finally:
            self._md_printing = False

    def _render_md_block(self, lines, kind, mode):
        """Render one raw markdown block into printable lines.

        mode False = the block started while pi_bridge.markdown was off: print
        it exactly as pi wrote it. Otherwise render it by construct (headings,
        emphasis, lists, quotes, rules). Emphasis may span the lines of one
        wrapped paragraph: the open delimiter is carried from line to line.
        """
        if not mode:
            return [C_PI + line + R for line in lines]
        if kind == MD_BLOCK_HEADING:
            parsed = _md_atx(lines[0])
            if parsed is None:                # classified as one, is not one
                return [C_PI + line + R for line in lines]
            level, text = parsed
            base = _md_heading_style(level)
            return [base + _md_inline(text, base)[0] + R]
        if kind == MD_BLOCK_SETEXT:
            # the underline is structure, not text: it sets the level, it is
            # never printed
            level = _md_setext_level(lines[-1]) or 2
            return self._render_styled(lines[:-1], _md_heading_style(level))
        if kind == MD_BLOCK_QUOTE:
            return self._render_quote(lines)
        if kind == MD_BLOCK_HR:
            return [MD_QUOTE_STYLE + MD_HR + R]
        if kind == MD_BLOCK_LIST:
            items = _md_list_items(lines)
            out = []
            pending = None
            for idx, (prefix, content) in enumerate(items):
                rest = "\n".join(c for _, c in items[idx + 1:]) \
                    if idx + 1 < len(items) else None
                rendered, pending = _md_inline(content, C_PI, pending, rest)
                out.append(prefix + C_PI + rendered + R)
            return out
        return self._render_styled(lines, C_PI)

    def _render_quote(self, lines):
        """Blockquote block → bar-prefixed dimmed lines.

        A line without `>` continues the quote at the current depth; nested
        `>>` print one bar per level. Blank lines separate quoted paragraphs
        and never trail the block.
        """
        body = []
        depth = 0
        for line in lines:
            m = MD_QUOTE_MARK_RE.match(line)
            if m:
                depth = m.group(1).count(">")
                inner = m.group(2).strip()
            else:
                inner = line.strip()
                depth = depth or 1
            # a list inside a quote keeps its list shape: the marker is read
            # from the quoted text, not from the line's first column
            if _md_block_kind(inner) == MD_BLOCK_LIST:
                prefix, inner = _md_list_items([inner])[0]
                body.append((depth, prefix + inner))
            else:
                body.append((depth, inner))
        while body and not body[-1][1]:
            body.pop()
        out = []
        pending = None
        texts = [t for _, t in body]
        for idx, (level, text) in enumerate(body):
            rest = "\n".join(texts[idx + 1:]) if idx + 1 < len(texts) else None
            rendered, pending = _md_inline(text, MD_QUOTE_STYLE, pending, rest)
            if not text:
                out.append("")           # blank between quoted paragraphs
            else:
                out.append(MD_QUOTE_BAR * level + MD_QUOTE_STYLE + rendered + R)
        return out

    def _render_styled(self, lines, base):
        """Render block lines in one base style, carrying open emphasis across
        the lines of a wrapped paragraph.
        """
        out = []
        pending = None
        for idx, line in enumerate(lines):
            rest = "\n".join(lines[idx + 1:]) if idx + 1 < len(lines) else None
            rendered, pending = _md_inline(line, base, pending, rest)
            out.append(base + rendered + R)
        return out

    # ------------------------------------------------------------ dispatching
    def dispatch(self, msg):
        t = msg.get("type")
        if t == "hello":
            return  # already handled (and gated) in _handle
        if t == "ping":
            self._send({"type": "pong", "ts": msg.get("ts")})
            return
        if t == "timing":
            run_ms = msg.get("runMs")
            turn_ms = msg.get("turnMs")
            turn = msg.get("turn")
            turns = msg.get("turns")
            run_active = msg.get("runActive")
            turn_active = msg.get("turnActive")
            run_paused = msg.get("runPaused")
            has_run = msg.get("hasRun")
            numbers = (run_ms, turn_ms, turns)
            if any(type(v) is not int or v < 0 or v > 2**53 - 1
                   for v in numbers):
                return
            if ((turn is not None and (type(turn) is not int or turn < 1))
                    or any(type(v) is not bool for v in
                           (run_active, turn_active, run_paused, has_run))):
                return
            self.timing_snapshot = {
                "runMs": run_ms,
                "turnMs": turn_ms,
                "turn": turn,
                "turns": max(turns, turn or 0),
                "runActive": run_active,
                "turnActive": turn_active,
                "runPaused": run_paused,
                "hasRun": has_run,
            }
            self.timing_received_at = time.monotonic()
            self.set_state(self.state, self._detail)
            return
        if t == "status":
            state = msg.get("state", "idle")
            was_busy = self.state == "thinking" or self.state.startswith("tool:")
            self.set_state(state, msg.get("detail"))
            if state == "idle" and was_busy and self.alive and self.buffer:
                # turn settled: one extra highlight line below the last
                # message line (left untouched); date 0 ⇒ now
                weechat.prnt_date_tags(self.buffer, 0, "notify_highlight",
                                       C_OK + "✔ ready!" + R)
            return
        if t == "session_info":
            cwd = msg.get("cwd")
            if isinstance(cwd, str) and cwd:
                self.session_cwd = cwd
                if not self.fixed_name:
                    # rename the buffer to the project (unique per cwd)
                    self.rename_buffer(self.bridge._new_buffer_name(
                        "pi:" + _short_path(cwd)))
                self.set_state(self.state)  # refresh the title with the path
            # track session-id changes (a !cd / !new switch mints a new id)
            sid = msg.get("sessionId")
            if isinstance(sid, str) and sid and sid != self.session_id:
                if self.session_id:
                    # release the old mapping only if it still points here
                    if self.bridge.session_by_id.get(self.session_id) is self:
                        del self.bridge.session_by_id[self.session_id]
                self.session_id = sid
                self.bridge.session_by_id[sid] = self
            bits = []
            if cwd:
                bits.append(_short_path(str(cwd)))
            if msg.get("model"):
                bits.append(msg["model"])
            if msg.get("name"):
                bits.append("“%s”" % msg["name"])
            self._print(C_STATUS + "session: %s%s" % (" ".join(bits) or "(unnamed)", R))
            return
        if t == "user_echo":
            text = msg.get("text", "")
            for line in str(text).splitlines() or [""]:
                self._print_msg(line, "user")
            return
        if t == "assistant_line":
            self._print_assistant(msg.get("text", ""), msg.get("msgId"))
            return
        if t == "thinking_line":
            if not self.thinking_enabled():
                return  # hidden; the line is dropped entirely
            self._print_msg(C_DIM + "\U0001F4AD " + msg.get("text", "") + R,
                            "pi", THINK_NICK)
            return
        if t == "assistant_flush":
            self._flush_md_block()   # the message is over: print what is pending
            return
        if t == "tool_start":
            name = msg.get("toolName") or "tool"
            nick = self._remember_tool(msg.get("toolCallId"), name)
            summary = format_tool_args(name, msg.get("args") or {})
            # auto: the nick column already names the tool, so the body is just
            # glyph + args. pi mode: keep the name in the body (legacy look).
            body = C_TOOL + "⚙"
            if self.nicks_mode() != "auto":
                body += " " + name
            if summary:
                body += C_DIM + " " + summary
            self._print_msg(body + R, "pi", nick)
            return
        if t == "tool_end":
            # tool_end carries no toolName on the wire: the nick comes back
            # from the tool_start that opened this call (`tool` if that start
            # was never seen — e.g. a reconnect in the middle of a tool).
            # Legacy (`pi`) mode labels the line with that same nick, where
            # before this option it always printed the literal "tool".
            nick = self.tool_nicks.pop(msg.get("toolCallId"), None) or "tool"
            ok = not msg.get("isError")
            color = C_OK if ok else C_ERR
            glyph = "✔" if ok else "✘"
            label = "" if self.nicks_mode() == "auto" else " " + nick
            self._print_msg(color + glyph + label + R, "pi", nick)
            for line in self._tool_output_lines(msg.get("output")):
                self._print_msg(C_TOOL_OUT + "  " + line + R, "pi", nick)
            return
        if t == "ui_request":
            self._handle_ui_request(msg)
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

    # ------------------------------------------------- interactive prompts
    def _handle_ui_request(self, msg):
        """Render a select/input prompt from pi; the user answers with !pick.

        `select` shows numbered options (comma list when multiple);
        `input` asks for free-form text. Only one prompt is tracked at a
        time — a new request supersedes the old one, which is released on
        the pi side with a cancelled ui_response so it doesn't wait forever.
        """
        req_id = msg.get("id")
        method = msg.get("method")
        title = str(msg.get("title") or "").strip() or "(untitled)"
        if not isinstance(req_id, int) or method not in ("select", "input"):
            self._print(C_ERR + "pi bridge: bad ui_request ignored" + R)
            return
        if self.pending_ui is not None and self.pending_ui["id"] != req_id:
            self._send({"type": "ui_response", "id": self.pending_ui["id"],
                        "cancelled": True})
        if method == "select":
            options = []
            for opt in (msg.get("options") or [])[:24]:  # sanity cap
                if isinstance(opt, dict) and isinstance(opt.get("label"), str):
                    options.append((opt["label"],
                                    str(opt.get("description") or "").strip()))
                elif isinstance(opt, str) and opt:
                    options.append((opt, ""))
            if not options:
                self._print(C_ERR + "pi bridge: select with no options ignored" + R)
                return
            multiple = bool(msg.get("multiple"))
            self.pending_ui = {"id": req_id, "method": "select",
                               "options": options, "multiple": multiple}
            self._print(C_STATUS + "? " + title + R)
            for i, (label, desc) in enumerate(options, 1):
                self._print("%2d. %s" % (i, label))
                if desc:
                    self._print("    " + C_DIM + desc.replace("\n", " ") + R)
            hint = "reply !pick <n>" + (", e.g. !pick 1,3 (multiple)" if multiple else "") \
                   + " · !pick cancel"
            self._print(C_DIM + hint + R)
        else:  # input
            self.pending_ui = {"id": req_id, "method": "input"}
            self._print(C_STATUS + "? " + title + R)
            placeholder = msg.get("placeholder")
            ph = (" (%s)" % str(placeholder).replace("\n", " ")) if placeholder else ""
            self._print(C_DIM + "reply !pick <your answer>%s · !pick cancel" % ph + R)
        self.set_state(self.state, "awaiting !pick")

    def handle_pick(self, arg, raw_line):
        """Answer the pending ui_request with one buffer line.

        select:  !pick <n> (comma list when multiple), or exact option text
        input:   !pick <free-form text>
        any:     !pick cancel
        """
        pending = self.pending_ui
        if pending is None:
            self._print(C_ERR +
                        "nothing to pick — !pick answers a “?” prompt from pi" + R)
            return
        if arg == "" or arg in ("cancel", "c"):
            self._respond_ui(pending, cancelled=True)
            self._print_msg(raw_line, "user")
            return
        if pending["method"] == "input":
            self._respond_ui(pending, value=arg)
            self._print_msg(raw_line, "user")
            return
        # select: try numbers first ("3", or "1,3"), then exact option text
        parts = [p.strip() for p in arg.split(",")]
        if (all(p.isdigit() for p in parts)
                and all(1 <= int(p) <= len(pending["options"]) for p in parts)):
            chosen = [pending["options"][int(p) - 1][0] for p in parts]
            if not pending["multiple"] and len(chosen) > 1:
                self._print(C_ERR + "single choice only — pick one number" + R)
                return
            value = chosen if pending["multiple"] else chosen[0]
        else:
            value = None
            for label, _desc in pending["options"]:
                if label == arg:
                    value = [label] if pending["multiple"] else label
                    break
            if value is None:
                self._print(C_ERR + "no such option: %s (numbers 1-%d, or “cancel”)"
                            % (arg, len(pending["options"])) + R)
                return
        self._respond_ui(pending, value=value)
        self._print_msg(raw_line, "user")

    def _respond_ui(self, pending, value=None, cancelled=False):
        """Send the ui_response for `pending` (rate-limited like input: it
        unblocks a pi command, so a flooded buffer must not unblock many)."""
        now = time.time()
        self.ui_times = [t for t in self.ui_times if now - t < 1.0]
        if len(self.ui_times) >= USER_INPUT_MAX_PER_S:
            self._print(C_REJECT + "input rate limited (max %d/s)%s"
                        % (USER_INPUT_MAX_PER_S, R))
            return
        self.ui_times.append(now)
        msg = {"type": "ui_response", "id": pending["id"]}
        if cancelled:
            msg["cancelled"] = True
        else:
            msg["value"] = value
        self._send(msg)
        self.pending_ui = None
        self.set_state(self.state)  # drop the “awaiting !pick” title hint

    # ---------------------------------------------------------- user input
    def _send_user_input(self, text, echo, msg):
        """Forward one buffer line to pi as user_input (rate-limited).

        `text` is what goes on the wire (prefixes like "!s " stripped),
        `echo` is what gets echoed into the buffer (the line as typed).

        This is the only path that spends the remote pi's LLM budget, so it
        is the only message type rate-limited (a flooded buffer — relay
        input, a bot, a compromised local host — must not translate 1:1
        into prompts). The line is still echoed into the buffer either way.
        """
        now = time.time()
        self.ui_times = [t for t in self.ui_times if now - t < 1.0]
        if len(self.ui_times) >= USER_INPUT_MAX_PER_S:
            self._print(C_REJECT + "input rate limited (max %d/s)%s"
                        % (USER_INPUT_MAX_PER_S, R))
            self._send({"type": "error", "code": "rate_limited"})
        else:
            self.ui_times.append(now)
            self._send(dict(msg, type="user_input", text=text))
        self._print_msg(echo, "user")

    def on_input(self, line):
        if self.conn is None or not self.conn.get("authed"):
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
        if line == "!nick":
            self._print(C_STATUS + "nick mode: %s (auto | pi)%s"
                        % (self.nicks_mode(), R))
            return
        if line.startswith("!nick "):
            arg = line[6:].strip().lower()
            if arg in NICK_MODES:
                self._set_plugin_option("nicks", arg)
                self._print(C_STATUS + "nick mode: %s%s" % (arg, R))
            else:
                self._print(C_ERR + "unknown nick mode: %s (auto | pi)%s"
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
                self._print(C_ERR + "unknown thinking mode: %s (on | off)%s"
                            % (arg, R))
            return
        if line == "!highlight":
            self._print(C_STATUS + "code highlighting: %s (!highlight on|off)%s" % (
                "on" if self.highlight_enabled() else "off", R))
            return
        if line.startswith("!highlight "):
            arg = line[11:].strip().lower()
            if arg in HIGHLIGHT_MODES:
                self._set_plugin_option("highlight", arg)
                self._print(C_STATUS + "code highlighting: %s%s" % (arg, R))
            else:
                self._print(C_ERR + "unknown highlight mode: %s (on | off)%s"
                            % (arg, R))
            return
        if line == "!markdown":
            self._print(C_STATUS + "markdown rendering: %s (!markdown on|off)%s" % (
                "on" if self.markdown_enabled() else "off", R))
            return
        if line.startswith("!markdown "):
            arg = line[10:].strip().lower()
            if arg in MARKDOWN_MODES:
                self._flush_md_block()  # pending text keeps the mode it started in
                self._set_plugin_option("markdown", arg)
                self._print(C_STATUS + "markdown rendering: %s%s" % (arg, R))
            else:
                self._print(C_ERR + "unknown markdown mode: %s (on | off)%s"
                            % (arg, R))
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
            self._print_msg(line, "user")
        elif line == "!model":
            self._send({"type": "command", "name": "model"})
            self._print_msg(line, "user")
        elif line.startswith("!model "):
            # pi's setModel via the weechat-ctl extension command
            self._send({"type": "command", "name": "model",
                        "arg": line[7:].strip()})
            self._print_msg(line, "user")
        elif line == "!cd" or (line.startswith("!cd ") and not line[4:].strip()):
            self._print(C_ERR + "usage: !cd <path> — e.g. !cd ~/my-project" + R)
            return
        elif line.startswith("!cd "):
            # switch pi to a project dir; fuzzy matches (and the "create as
            # new project" option) come back as a ? prompt answered with !pick
            self._send({"type": "command", "name": "cd", "arg": line[4:].strip()})
            self._print_msg(line, "user")
        elif line == "!pick" or line.startswith("!pick "):
            self.handle_pick(line[5:].strip(), line)
            return
        elif line.startswith("!s "):
            self._send_user_input(line[3:], line, {"deliverAs": "steer"})
        elif line.startswith("!q "):
            self._send_user_input(line[3:], line, {"deliverAs": "followUp"})
        else:
            self._send_user_input(line, line, {})

    def _reset_transient(self):
        """Forget per-connection rendering state: a (re)connect starts fresh
        "msgIds on the pi side, so stale fence state must not leak into new
        "messages."""
        self._md_msg = None
        self._md_fence = None
        self._flush_md_block()   # never strand a half-built block on screen
        self._md_block_done()
        self._md_blank_owed = False
        # no live peer left to answer a pending prompt, and tool calls
        # from the old connection will never be completed on this one
        self.pending_ui = None
        self.tool_nicks = {}
        # Drop the last timing snapshot while disconnected. The extension
        # will send a fresh Pi-owned snapshot after the next handshake.
        self.timing_snapshot = None
        self.timing_received_at = None
    def handle_disconnect(self):
        """The connection went away; the buffer stays open."""
        self._reset_transient()
        self.set_state("waiting")
        self._print(C_DIM + "— pi disconnected —%s" % R)
    def handle_reattach(self):
        """A reconnecting session reattaches to this buffer (history kept).
        "Stale per-connection state is flushed; if the user closed the
        "buffer in the meantime, recreate it."""
        self._reset_transient()
        if not (self.alive and self.buffer):
            self.make_buffer(self.bridge.session_name_for(self))
    def rename_buffer(self, name):
        if not (self.alive and self.buffer):
            return
        try:
            current = weechat.buffer_get_string(self.buffer, "name")
        except Exception:
            current = None
        if current == name:
            return
        weechat.buffer_set(self.buffer, "name", name)
        weechat.buffer_set(self.buffer, "short_name", name)

class Bridge(object):
    def __init__(self):
        self.sock_path = default_socket_path()
        # unix listener (always on)
        self.listen_sock = None
        self.listen_hook = None
        # tcp listener (opt-in, live rebind via pi_config_cb)
        self.tcp_listen_sock = None
        self.tcp_listen_hook = None
        self.tcp_listen_value = ""
        self.config_hook = None
        self.nick_config_hook = None  # irc.server_default.nicks live re-apply
        # sessions: one per pi session (the load-time buffer's session takes
        # the first client and keeps the name "pi"); connections: authed
        # clients + a few in-handshake pendings
        self.sessions = []             # all Sessions (creation order)
        self.load_session = Session(self, fixed_name=True)  # takes the 1st client
        self.sessions.append(self.load_session)
        self.session_by_buffer = {}   # buffer pointer → Session
        self.session_by_id = {}       # session id → Session (reattach)
        self.clients = []             # authenticated conns (list of dicts)
        self.pending = []             # accepted, not authed yet (list of dicts)
        # per-IP abuse state (TCP peers only)
        self.ip_failures = {}         # ip -> [timestamps of auth failures]
        self.ip_lockouts = {}         # ip -> lockout-until (epoch)
        self.tick_hook = None         # 1s hook_timer handle (live counters)

    # ------------------------------------------------------- connection state

    def _new_conn(self, conn_sock, ip, peer):
        return {
            "sock": conn_sock,
            "fd": conn_sock.fileno(),
            "ip": ip,                 # None for unix
            "peer": peer,             # human label for logs/prints
            "rxbuff": b"",
            "outq": b"",
            "read_hook": None,
            "write_hook": None,
            "timer": None,            # auth-deadline hook_timer handle
            "authed": False,
            "token": None,            # token we challenged with (None = anon)
            "nonce": None,
        }

    def _conn_by_fd(self, fd):
        for conn in self.clients:
            if conn["fd"] == fd:
                return conn
        for conn in self.pending:
            if conn["fd"] == fd:
                return conn
        return None
    def _new_buffer_name(self, base):
        """A buffer name not currently taken: base, base-2, base-3, …"""
        taken = set()
        for s in self.sessions:
            if s.buffer:
                try:
                    name = weechat.buffer_get_string(s.buffer, "name")
                except Exception:
                    name = None
                if name:
                    taken.add(name)
        name = base
        n = 2
        while name in taken:
            name = "%s-%d" % (base, n)
            n += 1
        return name
    def session_name_for(self, session):
        """Name for a session's (re)created buffer: fixed sessions keep
        "pi"; the rest are named after the session cwd when known."""
        if session.fixed_name:
            return self._new_buffer_name("pi")
        if session.session_cwd:
            return self._new_buffer_name("pi:" + _short_path(session.session_cwd))
        return self._new_buffer_name("pi")

    # ---------------------------------------------------------------- options

    def _opt(self, name):
        """Read a pi_bridge.* plugin option (WeeChat expands ${sec.data.…})."""
        if weechat is None:
            return ""
        try:
            return (weechat.config_get_plugin(name) or "").strip()
        except Exception:
            return ""

    def _token(self):
        return self._opt("token")

    def _allowed_ips_re(self):
        """Compiled allowed_ips regex, or None (empty = allow all)."""
        raw = self._opt("allowed_ips")
        if not raw:
            return None
        try:
            return re.compile(raw)
        except re.error as err:
            dbg("allowed_ips: invalid regex %r: %s (treating as allow-all)"
                % (raw, err))
            return None

    def config_warnings(self):
        """Loud buffer warnings for common misconfigurations."""
        token = self._token()
        if "${" in token:
            self._print_all(C_REJECT +
                        "pi_bridge.token still contains a ${…} reference — it "
                        "was not expanded. Store the secret with "
                        "/secure set pi_weechat_token <token> and use "
                        "/set plugins.var.python.pi_bridge.token \"${sec.data.pi_weechat_token}\""
                        + R)
        if self._opt("tcp_listen") and not token:
            self._print_all(C_ERR +
                        "pi_bridge.tcp_listen is set but pi_bridge.token is "
                        "empty — TCP clients are accepted WITHOUT "
                        "authentication" + R)

    def make_buffer(self):
        """Create the load-time buffer (its session keeps the name `pi`)."""
        return self.load_session.make_buffer()

    def _print_all(self, text):
        """Print into every live session buffer (listener-level news)."""
        for session in self.sessions:
            session._print(text)

    # ------------------------------------------------------------ listeners

    def make_unix_server(self):
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

    def make_tcp_server(self):
        """Start (or restart) the TCP listener (urlserver.py pattern).

        The listen socket stays BLOCKING: hook_fd only fires when a
        connection is queued, and WeeChat callbacks are single-threaded, so
        one plain accept() per event in pi_tcp_listen_cb is safe.
        """
        raw = self._opt("tcp_listen")
        host, port = _parse_tcp_listen(raw) if raw else (None, 0)
        if raw and host is None:
            self._print_all(C_ERR + "pi bridge: bad tcp_listen value %r (want "
                                   "host:port, e.g. 0.0.0.0:52311)%s" % (raw, R))
            return False
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(5)
        self.stop_tcp_server(keep_conns=True)
        self.tcp_listen_sock = sock
        self.tcp_listen_hook = weechat.hook_fd(sock.fileno(), 1, 0, 0,
                                               "pi_tcp_listen_cb", "")
        bound_host, bound_port = sock.getsockname()[:2]
        line = "listening on tcp %s:%d" % (bound_host, bound_port)
        if bound_host == "0.0.0.0":
            line += " (this host: %s)" % _local_ip()
        if self._token():
            line += " (token required)"
        self._print_all(C_STATUS + line + R)
        dbg("tcp listener started on %s:%d" % (bound_host, bound_port))
        return True

    def stop_tcp_server(self, keep_conns=False):
        """Close + unhook the TCP listener.

        With keep_conns=False (live rebind / cleanup) also drops any
        TCP-originated connections: authed clients that came over TCP, and
        all in-handshake TCP pendings.
        """
        if self.tcp_listen_sock is not None:
            try:
                self.tcp_listen_sock.close()
            except OSError:
                pass
            self.tcp_listen_sock = None
        if self.tcp_listen_hook:
            weechat.unhook(self.tcp_listen_hook)
            self.tcp_listen_hook = None
        if not keep_conns:
            for conn in list(self.pending):
                if conn.get("ip") is not None:
                    self.drop_conn(conn)
            for conn in list(self.clients):
                if conn.get("ip") is not None:
                    self.drop_conn(conn)

    # --------------------------------------------------------------- accept

    def accept_pending(self):
        """Read event on the (non-blocking) unix listen socket."""
        while True:
            try:
                conn_sock, _addr = self.listen_sock.accept()
            except BlockingIOError:
                return
            except OSError:
                self._print_all(C_ERR + "pi bridge: accept error" + R)
                return
            self.on_accept(conn_sock, None, "unix")

    def accept_tcp(self):
        """Read event on the (blocking) tcp listen socket: ONE accept."""
        try:
            conn_sock, addr = self.tcp_listen_sock.accept()
        except OSError as err:
            dbg("tcp accept error: %s" % err)
            return
        ip, port = addr[0], addr[1]
        try:
            conn_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.on_accept(conn_sock, ip, "tcp %s:%d" % (ip, port))

    def on_accept(self, conn_sock, ip, peer):
        now = time.time()
        # ---- peer-IP gates (TCP only), BEFORE any handshake byte goes out
        if ip is not None:
            self._prune_ip_state(now)
            until = self.ip_lockouts.get(ip)
            if until and now < until:
                dbg("accept: %s locked out (%.0fs remaining) — closing"
                    % (ip, until - now))
                self._close_sock(conn_sock)
                return
            allowed = self._allowed_ips_re()
            if allowed is not None and not allowed.search(ip):
                dbg("accept: %s not in allowed_ips — closing" % ip)
                self._close_sock(conn_sock)
                return
        # ---- unauthenticated-connection cap
        if len(self.pending) >= MAX_PENDING_UNAUTH:
            dbg("accept: %d pending unauthed connections — closing %s"
                % (len(self.pending), peer))
            self._close_sock(conn_sock)
            return
        # ---- admit: pending until a valid hello arrives
        conn_sock.setblocking(False)
        conn = self._new_conn(conn_sock, ip, peer)
        self.pending.append(conn)
        conn["read_hook"] = weechat.hook_fd(conn["fd"], 1, 0, 0,
                                            "pi_client_cb", str(conn["fd"]))
        token = self._token()
        if token:
            conn["token"] = token
            conn["nonce"] = secrets.token_hex(32)
            self._send_to(conn, {"type": "challenge", "nonce": conn["nonce"]})
        conn["timer"] = weechat.hook_timer(int(AUTH_TIMEOUT_S * 1000), 0, 1,
                                           "pi_auth_timeout_cb", str(conn["fd"]))
        dbg("accept: %s pending (fd=%d, challenge=%s)"
            % (peer, conn["fd"], bool(token)))

    @staticmethod
    def _close_sock(conn_sock):
        try:
            conn_sock.close()
        except OSError:
            pass

    # -------------------------------------------------------------- per-IP

    def _prune_ip_state(self, now):
        for ip in list(self.ip_lockouts):
            if now >= self.ip_lockouts[ip]:
                del self.ip_lockouts[ip]
        for ip in list(self.ip_failures):
            recent = [t for t in self.ip_failures[ip] if now - t < FAIL_WINDOW_S]
            if recent:
                self.ip_failures[ip] = recent
            else:
                del self.ip_failures[ip]

    def _record_failure(self, conn):
        """Count an auth failure per source IP; lock out after FAIL_MAX."""
        ip = conn.get("ip")
        if not ip:
            return
        now = time.time()
        fails = [t for t in self.ip_failures.get(ip, []) if now - t < FAIL_WINDOW_S]
        fails.append(now)
        self.ip_failures[ip] = fails
        if len(fails) > FAIL_MAX:
            self.ip_lockouts[ip] = now + LOCKOUT_S
            self.ip_failures.pop(ip, None)
            dbg("lockout: %s ignored silently for %ds" % (ip, LOCKOUT_S))

    # ------------------------------------------------------------ client I/O

    def client_event(self, data, fd):
        """Read events (and HUP) for accepted clients (unix or tcp)."""
        if fd is not None and fd < 0:
            # hooked fd gone (e.g. we closed it): `data` carries the fd
            conn = self._conn_by_fd(int(data) if data else -1)
            if conn is not None:
                self.drop_conn(conn)
            return
        conn = self._conn_by_fd(fd)
        if conn is None:
            return
        # per-event read cap: a burst can never stall WeeChat's UI; the
        # remainder stays in rxbuff (or the kernel) for the next event
        budget = MAX_BYTES_PER_EVENT
        total = 0
        while budget > 0:
            try:
                chunk = conn["sock"].recv(min(65536, budget))
            except BlockingIOError:
                break
            except OSError as err:
                dbg("recv error: %s" % err)
                self.drop_conn(conn)
                return
            if not chunk:  # peer closed (recv == 0) → disconnect
                dbg("recv 0 bytes — %s closed, dropping" % conn["peer"])
                self.drop_conn(conn)
                return
            budget -= len(chunk)
            total += len(chunk)
            conn["rxbuff"] += chunk
        if total:
            dbg("recv %d bytes (fd=%d, rxbuff=%d)"
                % (total, fd, len(conn["rxbuff"])))
        self._process_lines(conn)

    def _process_lines(self, conn):
        while b"\n" in conn["rxbuff"]:
            line, conn["rxbuff"] = conn["rxbuff"].split(b"\n", 1)
            if len(line) > MAX_LINE:
                if conn["authed"]:
                    sess = conn.get("session")
                    if sess:
                        sess._print(C_ERR + "pi bridge: dropped oversized message" + R)
                continue
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                if conn["authed"]:
                    sess = conn.get("session")
                    if sess:
                        sess._print(C_ERR + "pi bridge: bad JSON line ignored" + R)
                continue
            if isinstance(msg, dict):
                try:
                    self._handle(conn, msg)
                except Exception as err:  # never let one bad message kill the loop
                    if conn["authed"]:
                        sess = conn.get("session")
                        if sess:
                            sess._print(C_ERR + "pi bridge: dispatch error: %s%s" % (err, R))
                # If handling the message closed this connection (a rejection
                # during the handshake), the leftover bytes belong to a dead
                # socket — stop processing them.
                if self._conn_by_fd(conn["fd"]) is not conn:
                    break
        if len(conn["rxbuff"]) > MAX_LINE:
            conn["rxbuff"] = b""

    # ------------------------------------------------------------ handshaking

    def _handle(self, conn, msg):
        t = msg.get("type")
        if t == "hello":
            self._handle_hello(conn, msg)
            return
        if not conn["authed"]:
            # handshake gating: everything before a valid hello is ignored
            dbg("pre-auth %r from %s — ignored" % (t, conn["peer"]))
            return
        conn["session"].dispatch(msg)

    def _handle_hello(self, conn, msg):
        if conn["authed"]:
            return  # duplicate hello: ignore
        try:
            proto = int(msg.get("protocol", 0))
        except (TypeError, ValueError):
            proto = 0
        if proto != PROTOCOL:
            self._send_to(conn, {"type": "error", "code": "protocol_mismatch"})
            self._print_all(C_ERR + "pi bridge: protocol mismatch" + R)
            self.drop_conn(conn)
            return
        token = conn["token"]
        if token is not None:
            proof = str(msg.get("proof") or "")
            expected = _proof(token, conn["nonce"])
            try:
                ok = hmac.compare_digest(proof.lower(), expected)
            except TypeError:  # non-ascii proof
                ok = False
            if not ok:
                self._record_failure(conn)
                self._print_all(C_REJECT + "pi bridge: auth failed from %s%s"
                                % (conn["peer"], R))
                self._send_to(conn, {"type": "error", "code": "auth_failed"})
                self.drop_conn(conn)
                return
        # authenticated: promote to a client slot (one per pi session)
        self._unhook_timer(conn)
        conn["authed"] = True
        self.pending.remove(conn)
        session_id = msg.get("sessionId")
        if not (isinstance(session_id, str) and session_id):
            session_id = ""
        session = None
        reattach = False
        if session_id:
            candidate = self.session_by_id.get(session_id)
            if candidate is not None and candidate.conn is not None:
                # a live connection already owns this session id: reject
                self._send_to(conn, {"type": "error",
                                     "code": "session_id_in_use"})
                self.drop_conn(conn)
                dbg("hello: session id %s already connected — rejected %s"
                    % (session_id, conn["peer"]))
                return
            if candidate is not None:
                session = candidate     # its connection is gone → reattach
                reattach = True
        if session is None:
            if self.load_session.conn is None:
                session = self.load_session  # first client takes the load-time buffer
            else:
                session = Session(self)
                self.sessions.append(session)
                session.make_buffer(self.session_name_for(session))
        conn["session"] = session
        session.conn = conn
        if session_id:
            session.session_id = session_id
            self.session_by_id[session_id] = session
        self.clients.append(conn)
        if reattach:
            session.handle_reattach()
        self._send_to(conn, {"type": "hello", "protocol": PROTOCOL,
                             "name": "weechat-pi-bridge"})
        session.set_state("idle")
        if conn["ip"] is not None:
            session._print(C_OK + "— pi connected from %s —%s" % (conn["ip"], R))
        else:
            session._print(C_OK + "— pi connected —%s" % R)
        dbg("client authed: %s (session_id=%s, reattach=%s)"
            % (conn["peer"], session_id or "-", reattach))

    def auth_timeout(self, fd):
        """hook_timer: no valid hello within AUTH_TIMEOUT_S → drop silently."""
        conn = self._conn_by_fd(fd)
        if conn is None or conn["authed"] or not conn.get("timer"):
            return
        dbg("auth timeout: dropping %s" % conn["peer"])
        self.drop_conn(conn)

    def drop_conn(self, conn):
        dbg("drop_conn %s (authed=%s)" % (conn.get("peer"), conn.get("authed")))
        self._unhook_timer(conn)
        self._close_sock(conn["sock"])
        for key in ("read_hook", "write_hook"):
            if conn.get(key):
                weechat.unhook(conn[key])
                conn[key] = None
        if conn in self.pending:
            self.pending.remove(conn)
        if conn in self.clients:
            self.clients.remove(conn)
        session = conn.get("session")
        if session is not None:
            session.conn = None
            conn["session"] = None
            session.handle_disconnect()

    def _unhook_timer(self, conn):
        if conn.get("timer"):
            try:
                weechat.unhook(conn["timer"])
            except Exception:
                pass
            conn["timer"] = None

    # ------------------------------------------------------------- sending

    def _send_raw(self, conn_sock, obj):
        """Send one message on a not-yet-tracked socket (e.g. rejections)."""
        try:
            conn_sock.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        except OSError:
            pass

    def _send_to(self, conn, obj):
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        conn["outq"] += line
        dbg(">> send %s to %s (%d bytes, outq=%d)"
            % (obj.get("type"), conn.get("peer"), len(line), len(conn["outq"])))
        # Flush synchronously: do not rely on the write-readiness hook to
        # fire — if it ever doesn't, outbound messages (input, pongs) would
        # sit in outq forever. The hook is kept only as a backpressure
        # fallback for the rare case the socket buffer is full.
        self._try_flush(conn)

    def _try_flush(self, conn):
        while conn["outq"]:
            try:
                n = conn["sock"].send(conn["outq"])
            except BlockingIOError:
                dbg("flush: backpressure (outq=%d), waiting for write hook"
                    % len(conn["outq"]))
                if conn["write_hook"] is None:
                    conn["write_hook"] = weechat.hook_fd(
                        conn["fd"], 0, 1, 0, "pi_write_cb", str(conn["fd"]))
                return
            except OSError as err:
                dbg("flush: send error %s — dropping client" % err)
                self.drop_conn(conn)
                return
            conn["outq"] = conn["outq"][n:]
        if conn["write_hook"]:
            weechat.unhook(conn["write_hook"])
            conn["write_hook"] = None

    def flush_outq(self, data, fd):
        # write-readiness event: only matters after a backpressure pause
        conn = self._conn_by_fd(int(data) if data else fd)
        if conn is not None:
            dbg("write_cb fired (outq=%d)" % len(conn["outq"]))
            self._try_flush(conn)

    # -------------------------------------------------------------- cleanup

    def cleanup(self):
        self.stop_tcp_server()
        for conn in list(self.pending):
            self.drop_conn(conn)
        for conn in list(self.clients):
            self.drop_conn(conn)
        if self.listen_sock is not None:
            try:
                self.listen_sock.close()
            except OSError:
                pass
            self.listen_sock = None
        if self.listen_hook:
            weechat.unhook(self.listen_hook)
            self.listen_hook = None
        if self.config_hook:
            weechat.unhook(self.config_hook)
            self.config_hook = None
        if self.nick_config_hook:
            weechat.unhook(self.nick_config_hook)
            self.nick_config_hook = None
        if self.tick_hook:
            weechat.unhook(self.tick_hook)
            self.tick_hook = None
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass


def format_tool_args(name, args):
    """Human-oriented summary of a tool call's args (one line).

    Known tools show their main content (bash: command, edit: path + number
    of edits, memory_write: target + content, …); unknown tools get compact
    key=value pairs. Values longer than ARG_VALUE_LIMIT characters are
    clipped with a "…(+N)" marker. Returns "" when there is nothing to show.
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
    session = BRIDGE.session_by_buffer.get(buffer)
    if session is not None:
        session.on_input(line)
    return weechat.WEECHAT_RC_OK


def pi_close_cb(data, buffer):
    session = BRIDGE.session_by_buffer.pop(buffer, None)
    if session is not None:
        session.alive = False
    return weechat.WEECHAT_RC_OK


def pi_listen_cb(data, fd):
    BRIDGE.accept_pending()
    return weechat.WEECHAT_RC_OK


def pi_tcp_listen_cb(data, fd):
    BRIDGE.accept_tcp()
    return weechat.WEECHAT_RC_OK


def pi_client_cb(data, fd):
    BRIDGE.client_event(data, int(fd))
    return weechat.WEECHAT_RC_OK


def pi_write_cb(data, fd):
    BRIDGE.flush_outq(data, int(fd))
    return weechat.WEECHAT_RC_OK


def pi_auth_timeout_cb(data, *args):
    BRIDGE.auth_timeout(int(data))
    return weechat.WEECHAT_RC_OK

def pi_tick_cb(data, *args):
    """1s timer: refresh the buffer-title turn counter while a request runs."""
    for session in BRIDGE.sessions:
        session.tick()
    return weechat.WEECHAT_RC_OK


def pi_config_cb(data, option, *args):
    """Fired on any plugins.var.python.pi_bridge.* option change.

    tcp_listen changes rebind the listener live (urlserver.py pattern);
    token/allowed_ips need no restart — they apply per connection/accept.
    """
    name = option.rsplit(".", 1)[-1] if option else ""
    if name == "tcp_listen":
        new = BRIDGE._opt("tcp_listen")
        if new != BRIDGE.tcp_listen_value:
            BRIDGE.tcp_listen_value = new
            BRIDGE.stop_tcp_server()
            if new:
                try:
                    BRIDGE.make_tcp_server()
                except OSError as err:
                    BRIDGE._print_all(C_ERR + "pi bridge: cannot listen on tcp "
                                             "%s (%s)%s" % (new, err, R))
            else:
                BRIDGE._print_all(C_STATUS + "tcp listener stopped%s" % R)
    if name == "markdown":
        # /set changed the mode under WeeChat's hands; whatever is buffered was
        # built under the old one, so print it before the switch takes effect
        for session in BRIDGE.sessions:
            session._flush_md_block()
    if name in ("tcp_listen", "token"):
        BRIDGE.config_warnings()
    return weechat.WEECHAT_RC_OK


def pi_nick_cb(data, option, *args):
    """Fired when irc.server_default.nicks changes (IRC plugin loaded).

    Re-applies (or clears) the buffer nick localvar live — no reload.
    """
    for session in BRIDGE.sessions:
        session.apply_user_nick()
    return weechat.WEECHAT_RC_OK


def pi_signal_cb(data, signal, *args):
    BRIDGE.cleanup()
    return weechat.WEECHAT_RC_OK


def pi_shutdown_cb():
    # /python unload pi_bridge → release fd hooks + unlink the socket so a
    # fresh load can rebind cleanly
    BRIDGE.cleanup()
    return weechat.WEECHAT_RC_OK


# -------------------------------------------------------------------- main

def main():
    dbg("main(): loading (sock=%s, debug=%s)" % (default_socket_path(), bool(_DBG_PATH)))
    weechat.register("pi_bridge", "simeng", "0.6.0", "MIT",
                     "mirror a pi coding agent session through a WeeChat buffer",
                     "pi_shutdown_cb", "")
    # plugin options: PLUGIN_OPTIONS = (name, default, description).
    # Defaults are auto-created on first run (/set plugins.var.python.pi_bridge.<name> …).
    # WeeChat options only — no env-var fallback on this side; ${sec.data.x}
    # references are expanded by WeeChat at read time. Descriptions feed
    # /help set plugins.var.python.pi_bridge.<name> (config_set_desc_plugin, spotify.py pattern).
    for name, default, description in PLUGIN_OPTIONS:
        if not weechat.config_is_set_plugin(name):
            weechat.config_set_plugin(name, default)
        weechat.config_set_desc_plugin(name, description)
    BRIDGE.make_buffer()
    # 1s tick timer: refresh the buffer-title turn counter while a request runs
    BRIDGE.tick_hook = weechat.hook_timer(1000, 0, 0, "pi_tick_cb", "")
    try:
        BRIDGE.make_unix_server()
    except OSError as err:
        weechat.prnt("", C_ERR + "pi_bridge: cannot listen on %s (%s)%s"
                     % (BRIDGE.sock_path, err, R))
    BRIDGE.config_hook = weechat.hook_config(
        "plugins.var.python.pi_bridge.*", "pi_config_cb", "")
    BRIDGE.nick_config_hook = weechat.hook_config(
        "irc.server_default.nicks", "pi_nick_cb", "")
    BRIDGE.tcp_listen_value = BRIDGE._opt("tcp_listen")
    if BRIDGE.tcp_listen_value:
        try:
            BRIDGE.make_tcp_server()
        except OSError as err:
            weechat.prnt("", C_ERR + "pi_bridge: cannot listen on tcp %s (%s)%s"
                         % (BRIDGE.tcp_listen_value, err, R))
    BRIDGE.config_warnings()
    weechat.hook_signal("quit;upgrade", "pi_signal_cb", "")


if weechat is not None:
    main()
