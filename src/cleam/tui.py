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
  group title, a button in the bar at the bottom. **Hover** moves the
  highlight: mouse-move events (MOUSE_MOVED / xterm any-event mode 1003) are
  hit-tested the same way, and a frame identical to the last one is not
  written again, so a moving mouse costs nothing.
- **Nothing waits for a click.** The moment the menu opens, background
  threads read what each screen shows (debloat state, the system check, the
  junk scan); a screen opened later takes the finished result.
- **Debloat applies live.** Ticking a box makes the change, then reads the
  setting back from Windows, so the box shows what Windows actually kept.

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
import threading
import time
from dataclasses import dataclass, field

from . import __version__, debloat, junk, repair, security, snapshot
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
    "Taskbar & Explorer": "▬", "Gaming & comfort": "♦", "Performance": "►", "Security": "◘",
    "Diagnostics": "√", "Services": "±", "Apps": "♣",
    "System": "■", "Browsers": "○", "Developer tools": "±", "Recycle Bin": "×",
    "debloat": "▼", "clean": "░", "security": "◘", "undo": "◄", "admin": "↑", "quit": "×",
    "basic": "○", "advanced": "♦", "back": "◄",
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
class Hover:
    x: int
    y: int  # the mouse moved here, no button pressed


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
            # Report clicks, wheel and every mouse move (1003), in SGR coordinates.
            sys.stdout.write("\x1b[?1000h\x1b[?1003h\x1b[?1006h")
        sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
        sys.stdout.flush()
        return self

    def __exit__(self, *exc) -> None:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        if OS == "windows":
            self._k32.SetConsoleMode(self._in, self._in_mode)
        else:
            import termios

            sys.stdout.write("\x1b[?1000l\x1b[?1003l\x1b[?1006l")
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

    def _win_event(self, timeout: float | None = None):
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
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is not None:
                left = deadline - time.monotonic()
                # WAIT_OBJECT_0 (0) = input is waiting; anything else = timed out.
                if left <= 0 or self._k32.WaitForSingleObject(self._in, int(left * 1000)) != 0:
                    return None
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
                if m.flags == 1:  # MOUSE_MOVED
                    return Hover(m.x, m.y - top)
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

    def _posix_event(self, timeout: float | None = None):
        import select

        if timeout is not None and not select.select([sys.stdin], [], [], timeout)[0]:
            return None
        ch = sys.stdin.read(1)
        if ch != "\x1b":
            if ch == "\x03":
                raise KeyboardInterrupt
            return {"\n": ENTER, "\r": ENTER, " ": SPACE, "\t": TAB}.get(ch, ch.lower())
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
            if b & 32:  # motion, with or without a button held
                return Hover(x - 1, y - 1)
            return RESIZE  # a release or a drag: just redraw
        if rest in "56":
            sys.stdin.read(1)  # the ~ of PgUp/PgDn
        return {"A": UP, "B": DOWN, "5": PGUP, "6": PGDN, "H": HOME, "F": END}.get(rest, RESIZE)

    # ---- shared

    @staticmethod
    def size() -> tuple[int, int]:
        cols, rows = shutil.get_terminal_size((100, 30))
        return max(60, cols), max(20, rows)

    _last = ""

    def draw(self, lines: list[str]) -> None:
        cols, rows = self.size()
        out = ["\x1b[H"]
        for line in lines[:rows]:
            line = clip(line, cols)
            # Erase the rest only on a short line: after a full-width one the
            # cursor still sits on the last column (wrap pending), and ESC[K
            # there erases that column -- the panel's right border vanished.
            short = len(ANSI.sub("", line)) < cols
            out.append(line + ("\x1b[0m\x1b[K\n" if short else "\x1b[0m\n"))
        out.append("\x1b[J")
        frame = "".join(out).rstrip("\n")
        if frame == self._last:
            return  # mouse moves arrive by the dozen; an unchanged frame is not redrawn
        self._last = frame
        sys.stdout.write(frame)
        sys.stdout.flush()

    def key(self, timeout: float | None = None):
        """The next key, click, hover or wheel event; None if `timeout` seconds pass first."""
        k = self._win_event(timeout) if OS == "windows" else self._posix_event(timeout)
        if k == RESIZE:
            self._last = ""  # the terminal may have cleared itself: draw in full
        return k


# ------------------------------------------------------------------ building blocks


@dataclass
class Row:
    label: str
    checked: bool = False
    disabled: bool = False
    status: str = ""  # right-hand column: a size, "on", "needs admin"...
    detail: str = ""  # plain words, shown under the list for the highlighted row
    header: bool = False  # a group title, not selectable itself
    data: object = None
    default: bool = False  # ticked by 'recommended'
    # What the box showed when the list was read from the system. A list that
    # mirrors live state (Debloat) starts with was == checked, and only rows
    # whose box now differs are changes: ticked = turn on, unticked = turn off.
    was: bool = False
    tech: list = field(default_factory=list)  # exactly what changes: registry values, tasks, commands
    busy: bool = False  # being applied right now


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
    title = title[: max(0, inner - 4)]
    top = f" {G.tl}{G.h} {title} " + G.h * max(0, inner - len(title) - 2) + G.tr
    lines = [style(top, MUTED)]
    for text in body:
        lines.append(style(f" {G.v} ", MUTED) + pad(text, inner) + style(G.v, MUTED))
    lines.append(style(f" {G.bl}" + G.h * (inner + 1) + G.br, MUTED))
    return lines


def hit(spans: list, point):
    for y, x0, x1, action in spans:
        if point.y == y and x0 <= point.x < x1:
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


# ------------------------------------------------------------------ reading ahead


class Job:
    """One background read. A daemon thread, so quitting never waits for it."""

    def __init__(self, fn) -> None:
        self.result = None
        self.error: BaseException | None = None
        self.started = time.monotonic()
        self._done = threading.Event()
        threading.Thread(target=self._run, args=(fn,), daemon=True).start()

    def _run(self, fn) -> None:
        try:
            self.result = fn()
        except BaseException as e:  # shown on the screen that needed it
            self.error = e
        finally:
            self._done.set()

    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, seconds: float) -> bool:
        return self._done.wait(seconds)


class Background:
    """What every screen shows, read as soon as the menu opens.

    Opening a screen then takes the finished result instead of starting the
    work. A screen that changes the system asks for a fresh read when it closes.
    """

    def __init__(self, readers: dict) -> None:
        self.readers = readers
        self.jobs: dict[str, Job] = {}
        for name in readers:
            self.refresh(name)

    def refresh(self, name: str) -> None:
        if name in self.readers:
            self.jobs[name] = Job(self.readers[name])

    def ready(self, name: str) -> bool:
        return name in self.jobs and self.jobs[name].done()

    def pending(self) -> bool:
        return any(not j.done() for j in self.jobs.values())

    def get(self, term, name: str, title: str, what: str):
        """The result; if it is still being read, a progress line until it is."""
        job = self.jobs.get(name) or Job(self.readers[name])
        if not job.done():
            progress = Progress(term, title)
            while not job.wait(0.2):
                progress.step(f"{what}  ({time.monotonic() - job.started:.0f} s)")
        if job.error:
            self.refresh(name)  # the next visit tries again
            raise job.error
        return job.result


# ------------------------------------------------------------------ screens: generic


def menu(term, title: str, items: list[tuple], subtitle: list[str], status=None, footer: str = "") -> int | None:
    """A list of big items: hover highlights, click or Enter opens, a digit jumps.

    items: (icon key, name, about). status(i) -> (text, colour) for the right
    of an item's name, or None; while any is "reading", the frame refreshes on
    its own so it turns "ready" without a key press. Returns the index, or None.
    """
    pos = 0
    while True:
        cols, _ = Terminal.size()
        lines = [title_bar(title), ""] + subtitle
        spans = []
        live = False
        for i, (key, name, about) in enumerate(items):
            icon = G.icons.get(key, "*")
            y = len(lines)
            spans += [(y, 0, cols, i), (y + 1, 0, cols, i)]
            tag = ""
            if status:
                got = status(i)
                if got:
                    text, code = got
                    tag = "   " + style(text, code)
                    live = live or code == MUTED
            number = style(f"{i + 1}", MUTED)
            if i == pos:
                head = (style(f"  {G.cursor} ", ACCENT, BOLD) + style(f"{icon}  {name}", ACCENT, BOLD)
                        + "  " + number + tag)
                sub = style(f"       {about}", 97) if about else ""
                if COLOR:  # the whole item in the highlight band, both lines
                    band = f"\x1b[{CURRENT_BG}m"
                    head = band + head.replace("\x1b[0m", "\x1b[0m" + band) + " " * cols + "\x1b[0m"
                    sub = band + (sub or "").replace("\x1b[0m", "\x1b[0m" + band) + " " * cols + "\x1b[0m"
                lines += [head, sub]
            else:
                lines.append(f"    {style(icon, ACCENT)}  {name}  " + number + tag)
                lines.append(style(f"       {about}", MUTED) if about else "")
        lines.append("")
        lines.append(style(footer or f"  Point or click {G.sep} Up/Down + Enter {G.sep} its number {G.sep} Esc back",
                           MUTED))
        term.draw(lines)
        k = term.key(0.4) if live else term.key()
        if k is None:
            continue  # a background read may have finished: redraw
        if isinstance(k, Hover):
            over = hit(spans, k)
            if over is not None:
                pos = over
            continue
        if isinstance(k, Click):
            chosen = hit(spans, k)
            if chosen is None:
                continue
            return chosen
        if isinstance(k, Wheel):
            pos = (pos + (1 if k.steps > 0 else -1)) % len(items)
        elif k in (UP, "k"):
            pos = (pos - 1) % len(items)
        elif k in (DOWN, "j"):
            pos = (pos + 1) % len(items)
        elif isinstance(k, str) and k.isdigit() and 1 <= int(k) <= len(items):
            return int(k) - 1
        elif k == ENTER:
            return pos
        elif k in ("q", ESC):
            return None


def checklist(term, title: str, intro: str, rows: list[Row], action: str = "Continue",
              reset_label: str = "None", live=None) -> list[Row] | None:
    """Tick things with Space or a click.

    Plain mode returns the rows whose box changed (None on Back); for a list
    where every row starts unticked (was=False) that is simply "ticked".

    Live mode (`live` given, see DebloatLive) has no second step: a tick is
    handed to live.change() at once, which makes the change and sets each
    row to what the system has afterwards. It returns None.

    The panel under the list shows the highlighted row's technical detail
    (registry values, tasks, commands); ? switches it to plain words.
    """
    selectable = [i for i, r in enumerate(rows) if not r.header]
    if not selectable:
        message(term, title, [intro, "", "Nothing to show here."])
        return None
    pos, top = 0, 0
    plain = not any(r.tech for r in rows)  # nothing technical to show: plain words only

    def group_members(header_index: int) -> list[Row]:
        out = []
        for r in rows[header_index + 1:]:
            if r.header:
                break
            if not r.disabled:
                out.append(r)
        return out

    def change(changed: list[Row]) -> None:
        """Live mode: show them as busy, then apply."""
        changed = [r for r in changed if r.checked != r.was]
        if not changed:
            return
        for r in changed:
            r.busy = True
        draw()
        try:
            live.change(changed)
        finally:
            for r in changed:
                r.busy = False

    def toggle(rs: list[Row], on: bool | None = None) -> None:
        for r in rs:
            r.checked = (not r.checked) if on is None else on
        if live:
            change(rs)

    frame: dict = {}

    def draw() -> None:
        nonlocal top
        cols, height = Terminal.size()
        cur = selectable[pos]
        row = rows[cur]
        if plain or not row.tech:
            body = textwrap.wrap(row.detail, cols - 8)[:4] if row.detail else []
            what = "plain words"
        else:
            body = [clip(t, cols - 8) for t in row.tech[:3]]
            if len(row.tech) > 3:
                body.append(f"+ {len(row.tech) - 3} more")
            what = "technical"
        note = live.note() if live else ""
        head = 2 + (1 if intro else 0)
        panel_h = len(body) + 2 if body else 0
        list_height = max(5, height - head - 2 - panel_h - (1 if note else 0))
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
        if body:
            label = row.label if len(row.label) < cols - 40 else "About"
            switch = "" if plain or not row.tech else f"   [? {'technical' if what == 'plain words' else 'plain words'}]"
            lines += panel(f"{label}{switch}", body, cols)
        if note:
            lines.append("  " + note)
        if live:
            buttons = live.buttons()
            hint = f"point {G.sep} click or Space: switch now {G.sep} ? plain words"
        else:
            ticked = sum(1 for r in rows if r.checked != r.was and not r.disabled and not r.header)
            buttons = [(f"{action} ({ticked})", "go"), ("Recommended", "a"), (reset_label, "n"), ("Back", "back")]
            hint = f"click a row {G.sep} ↑↓ move {G.sep} Space tick {G.sep} Enter {action.lower()}"
        if any(r.tech for r in rows):
            buttons = buttons + [("?", "?")]
        bar, bar_spans = button_bar(len(lines), buttons, hint)
        lines.append(bar)
        frame["spans"] = spans + bar_spans
        frame["list_height"] = list_height
        term.draw(lines)

    while True:
        draw()
        cur = selectable[pos]
        k = term.key()
        if isinstance(k, Hover):
            over = hit(frame["spans"], k)
            if isinstance(over, tuple) and not rows[over[1]].header:
                pos = selectable.index(over[1])
            continue
        if isinstance(k, Click):
            what = hit(frame["spans"], k)
            if what is None:
                continue
            if isinstance(what, tuple):
                i = what[1]
                if rows[i].header:
                    members = group_members(i)
                    toggle(members, on=not all(r.checked for r in members))
                    continue
                pos = selectable.index(i)
                if not rows[i].disabled:
                    toggle([rows[i]])
                continue
            if live and what not in ("?", "back"):
                live.press(what, rows)
                continue
            k = {"go": ENTER, "back": ESC}.get(what, what)
        if isinstance(k, Wheel):
            pos = max(0, min(len(selectable) - 1, pos + k.steps))
        elif k in (UP, "k"):
            pos = max(0, pos - 1)
        elif k in (DOWN, "j"):
            pos = min(len(selectable) - 1, pos + 1)
        elif k == PGUP:
            pos = max(0, pos - frame["list_height"])
        elif k == PGDN:
            pos = min(len(selectable) - 1, pos + frame["list_height"])
        elif k == HOME:
            pos = 0
        elif k == END:
            pos = len(selectable) - 1
        elif k == "?" and not all(not r.tech for r in rows):
            plain = not plain
        elif k in (SPACE, "x") and not rows[cur].disabled:
            toggle([rows[cur]])
        elif k == ENTER and live:
            if not rows[cur].disabled:
                toggle([rows[cur]])
        elif k == "a":
            if live:
                live.press("a", rows)
            else:
                for r in rows:
                    if not r.disabled and not r.header:
                        r.checked = r.default or r.was  # recommended on top; nothing already on is dropped
        elif k == "n" and not live:
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
    icon = G.icons.get(r.label, G.icons.get(r.label.split(" (")[0].split(":")[0], "*"))
    left = f" {icon} {r.label.upper()} "
    right = f" {ticked} of {count} on" + (f" {G.sep} click to switch all " if members else " ")
    fill = G.h * max(1, cols - len(left) - len(right))
    return style(left, ACCENT, BOLD) + style(fill, MUTED) + style(right, MUTED)


def row_line(r: Row, current: bool, cols: int, status_w: int) -> str:
    """  ▌ ■ Title .................................. √ ON"""
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
    if r.busy:
        status, status_code = f"{arrow} working...", ACCENT
    elif not r.disabled and r.checked != r.was and status in ("on", "off", "partly"):
        # A pending change, shown next to what the system has now.
        status, status_code = f"{arrow} turn {'on' if r.checked else 'off'}", ACCENT
    elif status == "on":
        status, status_code = f"{G.done} ON", OK_C
    elif status == "off":
        status = f"{off_mark} off"
    elif status == "partly":
        status, status_code = "± PARTLY", WARN_C
    elif status == "removed":
        status, status_code = f"{G.done} removed", OK_C
    elif status in ("already set",):
        status, status_code = f"{G.done} already set", OK_C
    elif status.startswith(("failed", "not kept")):
        status, status_code = f"{G.fail} {status}", BAD_C
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
        if isinstance(k, Hover) or k is None:
            continue
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


def read_debloat() -> dict:
    """Everything the Debloat screens show, in one background read (~2 s)."""
    engine = debloat.Debloater()
    return {"engine": engine, "states": engine.states(), "apps": engine.removable_apps()}


def _state_word(state: str) -> str:
    return {"applied": "on", "partly": "partly"}.get(state, "off")


def debloat_rows(data: dict, level: str) -> list[Row]:
    engine, states = data["engine"], data["states"]
    env = engine.env
    rows: list[Row] = []
    for group in debloat.GROUPS:
        members = [t for t in debloat.TWEAKS if t.group == group and debloat.tier(t) == level]
        if not members:
            continue
        rows.append(Row(group, header=True))
        for t in members:
            state = states[t.id]
            # "n/a": nothing it touches exists here (a service this Windows
            # lacks), so there is nothing to switch.
            why = debloat.availability(t, env) or ("not on this PC" if state == "n/a" else "")
            on = state == "applied"
            extra = f" Takes effect after: {t.restart}." if t.restart else ""
            risk = " [moderate: read before ticking]" if t.risk == debloat.MODERATE else ""
            rows.append(Row(t.title, checked=on, was=on, disabled=bool(why), status=why or _state_word(state),
                            detail=t.about + extra + risk, data=t, default=t.default and not why,
                            tech=debloat.tech(t)))
    apps = [(a, p) for a, p in data["apps"] if debloat.tier(a) == level]
    if apps:
        # Undo re-registers the app; Windows deletes an app's own data on removal.
        rows.append(Row("Apps: tick to remove", header=True))
        for app, pkg in apps:
            rows.append(Row(app.name, status="installed", data=app, default=app.default,
                            detail=f"{app.about}. Undo reinstalls the app, not its data.",
                            tech=[f"Remove-AppxPackage {pkg.get('PackageFullName', app.id)}   (this user only)",
                                  f"undo: Add-AppxPackage -RegisterByFamilyName {pkg.get('PackageFamilyName', '')}"]))
    return rows


class DebloatLive:
    """The Debloat list's live mode: a tick is the change.

    Each change is journalled and made, then the setting is read back from
    Windows and the row shows that -- so a value another program or a policy
    immediately puts back shows as "not kept" instead of a tick that lies.
    """

    def __init__(self, term, engine, states: dict | None = None) -> None:
        self.term, self.engine = term, engine
        self.states = states if states is not None else {}  # updated in place: no re-read on the way out
        self.restarts: set[str] = set()
        self.last = ""
        self.changed = False
        self.apps_changed = False

    def change(self, rows: list[Row]) -> None:
        apps_out = [r for r in rows if isinstance(r.data, debloat.App) and r.checked and not r.was]
        if apps_out and not confirm(self.term, "Remove apps", [f"  - {r.label}" for r in apps_out] + [
                "", "Windows deletes an app's own data (notes, recordings, settings) with it.",
                "Undo reinstalls the app, not the data."], "Remove them?"):
            for r in apps_out:
                r.checked = r.was
            rows = [r for r in rows if r not in apps_out]
        ok = failed = 0
        for r in rows:
            item = r.data
            if isinstance(item, debloat.Tweak):
                want_on = r.checked
                out = self.engine.apply(item) if want_on else self.engine.revert(item)
                state = self.engine.state(item)  # what Windows has now, not what was asked
                self.states[item.id] = state
                r.checked = r.was = state == "applied"
                r.status = _state_word(state)
                if not out.ok:
                    r.status = "failed"
                    self.last = style(f"{G.fail} {item.title}: {out.message}", BAD_C)
                elif r.checked != want_on and state != "partly":
                    r.status = "not kept"
                    self.last = style(f"{G.warn} {item.title}: Windows still reports it "
                                      f"{'off' if want_on else 'on'} -- a policy or another tool sets it back", WARN_C)
                    out.ok = False
                if out.ok and out.restart:
                    self.restarts.add(out.restart)
                failed += not out.ok
                ok += out.ok
            elif isinstance(item, debloat.App):
                if r.checked:
                    out = self.engine.remove_app(item)
                    r.was = r.checked = out.ok
                    r.status = "removed" if out.ok else "failed"
                else:
                    out = self.engine.restore_app(item.id)
                    r.was = r.checked = not out.ok
                    r.status = "installed" if out.ok else "failed"
                if not out.ok:
                    self.last = style(f"{G.fail} {item.name}: {out.message}", BAD_C)
                self.apps_changed = self.apps_changed or out.ok
                failed += not out.ok
                ok += out.ok
        self.changed = self.changed or ok > 0
        if failed == 0 and ok:
            names = ", ".join(r.label for r in rows[:2]) + (f" +{len(rows) - 2}" if len(rows) > 2 else "")
            self.last = style(f"{G.ok} done: {names}", OK_C)

    def note(self) -> str:
        after = [w for w in ("restart", "sign out") if w in self.restarts]
        tail = f"   Some changes show after you {' / '.join(after)}." if after else ""
        return (self.last or style("Every tick is applied at once and can be undone.", MUTED)) + style(tail, WARN_C)

    def buttons(self) -> list[tuple[str, object]]:
        out = [("Back", "back"), ("Recommended", "a")]
        if "explorer" in self.restarts:
            out.append(("Restart Explorer", "explorer"))
        if OS == "windows" and self.engine.env.admin:
            out.append(("Restore point", "point"))
        return out

    def press(self, action, rows: list[Row]) -> None:
        if action == "a":
            todo = [r for r in rows if r.default and not r.disabled and not r.header and not r.checked
                    and not isinstance(r.data, debloat.App)]
            for r in todo:
                r.checked = True
                r.busy = True
            if todo:
                progress = Progress(self.term, "Debloat: recommended", len(todo))
                for r in todo:
                    progress.step(r.label)
                    self.change([r])
                    r.busy = False
                    progress.add(mark(r.status == "on") + r.label)
        elif action == "explorer":
            debloat.restart_explorer()
            self.restarts.discard("explorer")
            self.last = style(f"{G.ok} Explorer restarted", OK_C)
        elif action == "point":
            progress = Progress(self.term, "Restore point")
            progress.step("Creating a restore point (a minute or two)")
            code, text = snapshot.create("Cleam: debloat", capture=True)
            self.last = (style(f"{G.ok} restore point created", OK_C) if code == 0
                         else style(f"{G.fail} restore point: {text.strip()[:80]}", BAD_C))


def debloat_screen(term, bg: Background) -> None:
    if OS != "windows":
        message(term, "Debloat", ["Debloat is for Windows 10 and 11 so far.",
                                  "Linux and macOS support is planned."])
        return
    while True:
        data = bg.get(term, "debloat", "Debloat", "Reading what is set on this PC")
        env = data["engine"].env

        def count(level: str) -> str:
            ts = [t for t in debloat.TWEAKS if debloat.tier(t) == level and not debloat.availability(t, env)
                  and data["states"][t.id] != "n/a"]
            return f"{sum(data['states'][t.id] == 'applied' for t in ts)} of {len(ts)} on"

        items = [("basic", "Basic", "Privacy, ads, suggestions, AI features, taskbar clutter, bloat apps"),
                 ("advanced", "Advanced", "Services, power, gaming, performance, security hardening, "
                                          "developer telemetry, apps that keep data"),
                 ("back", "Back", "")]
        edition = f"Windows {env.windows} {debloat.edition_name(env.edition)} (build {env.build})"
        admin = "" if env.admin else f"  {G.sep}  not administrator: machine-wide items are greyed out"
        choice = menu(term, "Debloat", items, [style(f"  {edition}{admin}", MUTED), ""],
                      status=lambda i: (count(("basic", "advanced")[i]), OK_C) if i < 2 else None)
        if choice is None or choice == 2:
            return
        level = ("basic", "advanced")[choice]
        live = DebloatLive(term, data["engine"], data["states"])
        rows = debloat_rows(data, level)
        on_now = sum(1 for r in rows if r.was)
        intro = f"{edition}. {on_now} on. Tick = turn on, untick = turn off, applied at once."
        checklist(term, f"Debloat {G.sep} {level.capitalize()}", intro, rows, live=live)
        if live.apps_changed:
            bg.refresh("debloat")  # the list of removable apps changed
        if live.changed:
            bg.refresh("check")  # SMB1, PowerShell 2.0 and others show in the system check
        if live.restarts & {"explorer"} and confirm(term, "Debloat", [
                "Taskbar and File Explorer changes need Explorer to restart (the taskbar blinks; open windows stay)."],
                "Restart Explorer now?"):
            debloat.restart_explorer()


class UndoLive:
    """Undo as it is ticked: the change is put back at once and the row greys out."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.last = ""
        self.changed = False
        self.restarts: set[str] = set()

    def change(self, rows: list[Row]) -> None:
        for r in rows:
            if not r.checked:  # an undone row is disabled; nothing turns "un-undo"
                r.checked = r.was
                continue
            kind, ident = r.data
            out = self.engine.undo(ident) if kind == "tweak" else self.engine.restore_app(ident)
            if out.ok:
                r.was, r.disabled, r.status = True, True, "undone"
                self.changed = True
                if out.restart:
                    self.restarts.add(out.restart)
                self.last = style(f"{G.ok} put back: {r.label}", OK_C)
            else:
                r.checked, r.status = False, "failed"
                self.last = style(f"{G.fail} {r.label}: {out.message}", BAD_C)

    def note(self) -> str:
        return self.last or style("Tick an item to put it back now.", MUTED)

    def buttons(self) -> list[tuple[str, object]]:
        return [("Back", "back")] + ([("Restart Explorer", "explorer")] if "explorer" in self.restarts else [])

    def press(self, action, rows: list[Row]) -> None:
        if action == "explorer":
            debloat.restart_explorer()
            self.restarts.discard("explorer")
            self.last = style(f"{G.ok} Explorer restarted", OK_C)


def undo_screen(term, bg: Background) -> None:
    engine = debloat.Debloater()
    journal = engine.applied()
    by_id = {t.id: t for t in debloat.TWEAKS}
    rows: list[Row] = []
    if journal["tweaks"]:
        rows.append(Row("Settings Cleam changed", header=True))
        rows += [Row(by_id[i].title if i in by_id else i, status=e["applied"][:10],
                     detail="Undo puts back exactly what was there before.",
                     tech=[f"{r['hive']}\\{r['key']}  {r['name'] or '(Default)'}: back to "
                           + (repr(r["value"]) if r["existed"] else "absent") for r in e["registry"]]
                     + [f"task {t['path']}: {'Enabled' if t['enabled'] else 'Disabled'}" for t in e["tasks"]],
                     data=("tweak", i)) for i, e in journal["tweaks"].items()]
    if journal["apps"]:
        rows.append(Row("Apps Cleam removed", header=True))
        rows += [Row(e["name"], status=e["removed"][:10], detail="Registers the app again from the copy Windows kept.",
                     tech=[f"Add-AppxPackage -RegisterByFamilyName {e['family']}"],
                     data=("app", i)) for i, e in journal["apps"].items()]
    live = UndoLive(engine)
    checklist(term, "Undo debloat", "Tick what to put back; it happens at once.", rows, live=live)
    if live.changed:
        bg.refresh("debloat")
        bg.refresh("check")


def read_clean() -> list:
    """Measure every junk target that exists here (the scan, not the delete)."""
    present = [t for t in junk.targets() if junk.present(t)]  # once: targets() runs whoami
    return [(t, junk.run(t)) for t in present]


def clean_screen(term, bg: Background) -> None:
    scanned = bg.get(term, "clean", "Clean junk", "Measuring caches, temp files and logs")
    rows: list[Row] = []
    for group in ("System", "Browsers", "Apps", "Developer tools", "Recycle Bin"):
        members = [(t, r) for t, r in scanned if t.group == group]
        if not members:
            continue
        rows.append(Row(group, header=True))
        for t, r in members:
            empty = bool(r.skipped) or not r.files
            size = r.skipped or (human(r.bytes) if r.files else "clean")
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
    bg.refresh("clean")
    message(term, "Clean junk", [style(f"Freed {human(freed)} in total.", BOLD, GREEN), ""] + progress.lines)


# ---- System check: every topic is a dropdown, alerts stand out

SEVERITY = {security.BAD: 4, security.WARN: 3, security.UNKNOWN: 2, security.INFO: 1, security.OK: 0}


def badge(state: str) -> str:
    """A fixed-width state mark; alerts are filled blocks of colour, not just coloured words."""
    if state == security.BAD:
        return style(" ALERT ", 41, 97, BOLD) if COLOR else "[ALERT]"
    if state == security.WARN:
        return style(" CHECK ", 43, 30) if COLOR else "[CHECK]"
    if state == security.OK:
        return style(f"  {G.ok} ok ", OK_C) if COLOR else "[ ok  ]"
    if state == security.INFO:
        return style("  info ", MUTED) if COLOR else "[info ]"
    return style("   ??  ", WARN_C) if COLOR else "[ ??  ]"


@dataclass
class Topic:
    title: str
    items: list  # (state, label, detail, fix)
    open: bool = False

    @property
    def worst(self) -> str:
        return max((i[0] for i in self.items), key=lambda s: SEVERITY.get(s, 0), default=security.OK)


def read_check() -> tuple:
    return security.status(), security.startup(), security.ransom_signs()


def check_topics(checks, items, signs) -> list[Topic]:
    """Topics of (state, label, detail, advice, fixes) items; alerts first inside each."""
    topics: list[Topic] = []
    order = lambda m: -SEVERITY.get(m[0], 0)  # noqa: E731
    for group in list(security.CHECK_GROUPS) + sorted({c.group for c in checks} - set(security.CHECK_GROUPS)):
        members = [(c.state, c.label, c.detail, c.fix if c.state != security.OK else "", repair.fixes_for(c))
                   for c in checks if c.group == group]
        if members:
            topics.append(Topic(group, sorted(members, key=order)))
    found = [(signs.state, f, "", "Disconnect from the network, do not pay, and restore from a backup made "
              "before the date these files changed.", []) for f in signs.findings()]
    topics.append(Topic(f"Ransomware ({signs.files_checked:,} files checked)",
                        found or [(security.OK, "No signs found", "No ransom notes, known extensions or mass renames.",
                                   "", [])]))
    startup = []
    journal = debloat.load_journal()
    kinds = {"task": "task", "service": "service", "winlogon": "Winlogon", "ifeo": "IFEO", "appinit": "AppInit",
             "wmi": "WMI"}
    for i in items:
        detail = ((f"{kinds[i.kind]} {G.sep} " if i.kind in kinds else "") + (i.publisher or i.path or i.command)
                  + ("" if i.enabled else "  (turned off)"))
        # Suspicious but switched off (e.g. by the fix below): it cannot start, so it is information now.
        state = (security.WARN if i.enabled else security.INFO) if i.suspicious else security.OK
        startup.append((state, i.name, detail, "; ".join(i.reasons), repair.startup_fixes(i, journal)))
    if startup:
        topics.append(Topic("Starts with the computer", sorted(startup, key=order)))
    for t in topics:
        t.open = t.worst == security.BAD  # emergencies are open from the start
    return topics


def run_fix(term, bg: Background, fix, ask: bool = True) -> str:
    """Confirm (showing exactly what changes), run, and return a one-line result."""
    if fix.kind == repair.SCREEN:
        if fix.target == "clean":
            clean_screen(term, bg)
        return ""
    if ask:
        text = ([fix.about, ""] if fix.about else []) + ["What happens:"] + [f"  {t}" for t in fix.tech]
        if fix.undo:
            text += ["", f"To reverse it: {fix.undo}"]
        elif fix.kind == repair.CHANGE:
            text += ["", "Recorded first: Undo debloat puts the previous values back."]
        if fix.restart:
            text += ["", "Takes effect after a restart."]
        if fix.warn:
            text += ["", style(f"{G.warn} {fix.warn}", WARN_C)]
        if not confirm(term, fix.title, text, f"{fix.title}?"):
            return ""
    progress = Progress(term, fix.title)
    progress.step(f"{fix.title}...")
    ok, said = repair.run(fix)
    return style(f"{G.ok} {fix.title}: {said}", OK_C) if ok else style(f"{G.fail} {fix.title}: {said}", BAD_C)


def security_screen(term, bg: Background) -> None:
    checks, items, signs = bg.get(term, "check", "System check", "Checking protection, updates, hardware and health")
    topics = check_topics(checks, items, signs)
    pos, top = 0, 0
    note = ""
    while True:
        cols, height = Terminal.size()
        # Visible lines: a header per topic and, when open, its items. An
        # alert or check is written out in full with its advice, and every
        # fix it has is a line of its own that runs when clicked.
        entries: list[tuple] = []  # (kind, topic, payload)
        width = cols - 16
        for t in topics:
            entries.append(("topic", t, None))
            if not t.open:
                continue
            for it in t.items:
                entries.append(("item", t, it))
                if it[0] != security.OK:
                    entries += [("text", t, w) for w in textwrap.wrap(it[2], width)]
                    if not it[4]:
                        entries += [("advice", t, w) for w in textwrap.wrap(it[3], width)]
                for fix in it[4]:
                    entries.append(("fix", t, fix))
        pos = max(0, min(pos, len(entries) - 1))
        fixable = [f for t in topics for it in t.items if it[0] == security.BAD for f in it[4]
                   if f.kind in (repair.CHANGE, repair.RUN)]
        bad = sum(1 for t in topics for it in t.items if it[0] == security.BAD)
        warn = sum(1 for t in topics for it in t.items if it[0] == security.WARN)
        total = sum(len(t.items) for t in topics)
        if bad:
            summary = style(f" {G.warn} {bad} ALERT{'S' if bad > 1 else ''} NEED ATTENTION ", 41, 97, BOLD) + \
                style(f"   {warn} to check {G.sep} {total} checks", MUTED)
        elif warn:
            summary = style(f" {warn} to check ", 43, 30) + style(f"   no alerts {G.sep} {total} checks", MUTED)
        else:
            summary = style(f" {G.ok} all clear ", 42, 30) + style(f"   {total} checks", MUTED)
        lines = [title_bar("System check"), "", "  " + summary, "  " + (note or style(
            "Point at a fix and click it (or Enter): it says exactly what it will change first.", MUTED))]
        view = height - len(lines) - 2
        if pos < top:
            top = pos
        if pos >= top + view:
            top = pos - view + 1
        spans = []
        for n in range(top, min(len(entries), top + view)):
            kind, t, it = entries[n]
            y = len(lines)
            spans.append((y, 0, cols, n))
            if kind == "topic":
                arrow = ("v" if t.open else ">") if G is GLYPH_SETS["ascii"] else ("▼" if t.open else "►")
                counts = []
                for state, word in ((security.BAD, "alert"), (security.WARN, "check"), (security.UNKNOWN, "unknown"),
                                    (security.INFO, "info"), (security.OK, "ok")):
                    k = sum(1 for i in t.items if i[0] == state)
                    if k:
                        counts.append(f"{k} {word}")
                colour = BAD_C if t.worst == security.BAD else WARN_C if t.worst == security.WARN else ACCENT
                # ▼ ALERT  PROTECTION ────────────── 1 alert · 1 check · 3 ok
                left = f" {t.title.upper()} "
                right = " " + f" {G.sep} ".join(counts) + " "
                fill = G.h * max(1, cols - len(left) - len(right) - 11)
                line = (style(f" {arrow} ", colour, BOLD) + badge(t.worst) + style(left, colour, BOLD)
                        + style(fill, MUTED) + style(right, MUTED))
            elif kind == "item":
                state, label, detail = it[0], it[1], it[2]
                alarm = state in (security.BAD, security.WARN, security.UNKNOWN)
                text = label if state != security.OK or not detail else f"{label}: {detail}"
                emph = (BAD_C, BOLD) if state == security.BAD else (WARN_C, BOLD) if alarm else ()
                line = "      " + badge(state) + "  " + (style(text, *emph) if emph else text)
            elif kind == "text":
                line = "               " + it
            elif kind == "advice":
                line = "               " + style(it, MUTED)
            else:  # a fix: a button of its own
                where = {repair.OPEN: "opens a page", repair.UEFI: "restarts into UEFI", repair.REBOOT: "restarts",
                         repair.SCREEN: "opens Clean junk", repair.CHANGE: "undoable",
                         repair.REFRESH: "display"}.get(it.kind, "")
                button = style(f" {G.cursor if G is not GLYPH_SETS['ascii'] else '>'} Fix: {it.title} ",
                               *ON_ACCENT) if COLOR else f"[ Fix: {it.title} ]"
                line = "               " + button + style(f"  {where}", MUTED)
            if n == pos and COLOR:
                band = f"\x1b[{CURRENT_BG}m"
                line = band + line.replace("\x1b[0m", "\x1b[0m" + band) + " " * cols + "\x1b[0m"
            lines.append(line)
        while len(lines) < 4 + view:
            lines.append("")
        buttons = [("Back", "back")] + ([(f"Fix all alerts ({len(fixable)})", "fixall")] if fixable else []) + [
            ("Open all", "open"), ("Close all", "close"), ("Check again", "again")]
        bar, bar_spans = button_bar(len(lines), buttons, f"point {G.sep} click a topic to open it, a fix to run it")
        lines.append(bar)
        term.draw(lines)
        k = term.key()
        if isinstance(k, Hover):
            over = hit(spans, k)
            if over is not None:
                pos = over
            continue
        if isinstance(k, Click):
            what = hit(spans + bar_spans, k)
            if what is None:
                continue
            if isinstance(what, int):
                pos = what
                k = ENTER
            else:
                k = what
        rerun = False
        if isinstance(k, Wheel):
            pos = max(0, min(len(entries) - 1, pos + k.steps))
        elif k in (UP, "k"):
            pos = max(0, pos - 1)
        elif k in (DOWN, "j"):
            pos = min(len(entries) - 1, pos + 1)
        elif k == PGUP:
            pos = max(0, pos - view)
        elif k == PGDN:
            pos = min(len(entries) - 1, pos + view)
        elif k in (ENTER, SPACE):
            kind, t, it = entries[pos]
            if kind == "fix":
                note = run_fix(term, bg, it)
                rerun = bool(note) and it.kind in (repair.CHANGE, repair.RUN, repair.REFRESH)
            else:
                t.open = not t.open
                pos = next(i for i, e in enumerate(entries) if e[1] is t and e[0] == "topic")
        elif k == "fixall" and fixable:
            text = [f"  - {f.title}" for f in fixable] + ["", "Each change is recorded first where it can be."]
            text += [style(f"{G.warn} {f.warn}", WARN_C) for f in fixable if f.warn]
            if confirm(term, "Fix all alerts", text, f"Run these {len(fixable)} fixes?"):
                results = [run_fix(term, bg, f, ask=False) for f in fixable]
                note = "   ".join(results)
                rerun = True
        elif k == "open":
            for t in topics:
                t.open = True
        elif k == "close":
            for t in topics:
                t.open = False
        elif k == "again":
            rerun = True
        elif k in (ESC, "q", "back"):
            return
        if rerun:  # read the system again, and keep open what was open
            opened = {t.title.split(" (")[0] for t in topics if t.open}
            bg.refresh("check")
            bg.refresh("debloat")  # several fixes are debloat settings too
            checks, items, signs = bg.get(term, "check", "System check", "Checking again")
            topics = check_topics(checks, items, signs)
            for t in topics:
                t.open = t.open or t.title.split(" (")[0] in opened


def main_menu(term) -> None:
    global STATUS
    readers = {"check": read_check, "clean": read_clean}
    if OS == "windows":
        readers["debloat"] = read_debloat
    bg = Background(readers)
    items = [("debloat", "Debloat", "Privacy, ads, AI features, clutter, apps, services, gaming, security",
              debloat_screen, "debloat"),
             ("clean", "Clean junk", "Caches, temp files, crash dumps, old logs", clean_screen, "clean"),
             ("security", "System check", "Protection, updates, hardware, health, gaming, startup", security_screen,
              "check"),
             ("undo", "Undo debloat", "Put back anything Cleam changed", undo_screen, None)]
    if OS == "windows" and not is_admin():
        items.append(("admin", "Restart as administrator", "For machine-wide settings and system junk", None, None))
    items.append(("quit", "Quit", "", None, None))
    env = debloat.current_env() if OS == "windows" else None
    if env:
        STATUS = (f"Windows {env.windows} {debloat.edition_name(env.edition)} {G.sep} build {env.build} {G.sep} "
                  + (f"admin {G.ok}" if env.admin else "not admin"))
    else:
        STATUS = sys.platform

    def status(i: int):
        job = items[i][4]
        if not job or job not in bg.jobs:
            return None
        return (f"{G.ok} ready", OK_C) if bg.ready(job) else ("reading...", MUTED)

    if G is GLYPH_SETS["ascii"] or not COLOR:
        banner = [style("  " + b, ACCENT, BOLD) for b in (BANNER if G is not GLYPH_SETS["ascii"] else BANNER_ASCII)]
    else:
        banner = [style("  " + b, f"38;5;{shade}", BOLD) for b, shade in zip(BANNER, BANNER_SHADES)]
    subtitle = banner + [style(f"  clean {G.sep} debloat {G.sep} check", MUTED), ""]
    while True:
        chosen = menu(term, "Menu", [(k, n, a) for k, n, a, _, _ in items], subtitle, status,
                      f"  Point or click an item {G.sep} Up/Down + Enter {G.sep} its number {G.sep} Q quits")
        if chosen is None:
            return
        key, _, _, screen, _ = items[chosen]
        if key == "quit":
            return
        if key == "admin":
            if relaunch_as_admin():
                return  # the elevated copy opens its own window; this one closes
            message(term, "Menu", ["Windows refused the elevation prompt."])
            continue
        try:
            screen(term, bg)
        except Exception as e:  # a failed read must not take the whole menu down
            message(term, "Something went wrong", [f"{type(e).__name__}: {e}"])


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
