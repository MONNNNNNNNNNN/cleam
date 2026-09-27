"""Cleam in the terminal: an ASCII menu with checkboxes, for keyboard and mouse.

Standard library only -- no curses, which Windows Python does not ship.

- **Input.** On Windows, raw console input records (ReadConsoleInputW): key
  events *and* mouse events. msvcrt.getwch() cannot see the mouse at all, and
  the console's Quick Edit mode swallows clicks for text selection, so it is
  switched off while the menu runs and the old mode is put back on exit.
  Elsewhere, termios cbreak plus xterm SGR mouse reporting (ESC[<b;x;yM).
- **Output.** ANSI escape codes in the alternate screen buffer, one write per
  frame (no flicker, the user's scrollback is untouched), every line clipped
  to the visible width so nothing wraps and shifts the frame.
- **Clicks** are hit-tested against the spans the last frame drew: a row, a
  group title, a button in the bar at the bottom.

Long jobs show a progress bar inside the menu. Everything they start
(PowerShell, pnputil, the restore point) runs with no window of its own
(system.NO_WINDOW), so nothing else appears on screen.

Like the window, this front end decides nothing: it lists what debloat, junk
and security report, and calls the same functions the CLI does.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from dataclasses import dataclass

from . import __version__, debloat, junk, security, snapshot
from .system import OS, human, is_admin, relaunch_as_admin

# Three symbol sets, picked by what the terminal can actually draw. Checked
# against the font files: Consolas (the classic console's default) and Lucida
# Console have box drawing, blocks and ■ ► √ · × ○ ♦ ¤ ≈ ∞ ▬ ± ♣ ▼ ░ ◘ ◄ ↑,
# but no ✓ ✗ ☐ ☑ ⚠ -- those would print as empty boxes, because the classic
# console has no font fallback. Windows Terminal does fall back, so it gets
# the rich set. Emoji are never used: they are two cells wide and break every
# column.
@dataclass(frozen=True)
class Glyphs:
    on: str
    off: str
    done: str
    na: str
    cursor: str
    ok: str
    fail: str
    warn: str
    full: str
    empty: str
    tl: str
    tr: str
    bl: str
    br: str
    h: str
    v: str
    sep: str
    icons: dict


ICONS = {
    "Power & storage": "■", "Privacy": "○", "Ads & suggestions": "¤", "Search & Start": "≈", "AI & Copilot": "∞",
    "Taskbar & Explorer": "▬", "Gaming & comfort": "♦", "Services": "±", "Apps": "♣",
    "System": "■", "Browsers": "○", "Developer tools": "±", "Recycle Bin": "×",
    "debloat": "▼", "clean": "░", "security": "◘", "undo": "◄", "admin": "↑", "quit": "×",
}
GLYPH_SETS = {
    "rich": Glyphs("☑", "☐", "✓", "·", "▌", "✓", "✗", "⚠", "█", "░", "╭", "╮", "╰", "╯", "─", "│", "·", ICONS),
    "console": Glyphs("■", " ", "√", "·", "▌", "√", "×", "!", "█", "░", "┌", "┐", "└", "┘", "─", "│", "·", ICONS),
    "ascii": Glyphs("x", " ", "+", "-", ">", "+", "x", "!", "#", "-", "+", "+", "+", "+", "-", "|", "-",
                    {k: "*" for k in ICONS}),
}


def pick_glyphs() -> Glyphs:
    wanted = os.environ.get("CLEAM_GLYPHS", "").lower()
    if wanted in GLYPH_SETS:
        return GLYPH_SETS[wanted]
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    if OS == "windows":
        # The console API writes UTF-16 whatever the code page; what varies is the font.
        return GLYPH_SETS["rich" if os.environ.get("WT_SESSION") else "console"]
    if "utf" in encoding and os.environ.get("TERM") != "linux":  # the Linux text console has no such glyphs
        return GLYPH_SETS["rich"]
    return GLYPH_SETS["ascii"]


G = pick_glyphs()

# "ANSI Shadow" block letters: every character in them is in Consolas and Lucida.
BANNER = [
    " ██████╗██╗     ███████╗ █████╗ ███╗   ███╗",
    "██╔════╝██║     ██╔════╝██╔══██╗████╗ ████║",
    "██║     ██║     █████╗  ███████║██╔████╔██║",
    "██║     ██║     ██╔══╝  ██╔══██║██║╚██╔╝██║",
    "╚██████╗███████╗███████╗██║  ██║██║ ╚═╝ ██║",
    " ╚═════╝╚══════╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝",
]
BANNER_ASCII = [
    "   ____ _",
    "  / ___| | ___  __ _ _ __ ___",
    " | |   | |/ _ \\/ _` | '_ ` _ \\",
    " | |___| |  __/ (_| | | | | | |",
    "  \\____|_|\\___|\\__,_|_| |_| |_|",
]
BANNER_SHADES = (51, 45, 39, 33, 27, 21)  # 256-colour cyan to blue, one per line


UP, DOWN, PGUP, PGDN, HOME, END, ENTER, ESC, SPACE, TAB, RESIZE = (
    "up", "down", "pgup", "pgdn", "home", "end", "enter", "esc", "space", "tab", "resize")


@dataclass(frozen=True)
class Click:
    x: int
    y: int  # row within the visible screen, 0 = the title bar


@dataclass(frozen=True)
class Wheel:
    steps: int  # negative = up


COLOR = not os.environ.get("NO_COLOR")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
BOLD, DIM, REVERSE = 1, 2, 7
RED, GREEN, YELLOW, CYAN = 31, 32, 33, 36
# Semantic colours (bright variants read better on the usual black console).
# A state is never colour alone: every one also has a word or a symbol.
ACCENT, OK_C, WARN_C, BAD_C, MUTED = 96, 92, 93, 91, 90
ON_ACCENT = (46, 30)  # black on cyan: the title bar and the primary button
CURRENT_BG = "48;5;236"  # the highlighted row: a dark grey band


def style(text: str, *codes) -> str:
    return f"\x1b[{';'.join(map(str, codes))}m{text}\x1b[0m" if COLOR and codes else text


def clip(line: str, width: int) -> str:
    """Cut a line to `width` visible characters; colour codes do not count.

    A line one character too wide wraps in the terminal and pushes the whole
    frame down, so every line is clipped before it is drawn.
    """
    out, seen, pos = [], 0, 0
    for m in ANSI.finditer(line):
        take = line[pos:m.start()][: max(0, width - seen)]
        out += [take, m.group()]
        seen += len(take)
        pos = m.end()
    out.append(line[pos:][: max(0, width - seen)])
    return "".join(out)


def pad(text: str, width: int) -> str:
    return text[:width] + " " * max(0, width - len(text))


# ------------------------------------------------------------------ terminal


class Terminal:
    def __enter__(self) -> "Terminal":
        if OS == "windows":
            self._win_setup()
        else:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            sys.stdout.write("\x1b[?1000h\x1b[?1006h")  # report clicks and wheel, SGR coordinates
        sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
        sys.stdout.flush()
        return self

    def __exit__(self, *exc) -> None:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        if OS == "windows":
            self._k32.SetConsoleMode(self._in, self._in_mode)
        else:
            import termios

            sys.stdout.write("\x1b[?1000l\x1b[?1006l")
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        sys.stdout.flush()

    # ---- Windows console

    def _win_setup(self) -> None:
        import ctypes

        k32 = self._k32 = ctypes.windll.kernel32
        out = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(out, ctypes.byref(mode)):
            k32.SetConsoleMode(out, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        self._out = out
        self._in = k32.GetStdHandle(-10)
        k32.GetConsoleMode(self._in, ctypes.byref(mode))
        self._in_mode = mode.value
        # + ENABLE_MOUSE_INPUT | ENABLE_WINDOW_INPUT | ENABLE_EXTENDED_FLAGS,
        # - ENABLE_QUICK_EDIT_MODE (it eats clicks), - line/echo input.
        new = (mode.value | 0x0010 | 0x0008 | 0x0080) & ~0x0040 & ~0x0002 & ~0x0004
        k32.SetConsoleMode(self._in, new)

    def _win_event(self):
        import ctypes
        from ctypes import wintypes

        class KEY(ctypes.Structure):
            _fields_ = [("down", wintypes.BOOL), ("repeat", wintypes.WORD), ("vk", wintypes.WORD),
                        ("scan", wintypes.WORD), ("char", wintypes.WCHAR), ("state", wintypes.DWORD)]

        class MOUSE(ctypes.Structure):
            _fields_ = [("x", wintypes.SHORT), ("y", wintypes.SHORT), ("buttons", wintypes.DWORD),
                        ("state", wintypes.DWORD), ("flags", wintypes.DWORD)]

        class EVENT(ctypes.Union):
            _fields_ = [("key", KEY), ("mouse", MOUSE), ("pad", ctypes.c_byte * 16)]

        class RECORD(ctypes.Structure):
            _fields_ = [("type", wintypes.WORD), ("event", EVENT)]

        rec, n = RECORD(), wintypes.DWORD()
        while True:
            if not self._k32.ReadConsoleInputW(self._in, ctypes.byref(rec), 1, ctypes.byref(n)):
                return ESC
            if rec.type == 1 and rec.event.key.down:  # KEY_EVENT
                key = rec.event.key
                named = {0x26: UP, 0x28: DOWN, 0x21: PGUP, 0x22: PGDN, 0x24: HOME, 0x23: END,
                         0x0D: ENTER, 0x1B: ESC, 0x20: SPACE, 0x09: TAB}.get(key.vk)
                if named:
                    return named
                if key.char == "\x03":
                    raise KeyboardInterrupt
                if key.char and key.char != "\x00":
                    return key.char.lower()
            elif rec.type == 2:  # MOUSE_EVENT
                m = rec.event.mouse
                top = self._window_top()
                if m.flags == 0 and m.buttons & 1:  # left button pressed
                    return Click(m.x, m.y - top)
                if m.flags == 4:  # wheel: the high word of buttons is the signed delta
                    delta = ctypes.c_short(m.buttons >> 16).value
                    return Wheel(-3 if delta > 0 else 3)
            elif rec.type == 4:  # WINDOW_BUFFER_SIZE_EVENT
                return RESIZE

    def _window_top(self) -> int:
        """Mouse events carry buffer coordinates; the visible screen may start lower."""
        import ctypes
        from ctypes import wintypes

        class INFO(ctypes.Structure):
            _fields_ = [("size", wintypes._COORD), ("cursor", wintypes._COORD), ("attr", wintypes.WORD),
                        ("window", wintypes.SMALL_RECT), ("max", wintypes._COORD)]

        info = INFO()
        if self._k32.GetConsoleScreenBufferInfo(self._out, ctypes.byref(info)):
            return info.window.Top
        return 0

    # ---- POSIX

    def _posix_event(self):
        ch = sys.stdin.read(1)
        if ch != "\x1b":
            if ch == "\x03":
                raise KeyboardInterrupt
            return {"\n": ENTER, "\r": ENTER, " ": SPACE, "\t": TAB}.get(ch, ch.lower())
        import select

        if not select.select([sys.stdin], [], [], 0.05)[0]:
            return ESC
        seq = sys.stdin.read(1)
        if seq != "[":
            return ESC
        rest = sys.stdin.read(1)
        if rest == "<":  # SGR mouse: ESC [ < b ; x ; y (M|m)
            data = ""
            while (c := sys.stdin.read(1)) not in "Mm":
                data += c
            b, x, y = (int(v) for v in data.split(";"))
            if c == "M" and b == 0:
                return Click(x - 1, y - 1)
            if b in (64, 65):
                return Wheel(-3 if b == 64 else 3)
            return RESIZE  # a release or a drag: just redraw
        if rest in "56":
            sys.stdin.read(1)  # the ~ of PgUp/PgDn
        return {"A": UP, "B": DOWN, "5": PGUP, "6": PGDN, "H": HOME, "F": END}.get(rest, RESIZE)

    # ---- shared

    @staticmethod
    def size() -> tuple[int, int]:
        cols, rows = shutil.get_terminal_size((100, 30))
        return max(60, cols), max(20, rows)

    def draw(self, lines: list[str]) -> None:
        cols, rows = self.size()
        out = ["\x1b[H"]
        for line in lines[:rows]:
            out.append(clip(line, cols) + "\x1b[0m\x1b[K\n")
        out.append("\x1b[J")
        sys.stdout.write("".join(out).rstrip("\n"))
        sys.stdout.flush()

    def key(self):
        return self._win_event() if OS == "windows" else self._posix_event()


# ------------------------------------------------------------------ building blocks


@dataclass
class Row:
    label: str
    checked: bool = False
    disabled: bool = False
    status: str = ""  # right-hand column: a size, "already set", "needs admin"...
    detail: str = ""  # shown under the list for the highlighted row
    header: bool = False  # a group title, not selectable itself
    data: object = None
    default: bool = False  # ticked by 'recommended'
    # What the box showed when the list was read from the system. A list that
    # mirrors live state (Debloat) starts with was == checked, and only rows
    # whose box now differs are changes: ticked = turn on, unticked = turn off.
    was: bool = False


STATUS = ""  # "Windows 10 Pro · build 19045 · admin √", set once by main_menu


def title_bar(title: str) -> str:
    cols, _ = Terminal.size()
    left = f" {ICONS['debloat'] if G is not GLYPH_SETS['ascii'] else '*'} CLEAM {__version__}  {G.sep}  {title} "
    right = f" {STATUS} " if STATUS else ""
    gap = max(1, cols - len(left) - len(right))
    # No bold on the accent background: the classic console draws bold black as grey.
    return style(left + " " * gap + right, *ON_ACCENT) if COLOR else pad(left + " " * gap + right, cols)


def button_bar(y: int, buttons: list[tuple[str, object]], hint: str = "") -> tuple[str, list]:
    """A line of clickable buttons, the first one primary, and the spans to hit-test them."""
    parts, spans, x = [], [], 1
    for n, (label, action) in enumerate(buttons):
        text = f" {label} " if COLOR else f"[ {label} ]"
        parts.append(style(text, *ON_ACCENT) if n == 0 else style(text, 100, 97))
        spans.append((y, x, x + len(text), action))
        x += len(text) + 2
    line = " " + "  ".join(parts)
    if hint:
        line += "   " + style(hint, MUTED)
    return line, spans


def panel(title: str, body: list[str], width: int) -> list[str]:
    """A thin frame with a title in its top edge: the one box on a screen."""
    inner = width - 4
    top = f" {G.tl}{G.h} {title} " + G.h * max(0, inner - len(title) - 2) + G.tr
    lines = [style(top, MUTED)]
    for text in body:
        lines.append(style(f" {G.v} ", MUTED) + pad(text, inner) + style(G.v, MUTED))
    lines.append(style(f" {G.bl}" + G.h * (inner + 1) + G.br, MUTED))
    return lines


def hit(spans: list, click: Click):
    for y, x0, x1, action in spans:
        if click.y == y and x0 <= click.x < x1:
            return action
    return None


def progress_bar(done: int, total: int, width: int) -> str:
    """The coloured form: filled cells in the accent colour, the rest muted."""
    total = max(1, total)
    bar_w = max(10, min(46, width - 26))
    filled = round(bar_w * min(done, total) / total)
    pct = round(100 * min(done, total) / total)
    return (style(G.full * filled, ACCENT) + style(G.empty * (bar_w - filled), MUTED)
            + style(f"  {pct:3d}%", BOLD) + style(f"  {done} of {total}", MUTED))


class Progress:
    """A progress bar, the step being worked on, and a short log -- all inside the menu."""

    def __init__(self, term, title: str, total: int = 0) -> None:
        self.term, self.title, self.total = term, title, total
        self.done = 0
        self.now = ""
        self.lines: list[str] = []

    def step(self, now: str) -> None:
        """Announce the next step (before doing it)."""
        self.now = now
        self._draw()

    def add(self, line: str, advance: bool = True) -> None:
        self.lines.append(line)
        if advance:
            self.done += 1
        self._draw()

    def _draw(self) -> None:
        cols, height = Terminal.size()
        body = [title_bar(self.title), ""]
        if self.total:
            body.append("  " + progress_bar(self.done, self.total, cols))
        working = self.now and self.done < max(1, self.total)
        body.append("  " + (style(f"{G.cursor} {self.now}", ACCENT, BOLD) if working else ""))
        body.append("")
        body += ["  " + x for x in self.lines[-(height - 7):]]
        self.term.draw(body)


# ------------------------------------------------------------------ screens: generic


def checklist(term, title: str, intro: str, rows: list[Row], action: str = "Continue",
              reset_label: str = "None") -> list[Row] | None:
    """Tick things with Space or a click. Returns the rows whose box changed, None on Back.

    For a plain list every row starts unticked (was=False), so "changed" is
    simply "ticked".
    """
    selectable = [i for i, r in enumerate(rows) if not r.header]
    if not selectable:
        message(term, title, [intro, "", "Nothing to show here."])
        return None
    pos, top = 0, 0

    def group_members(header_index: int) -> list[Row]:
        out = []
        for r in rows[header_index + 1:]:
            if r.header:
                break
            if not r.disabled:
                out.append(r)
        return out

    while True:
        cols, height = Terminal.size()
        cur = selectable[pos]
        detail_lines = textwrap.wrap(rows[cur].detail, cols - 8)[:3] if rows[cur].detail else []
        ticked = sum(1 for r in rows if r.checked != r.was and not r.disabled and not r.header)
        head = 2 + (1 if intro else 0)
        panel_h = len(detail_lines) + 2 if detail_lines else 0
        list_height = max(5, height - head - 2 - panel_h)
        if cur < top:
            top = cur
        if cur >= top + list_height:
            top = cur - list_height + 1
        lines = [title_bar(title), ""] + ([style("  " + intro, MUTED)] if intro else [])
        spans: list = []
        status_w = 24
        for i in range(top, min(len(rows), top + list_height)):
            r, y = rows[i], len(lines)
            spans.append((y, 0, cols, ("row", i)))
            if r.header:
                lines.append(group_line(r, group_members(i), rows, i, cols))
                continue
            lines.append(row_line(r, i == cur, cols, status_w))
        while len(lines) < head + list_height:
            lines.append("")
        if detail_lines:
            about = rows[cur].label if len(rows[cur].label) < cols - 20 else "About"
            lines += panel(about, detail_lines, cols)
        bar, bar_spans = button_bar(len(lines), [(f"{action} ({ticked})", "go"), ("Recommended", "a"),
                                                 (reset_label, "n"), ("Back", "back")],
                                    f"click a row {G.sep} ↑↓ move {G.sep} Space tick {G.sep} Enter {action.lower()}")
        lines.append(bar)
        spans += bar_spans
        term.draw(lines)
        k = term.key()
        if isinstance(k, Click):
            what = hit(spans, k)
            if what is None:
                continue
            if isinstance(what, tuple):
                i = what[1]
                if rows[i].header:
                    members = group_members(i)
                    on = not all(r.checked for r in members)
                    for r in members:
                        r.checked = on
                    continue
                pos = selectable.index(i)
                if not rows[i].disabled:
                    rows[i].checked = not rows[i].checked
                continue
            k = {"go": ENTER, "back": ESC}.get(what, what)
        if isinstance(k, Wheel):
            pos = max(0, min(len(selectable) - 1, pos + k.steps))
        elif k in (UP, "k"):
            pos = max(0, pos - 1)
        elif k in (DOWN, "j"):
            pos = min(len(selectable) - 1, pos + 1)
        elif k == PGUP:
            pos = max(0, pos - list_height)
        elif k == PGDN:
            pos = min(len(selectable) - 1, pos + list_height)
        elif k == HOME:
            pos = 0
        elif k == END:
            pos = len(selectable) - 1
        elif k in (SPACE, "x") and not rows[cur].disabled:
            rows[cur].checked = not rows[cur].checked
        elif k == "a":
            for r in rows:
                if not r.disabled and not r.header:
                    r.checked = r.default or r.was  # recommended on top; nothing already on is dropped
        elif k == "n":
            for r in rows:
                r.checked = r.was  # back to what the system has now
        elif k == ENTER:
            return [r for r in rows if r.checked != r.was and not r.disabled and not r.header]
        elif k in (ESC, "q"):
            return None


def group_line(r: Row, members: list[Row], rows: list[Row], index: int, cols: int) -> str:
    """ ○ PRIVACY ─────────────────────── 3 of 14 ticked · click to tick all"""
    count = 0
    for other in rows[index + 1:]:
        if other.header:
            break
        count += 1
    ticked = sum(1 for m in members if m.checked)
    icon = G.icons.get(r.label, G.icons.get(r.label.split(" (")[0], "*"))
    left = f" {icon} {r.label.upper()} "
    right = f" {ticked} of {count} ticked" + (f" {G.sep} click to tick all " if members else " ")
    fill = G.h * max(1, cols - len(left) - len(right))
    return style(left, ACCENT, BOLD) + style(fill, MUTED) + style(right, MUTED)


def row_line(r: Row, current: bool, cols: int, status_w: int) -> str:
    """  ▌ ■ Title .................................. √ already set"""
    if r.disabled:
        box = G.done if r.status in ("on", "already set") else G.na
    else:
        box = G.on if r.checked else G.off
    mark = f"[{box}]" if G is not GLYPH_SETS["rich"] else f" {box} "
    cursor = G.cursor if current else " "
    status = r.status
    status_code = MUTED
    off_mark = "-" if G is GLYPH_SETS["ascii"] else "○"
    arrow = "->" if G is GLYPH_SETS["ascii"] else "→"
    if not r.disabled and r.checked != r.was and status in ("on", "off", "partly"):
        # A pending change, shown next to what the system has now.
        status, status_code = f"{arrow} turn {'on' if r.checked else 'off'}", ACCENT
    elif status == "on":
        status, status_code = f"{G.done} ON", OK_C
    elif status == "off":
        status = f"{off_mark} off"
    elif status == "partly":
        status, status_code = "± PARTLY", WARN_C
    elif status in ("already set",):
        status, status_code = f"{G.done} already set", OK_C
    elif status in ("partly set", "needs admin"):
        status_code = WARN_C
    elif r.detail.endswith("[moderate: read before ticking]") and not r.disabled:
        status, status_code = f"{G.warn} read first", WARN_C
    elif status and status[0].isdigit():
        status_code = ACCENT  # a size
    label_w = cols - status_w - 8
    text = f" {mark} " + pad(r.label, label_w)
    body = style(text, MUTED) if r.disabled else style(text, OK_C, BOLD) if r.checked else text
    line = style(f" {cursor}", ACCENT, BOLD) + body + " " + style(pad(status, status_w), status_code)
    return f"\x1b[{CURRENT_BG}m" + line.replace("\x1b[0m", f"\x1b[0m\x1b[{CURRENT_BG}m") + " " * 2 + "\x1b[0m" \
        if current and COLOR else line


def message(term, title: str, text: list[str], buttons: list[tuple[str, object]] | None = None,
            hint: str = ""):
    """Scrollable text with buttons. Returns the button's action (or the key pressed)."""
    buttons = buttons or [("OK", "ok")]
    top = 0
    while True:
        cols, height = Terminal.size()
        wrapped: list[str] = []
        for line in text:
            if line.startswith("\x1b"):  # a styled heading: short, never wrapped
                wrapped.append(line)
            else:
                wrapped += textwrap.wrap(line, cols - 4, subsequent_indent="    ") or [""]
        view = height - 4
        top = max(0, min(top, len(wrapped) - view))
        lines = [title_bar(title), ""] + ["  " + w for w in wrapped[top:top + view]]
        while len(lines) < 2 + view:
            lines.append("")
        # A caller's hint always shows ("Y or click Yes" was hidden on short
        # questions); the scrolling hint only when there is something to scroll.
        scroll = "Up/Down or wheel to scroll" if len(wrapped) > view else ""
        bar, spans = button_bar(len(lines), buttons, "  ".join(h for h in (hint, scroll) if h))
        term.draw(lines + [bar])
        k = term.key()
        if isinstance(k, Click):
            action = hit(spans, k)
            if action is not None:
                return action
            continue
        if isinstance(k, Wheel):
            top += k.steps
        elif k in (UP, "k"):
            top -= 1
        elif k in (DOWN, "j"):
            top += 1
        elif k == PGDN:
            top += view
        elif k == PGUP:
            top -= view
        elif k == RESIZE:
            continue
        else:
            return k


def confirm(term, title: str, text: list[str], question: str) -> bool:
    """Only an explicit Yes -- click or Y -- goes ahead."""
    answer = message(term, title, text + ["", style(f"{G.warn} {question}", BOLD, WARN_C)], [("Yes", "y"), ("No", "no")],
                     "Y or click Yes; anything else is No")
    return answer == "y"


# ------------------------------------------------------------------ screens: Cleam


def mark(ok: bool) -> str:
    return style(f"{G.ok} ", OK_C, BOLD) if ok else style(f"{G.fail} ", BAD_C, BOLD)


def debloat_screen(term) -> None:
    """Every tweak shows what the system has now (ON / off / PARTLY); the boxes
    start as that, and only what you change is applied or reverted. After a
    run the list is read again from the system, so it stays in sync."""
    while True:
        result = _debloat_round(term)
        if result is None:
            return


def _debloat_round(term):
    progress = Progress(term, "Debloat Windows", 2)
    progress.step("Reading what is set on this PC")
    engine = debloat.Debloater()
    env = engine.env
    states = engine.states()
    progress.add("settings read")
    progress.step("Looking for removable apps")
    apps = engine.removable_apps()
    progress.add(f"{len(apps)} removable apps")
    rows: list[Row] = []
    for group in debloat.GROUPS:
        if group == "Apps":
            continue
        rows.append(Row(group, header=True))
        for t in (t for t in debloat.TWEAKS if t.group == group):
            state = states[t.id]
            # "n/a": nothing it touches exists here (a service this Windows
            # lacks), so there is nothing to switch.
            why = debloat.availability(t, env) or ("not on this PC" if state == "n/a" else "")
            on = state == "applied"
            status = why or {"applied": "on", "partly": "partly"}.get(state, "off")
            extra = f" Takes effect after: {t.restart}." if t.restart else ""
            risk = " [moderate: read before ticking]" if t.risk == debloat.MODERATE else ""
            rows.append(Row(t.title, checked=on, was=on, disabled=bool(why), status=status,
                            detail=t.about + extra + risk, data=t, default=t.default and not why))
    if apps:
        # Undo re-registers the app; Windows deletes an app's own data on removal.
        rows.append(Row("Apps: tick to remove (undo reinstalls the app, not its data)", header=True))
        for app, _ in apps:
            rows.append(Row(app.name, status="installed",
                            detail=f"{app.about}. Package {app.id}." + ("" if app.default else " [optional]"),
                            data=app, default=app.default))
    edition = f"Windows {env.windows} {debloat.edition_name(env.edition)} (build {env.build})"
    on_now = sum(1 for r in rows if r.was)
    admin = "" if env.admin else "  Not administrator: machine-wide items are greyed out."
    intro = f"{edition}. {on_now} already on. Tick to turn on, untick to turn off.{admin}"
    changed = checklist(term, "Debloat Windows", intro, rows, "Review", reset_label="As is")
    if not changed:
        return None
    turn_on = [r.data for r in changed if isinstance(r.data, debloat.Tweak) and r.checked]
    turn_off = [r.data for r in changed if isinstance(r.data, debloat.Tweak) and not r.checked]
    to_remove = [r.data for r in changed if isinstance(r.data, debloat.App)]
    summary = ([f"  + {t.title}" for t in turn_on] + [f"  - turn off: {t.title}" for t in turn_off]
               + [f"  - remove app: {a.name}" for a in to_remove])
    note = ("Cleam's own changes are undone exactly; ones made by another tool go back to Windows' defaults."
            if turn_off else "Every change is recorded and can be undone.")
    if not confirm(term, "Debloat Windows: review", ["These changes will be made:"] + summary + ["", note],
                   "Apply them now?"):
        return True  # back to the list
    make_point = env.admin and OS == "windows" and confirm(term, "Restore point", [
        "A restore point lets Windows roll everything back if something goes wrong.",
        "Windows allows one every 24 hours; creating it takes a minute or two."], "Create a restore point first?")
    progress = Progress(term, "Debloat Windows", len(changed) + (1 if make_point else 0))
    if make_point:
        progress.step("Creating a restore point (a minute or two)")
        code, text = snapshot.create("Cleam: before debloat", capture=True)
        progress.add(mark(code == 0) + ("restore point created" if code == 0 else f"restore point failed: {text}"))
        if code != 0 and not confirm(term, "Restore point failed", [text], "Continue without one?"):
            return True
    restarts: set[str] = set()
    failures = 0
    for t, verb in [(t, "on") for t in turn_on] + [(t, "off") for t in turn_off]:
        progress.step(f"Turning {verb}: {t.title}")
        out = engine.apply(t) if verb == "on" else engine.revert(t)
        failures += not out.ok
        if out.ok and out.restart:
            restarts.add(out.restart)
        progress.add(mark(out.ok) + f"{verb:3} {t.title}" + ("" if out.ok else f"  ({out.message})"))
    for app in to_remove:
        progress.step(f"Removing {app.name}")
        out = engine.remove_app(app)
        failures += not out.ok
        progress.add(mark(out.ok) + f"removed {app.name}" + ("" if out.ok else f"  ({out.message})"))
    end = [f"Done: {len(changed) - failures} changes made, {failures} failed. The list is read again next."]
    if "restart" in restarts:
        end.append("Some changes take effect after you restart Windows.")
    if "sign out" in restarts:
        end.append("Some take effect after you sign out and back in.")
    if "explorer" in restarts and confirm(term, "Debloat Windows", end + [
            "", "Taskbar and File Explorer changes need Explorer to restart (the taskbar blinks; open windows stay)."],
            "Restart Explorer now?"):
        debloat.restart_explorer()
    elif "explorer" not in restarts:
        message(term, "Debloat Windows", end + [""] + progress.lines)
    return True  # read the system again and show the list in sync


def undo_screen(term) -> None:
    engine = debloat.Debloater()
    journal = engine.applied()
    titles = {t.id: t.title for t in debloat.TWEAKS}
    rows: list[Row] = []
    if journal["tweaks"]:
        rows.append(Row("Settings Cleam changed", header=True))
        rows += [Row(titles.get(i, i), status=e["applied"][:10], detail="Undo puts back exactly what was there before.",
                     data=("tweak", i)) for i, e in journal["tweaks"].items()]
    if journal["apps"]:
        rows.append(Row("Apps Cleam removed", header=True))
        rows += [Row(e["name"], status=e["removed"][:10], detail="Registers the app again from the copy Windows kept.",
                     data=("app", i)) for i, e in journal["apps"].items()]
    chosen = checklist(term, "Undo debloat", "Tick what to put back.", rows, "Undo")
    if not chosen:
        return
    progress = Progress(term, "Undo debloat", len(chosen))
    for r in chosen:
        progress.step(r.label)
        kind, ident = r.data
        out = engine.undo(ident) if kind == "tweak" else engine.restore_app(ident)
        progress.add(mark(out.ok) + r.label + ("" if out.ok else f"  ({out.message})"))
    message(term, "Undo debloat", progress.lines + ["", "Restart Explorer or sign out to see taskbar changes."])


def clean_screen(term) -> None:
    present = [t for t in junk.targets() if junk.present(t)]  # once: targets() runs whoami
    progress = Progress(term, "Clean junk: scanning", len(present))
    rows: list[Row] = []
    for group in ("System", "Browsers", "Apps", "Developer tools", "Recycle Bin"):
        members = [t for t in present if t.group == group]
        if not members:
            continue
        rows.append(Row(group, header=True))
        for t in members:
            progress.step(f"Scanning {t.label}")
            r = junk.run(t)
            empty = bool(r.skipped) or not r.files
            size = r.skipped or (human(r.bytes) if r.files else "clean")
            progress.add(f"{t.label}: {size}")
            rows.append(Row(t.label, checked=not empty and not t.opt_in, disabled=empty, status=size,
                            detail=t.about + (" [opt-in]" if t.opt_in else ""), data=t,
                            default=not empty and not t.opt_in))
    total = sum(1 for r in rows if not r.header and not r.disabled)
    chosen = checklist(term, "Clean junk", f"{total} places with something to remove.", rows, "Clean")
    if not chosen:
        return
    if not confirm(term, "Clean junk", [f"  - {r.label}: {r.status}" for r in chosen] + ["", "This cannot be undone."],
                   "Delete these files?"):
        return
    progress = Progress(term, "Clean junk", len(chosen))
    freed = 0
    for r in chosen:
        progress.step(f"Cleaning {r.label}")
        res = junk.run(r.data, delete=True)
        freed += res.bytes
        progress.add(f"{r.label}: freed {human(res.bytes)}" + (f", {res.errors} left (in use)" if res.errors else ""))
    message(term, "Clean junk", [style(f"Freed {human(freed)} in total.", BOLD, GREEN), ""] + progress.lines)


def security_screen(term) -> None:
    progress = Progress(term, "System check", 3)
    progress.step("Checking protection, updates, hardware, health and gaming settings")
    checks = security.status()
    progress.add(f"{len(checks)} checks done")
    progress.step("Reading startup programs")
    items = security.startup()
    progress.add("startup programs read")
    progress.step("Looking for signs of ransomware in your folders")
    signs = security.ransom_signs()
    progress.add("folders checked")
    # Plain marks: a coloured prefix would stop message() from wrapping the line.
    tag = {security.OK: "[ ok ]", security.WARN: "[warn]", security.BAD: "[FAIL]", security.UNKNOWN: "[ ?? ]",
           security.INFO: "[info]"}
    bad = sum(c.state == security.BAD for c in checks)
    warn = sum(c.state == security.WARN for c in checks)
    text = [style(f"{G.icons['security']} {bad} problem(s), {warn} to check, {len(checks)} checks", ACCENT, BOLD), ""]
    group = ""
    for c in checks:
        if c.group != group:
            if group:
                text.append("")
            group = c.group
            text.append(style(f"{G.icons['security']} {group.upper()}", ACCENT, BOLD))
        text.append(f"{tag[c.state]} {c.label}: {c.detail}")
        if c.fix and c.state != security.OK:
            text.append(f"       fix: {c.fix}")
    text += ["", style(f"{G.icons['security']} RANSOMWARE ({signs.files_checked:,} files checked)", ACCENT, BOLD)]
    text += [f"{tag[signs.state]} {f}" for f in signs.findings()] or [f"{tag[security.OK]} no signs found"]
    text += ["", style(f"{G.icons['security']} STARTS WITH THE COMPUTER", ACCENT, BOLD)]
    for i in items:
        state = "" if i.enabled else " (off)"
        text.append(f"{tag[security.WARN if i.suspicious else security.OK]} {i.name}{state}"
                    + (f" - {i.publisher}" if i.publisher else ""))
        text += [f"       {reason}" for reason in i.reasons]
    message(term, "System check (read-only)", text, [("Back", "back")])


def main_menu(term) -> None:
    global STATUS
    items = [("debloat", "Debloat Windows", "Privacy, ads, AI features, clutter, apps, disk space", debloat_screen),
             ("clean", "Clean junk", "Caches, temp files, crash dumps, old logs", clean_screen),
             ("security", "System check", "Protection, updates, hardware, health, gaming, startup", security_screen),
             ("undo", "Undo debloat", "Put back anything Cleam changed", undo_screen)]
    if OS == "windows" and not is_admin():
        items.append(("admin", "Restart as administrator", "For machine-wide settings and system junk", None))
    items.append(("quit", "Quit", "", None))
    env = debloat.current_env() if OS == "windows" else None
    if env:
        STATUS = (f"Windows {env.windows} {debloat.edition_name(env.edition)} {G.sep} build {env.build} {G.sep} "
                  + (f"admin {G.ok}" if env.admin else "not admin"))
    else:
        STATUS = sys.platform
    pos = 0
    while True:
        cols, _ = Terminal.size()
        lines = [title_bar("Menu"), ""]
        if G is GLYPH_SETS["ascii"] or not COLOR:
            lines += [style("  " + b, ACCENT, BOLD) for b in (BANNER if G is not GLYPH_SETS["ascii"] else BANNER_ASCII)]
        else:
            lines += [style("  " + b, f"38;5;{shade}", BOLD) for b, shade in zip(BANNER, BANNER_SHADES)]
        lines += [style(f"  clean {G.sep} debloat {G.sep} check", MUTED), ""]
        spans = []
        for i, (key, name, about, _) in enumerate(items):
            icon = G.icons.get(key, "*")
            y = len(lines)
            spans += [(y, 0, cols, i), (y + 1, 0, cols, i)]
            number = style(f"{i + 1}", MUTED)
            if i == pos:
                lines.append(style(f"  {G.cursor} ", ACCENT, BOLD) + style(f"{icon}  {name}", ACCENT, BOLD)
                             + "  " + number)
                lines.append(style(f"       {about}", 97) if about else "")
            else:
                lines.append(f"    {style(icon, ACCENT)}  {name}  " + number)
                lines.append(style(f"       {about}", MUTED) if about else "")
        lines.append("")
        lines.append(style(f"  Click an item {G.sep} Up/Down + Enter {G.sep} its number {G.sep} Q quits", MUTED))
        term.draw(lines)
        k = term.key()
        if isinstance(k, Click):
            chosen = hit(spans, k)
            if chosen is None:
                continue
            pos, k = chosen, ENTER
        if isinstance(k, Wheel):
            pos = (pos + (1 if k.steps > 0 else -1)) % len(items)
        elif k in (UP, "k"):
            pos = (pos - 1) % len(items)
        elif k in (DOWN, "j"):
            pos = (pos + 1) % len(items)
        elif isinstance(k, str) and k.isdigit() and 1 <= int(k) <= len(items):
            pos, k = int(k) - 1, ENTER
        if k in ("q", ESC):
            return
        if k == ENTER:
            key, _, _, screen = items[pos]
            if key == "quit":
                return
            if key == "admin":
                if relaunch_as_admin():
                    return  # the elevated copy opens its own window; this one closes
                message(term, "Menu", ["Windows refused the elevation prompt."])
                continue
            screen(term)


def run() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("cleam: the menu needs an interactive terminal; see `cleam --help` for commands.", file=sys.stderr)
        return 2
    try:
        with Terminal() as term:
            main_menu(term)
    except KeyboardInterrupt:
        pass
    return 0
