"""Terminal utilities — size query, raw mode, key reading.

`size()` returns (cols, rows, cell_w_px, cell_h_px). The pixel sizes
come from a TIOCGWINSZ ioctl that kitty fills in — most other emulators
leave them zero, which is fine, we just fall back to assumptions.

`KeyReader` is a context manager that switches stdin to raw mode and
yields decoded key events (`up`, `down`, `left`, `right`, `enter`,
`esc`, `tab`, plain characters, or `None` if no key within timeout).
"""
from __future__ import annotations

import fcntl
import os
import select
import struct
import sys
import termios
import tty
from dataclasses import dataclass
from typing import Optional


@dataclass
class TermSize:
    cols: int
    rows: int
    cell_w: int   # pixels per cell, 0 if unknown
    cell_h: int

    @property
    def width_px(self) -> int:
        return self.cols * self.cell_w

    @property
    def height_px(self) -> int:
        return self.rows * self.cell_h


def size() -> TermSize:
    """Query terminal size in cells and pixels (kitty fills the latter).

    Falls back to 80×24 with no pixel info if stdout isn't a TTY.
    """
    # struct winsize { ws_row, ws_col, ws_xpixel, ws_ypixel }; all uint16
    for fd in (sys.stdout.fileno(), sys.stderr.fileno(), sys.stdin.fileno()):
        try:
            buf = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols, xpx, ypx = struct.unpack("HHHH", buf)
            cell_w = (xpx // cols) if (xpx and cols) else 0
            cell_h = (ypx // rows) if (ypx and rows) else 0
            return TermSize(cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h)
        except OSError:
            continue
    return TermSize(cols=80, rows=24, cell_w=0, cell_h=0)


# --- key reading -----------------------------------------------------------

# Common keys we surface to the UI layer. Plain chars come through as their
# string. Unknown escape sequences come through as their raw form.
KEY_UP = "up"
KEY_DOWN = "down"
KEY_LEFT = "left"
KEY_RIGHT = "right"
KEY_ENTER = "enter"
KEY_ESC = "esc"
KEY_TAB = "tab"
KEY_BACKSPACE = "backspace"
KEY_HOME = "home"
KEY_END = "end"
KEY_PGUP = "pgup"
KEY_PGDN = "pgdn"

_ESCAPES = {
    "[A": KEY_UP,
    "[B": KEY_DOWN,
    "[C": KEY_RIGHT,
    "[D": KEY_LEFT,
    "[H": KEY_HOME,
    "[F": KEY_END,
    "[5~": KEY_PGUP,
    "[6~": KEY_PGDN,
    "OA": KEY_UP,    # alt mode some terminals use
    "OB": KEY_DOWN,
    "OC": KEY_RIGHT,
    "OD": KEY_LEFT,
}


class KeyReader:
    """Context manager that puts stdin in cbreak/raw mode for key events."""

    def __init__(self):
        self._old: Optional[list] = None
        self._fd = sys.stdin.fileno()

    def __enter__(self) -> "KeyReader":
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc) -> None:
        if self._old is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def suspend(self) -> None:
        """Temporarily restore the terminal to its original mode.

        Use this around a subprocess (e.g. mpv) that needs to set up
        its own raw-mode handling. Pair with `resume()` afterward.
        """
        if self._old is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def resume(self) -> None:
        """Re-enter cbreak mode after a `suspend()`."""
        tty.setcbreak(self._fd)

    def read(self, timeout: float = 0.1) -> Optional[str]:
        """Wait up to `timeout` seconds for a key. Returns key name or None.

        Uses os.read on the raw file descriptor so we don't fight Python's
        stdio text-mode buffering, which can delay or chunk bytes from CSI
        sequences like the arrow keys.
        """
        r, _, _ = select.select([self._fd], [], [], timeout)
        if not r:
            return None
        first = os.read(self._fd, 1)
        if not first:
            return None
        b0 = first[0]
        # UTF-8 multi-byte lead byte → slurp continuation bytes so
        # Cyrillic / other non-ASCII keybindings (й, к, а, …) decode
        # as the user-visible character instead of a replacement glyph.
        if 0xC0 <= b0 < 0xF8:
            if b0 < 0xE0:
                need = 1
            elif b0 < 0xF0:
                need = 2
            else:
                need = 3
            try:
                rest = os.read(self._fd, need)
            except OSError:
                rest = b""
            return (first + rest).decode("utf-8", errors="replace")
        ch = first.decode("utf-8", errors="replace")
        if ch == "\x1b":
            # Try to slurp the rest of the escape sequence in one read.
            # Arrow keys send 3 bytes (\x1b [ A) that arrive together.
            r2, _, _ = select.select([self._fd], [], [], 0.05)
            if not r2:
                return KEY_ESC
            rest = os.read(self._fd, 16).decode("utf-8", errors="replace")
            if rest in _ESCAPES:
                return _ESCAPES[rest]
            for k, v in _ESCAPES.items():
                if rest.startswith(k):
                    return v
            return f"esc-{rest}"
        if ch == "\r" or ch == "\n":
            return KEY_ENTER
        if ch == "\t":
            return KEY_TAB
        if ch == "\x7f" or ch == "\x08":
            return KEY_BACKSPACE
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x04":
            return KEY_ESC  # treat EOF as esc
        return ch
