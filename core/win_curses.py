"""
Minimal curses-like compatibility layer for native Windows consoles.

This is intentionally tiny and only implements the subset used by TUI.py.
It relies on ANSI escape sequences plus msvcrt-based keyboard polling.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
from dataclasses import dataclass

try:
    import ctypes
    import msvcrt
except ImportError as exc:  # pragma: no cover - Windows-only runtime path
    raise ModuleNotFoundError("_curses replacement only available on Windows") from exc


class error(Exception):
    pass


A_NORMAL = 0
A_BOLD = 1 << 0
A_DIM = 1 << 1
_COLOR_SHIFT = 8

COLOR_BLACK = 0
COLOR_RED = 1
COLOR_GREEN = 2
COLOR_YELLOW = 3
COLOR_BLUE = 4
COLOR_MAGENTA = 5
COLOR_CYAN = 6
COLOR_WHITE = 7

KEY_UP = 1001
KEY_DOWN = 1002
KEY_LEFT = 1003
KEY_RIGHT = 1004
KEY_PPAGE = 1005
KEY_NPAGE = 1006
KEY_END = 1007
KEY_BACKSPACE = 127
KEY_DC = 1008
KEY_RESIZE = 1999

_COLOR_PAIRS: dict[int, tuple[int, int]] = {}
_ACTIVE_ROOT: "_Window | None" = None
_TERMINAL_READY = False


def _enable_vt_mode() -> None:
    global _TERMINAL_READY
    if _TERMINAL_READY:
        return
    os.system("")
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass
    _TERMINAL_READY = True


def start_color() -> None:
    _enable_vt_mode()


def use_default_colors() -> None:
    _enable_vt_mode()


def curs_set(_visibility: int) -> None:
    _enable_vt_mode()


def init_pair(pair_number: int, fg: int, bg: int) -> None:
    _COLOR_PAIRS[pair_number] = (fg, bg)


def color_pair(pair_number: int) -> int:
    return pair_number << _COLOR_SHIFT


def doupdate() -> None:
    if _ACTIVE_ROOT is not None:
        _ACTIVE_ROOT._flush()


def wrapper(func):
    _enable_vt_mode()
    root = _Window(height=0, width=0, top=0, left=0, root=None)
    root.nodelay(True)
    global _ACTIVE_ROOT
    _ACTIVE_ROOT = root
    _enter_alt_screen()
    try:
        return func(root)
    finally:
        _leave_alt_screen()
        _ACTIVE_ROOT = None


def _enter_alt_screen() -> None:
    sys.stdout.write("\x1b[?1049h\x1b[H\x1b[?25h")
    sys.stdout.flush()


def _leave_alt_screen() -> None:
    sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
    sys.stdout.flush()


atexit.register(_leave_alt_screen)


def _terminal_size() -> tuple[int, int]:
    size = shutil.get_terminal_size((80, 24))
    return size.lines, size.columns


def _attr_to_ansi(attr: int) -> str:
    pair_id = attr >> _COLOR_SHIFT
    fg, bg = _COLOR_PAIRS.get(pair_id, (-1, -1))
    codes: list[str] = []

    if attr & A_BOLD:
        codes.append("1")
    elif attr & A_DIM:
        codes.append("2")
    else:
        codes.append("22")

    if fg < 0:
        codes.append("39")
    else:
        codes.append(str(30 + fg))

    if bg < 0:
        codes.append("49")
    else:
        codes.append(str(40 + bg))

    return "\x1b[" + ";".join(codes) + "m"


def _translate_key() -> int:
    if not msvcrt.kbhit():
        return -1

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        nxt = msvcrt.getwch()
        mapping = {
            "H": KEY_UP,
            "P": KEY_DOWN,
            "K": KEY_LEFT,
            "M": KEY_RIGHT,
            "I": KEY_PPAGE,
            "Q": KEY_NPAGE,
            "O": KEY_END,
            "S": KEY_DC,
        }
        return mapping.get(nxt, -1)

    if ch == "\r":
        return 10
    if ch == "\x08":
        return KEY_BACKSPACE
    if ch == "\x1b":
        return 27
    if ch == "\t":
        return 9
    if ch == "\x03":
        return 3
    if ch == "\x04":
        return 4

    if len(ch) == 1:
        return ord(ch)
    return -1


@dataclass
class _Cell:
    ch: str = " "
    attr: int = A_NORMAL


class _Window:
    def __init__(self, height: int, width: int, top: int, left: int, root: "_Window | None"):
        self.root = root
        self.height = height
        self.width = width
        self.top = top
        self.left = left
        self.default_char = " "
        self.default_attr = A_NORMAL
        self._cursor: tuple[int, int] = (0, 0)
        self._nodelay = True
        if root is None:
            self._resize_buffer()

    def _resize_buffer(self) -> None:
        if self.root is not None:
            self.root._resize_buffer()
            return
        height, width = _terminal_size()
        if getattr(self, "height", None) == height and getattr(self, "width", None) == width and hasattr(self, "buffer"):
            return
        self.height = height
        self.width = width
        self.buffer = [[_Cell() for _ in range(width)] for _ in range(height)]
        self._dirty = True

    def _root(self) -> "_Window":
        return self if self.root is None else self.root

    def getmaxyx(self) -> tuple[int, int]:
        root = self._root()
        if self.root is None:
            root._resize_buffer()
            return root.height, root.width
        return self.height, self.width

    def erase(self) -> None:
        root = self._root()
        if root.root is None:
            root._resize_buffer()
        height, width = self.getmaxyx()
        for y in range(height):
            for x in range(width):
                ay = self.top + y
                ax = self.left + x
                if 0 <= ay < root.height and 0 <= ax < root.width:
                    root.buffer[ay][ax] = _Cell(self.default_char, self.default_attr)
        root._dirty = True

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = A_NORMAL) -> None:
        root = self._root()
        height, width = self.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        clipped = str(text)[: max(0, n)]
        for offset, ch in enumerate(clipped):
            tx = x + offset
            if tx >= width:
                break
            ay = self.top + y
            ax = self.left + tx
            if 0 <= ay < root.height and 0 <= ax < root.width:
                root.buffer[ay][ax] = _Cell(ch, attr)
        root._dirty = True

    def noutrefresh(self) -> None:
        self._root()._dirty = True

    def derwin(self, height: int, width: int, top: int, left: int) -> "_Window":
        return _Window(height=height, width=width, top=self.top + top, left=self.left + left, root=self._root())

    def bkgd(self, ch: str, attr: int) -> None:
        self.default_char = (ch or " ")[0]
        self.default_attr = attr

    def box(self) -> None:
        height, width = self.getmaxyx()
        if height < 2 or width < 2:
            return
        self.hline(0, 1, "-", max(0, width - 2))
        self.hline(height - 1, 1, "-", max(0, width - 2))
        for y in range(1, height - 1):
            self.addnstr(y, 0, "|", 1, self.default_attr)
            self.addnstr(y, width - 1, "|", 1, self.default_attr)
        self.addnstr(0, 0, "+", 1, self.default_attr)
        self.addnstr(0, width - 1, "+", 1, self.default_attr)
        self.addnstr(height - 1, 0, "+", 1, self.default_attr)
        self.addnstr(height - 1, width - 1, "+", 1, self.default_attr)

    def hline(self, y: int, x: int, ch: str, width: int) -> None:
        self.addnstr(y, x, (ch or "-")[0] * max(0, width), max(0, width), self.default_attr)

    def move(self, y: int, x: int) -> None:
        root = self._root()
        root._cursor = (self.top + y, self.left + x)
        root._dirty = True

    def nodelay(self, flag: bool) -> None:
        self._nodelay = bool(flag)

    def keypad(self, _flag: bool) -> None:
        return

    def getch(self) -> int:
        if self.root is not None:
            return self._root().getch()
        key = _translate_key()
        if key == -1 and not self._nodelay:
            while key == -1:
                key = _translate_key()
        return key

    def _flush(self) -> None:
        if self.root is not None:
            self._root()._flush()
            return
        if not getattr(self, "_dirty", True):
            return

        self._resize_buffer()
        out: list[str] = ["\x1b[H"]
        for row in self.buffer:
            current_attr = None
            for cell in row:
                if cell.attr != current_attr:
                    out.append(_attr_to_ansi(cell.attr))
                    current_attr = cell.attr
                out.append(cell.ch)
            out.append("\x1b[0m\n")
        cy, cx = self._cursor
        out.append(f"\x1b[{max(1, cy + 1)};{max(1, cx + 1)}H")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._dirty = False
