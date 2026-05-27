"""kitty graphics protocol primitives — transmit, place, replace, delete.

Protocol summary (full spec: sw.kovidgoyal.net/kitty/graphics-protocol):

  \033_G<keys>;<base64-payload>\033\\

Keys we use:
  a   action: T=transmit+display, t=transmit only, p=place, d=delete
  i   image id (we allocate)
  q   quiet level (2 = suppress all responses; we never read them)
  f   format: 100 = PNG, 24 = RGB raw, 32 = RGBA raw
  m   more-chunks flag (1 means more chunks coming, 0 means last)
  C   cursor movement: 1 = don't move cursor after display
  c   width  in cells
  r   height in cells
  z   z-index (negative = behind text)
  d   delete what: I = by id, A = all, etc.

The terminal won't necessarily echo errors at quiet=2 — if a frame is
silently dropped, retry with q=0 to debug.
"""
from __future__ import annotations

import base64
import sys
from typing import IO

# Chunk size in bytes of base64 payload. The kitty docs say to keep
# each chunk ≤ 4096 b64 chars to avoid terminal IO buffer trouble.
CHUNK = 4096


def _write(out: IO[bytes], data: bytes) -> None:
    out.write(data)


def transmit_and_place(image_id: int, png: bytes, *,
                       width_cells: int, height_cells: int,
                       z_index: int = 0,
                       out: IO[bytes] | None = None) -> None:
    """Send a PNG and display it at the current cursor position.

    The image fills exactly `width_cells × height_cells` terminal cells.
    Reusing the same image_id replaces a previously transmitted image in
    place — kitty redraws without flicker.

    Caller is responsible for positioning the cursor first (eg. via
    `\\033[<row>;<col>H`).
    """
    out = out or sys.stdout.buffer
    enc = base64.standard_b64encode(png)
    parts = [enc[i:i + CHUNK] for i in range(0, len(enc), CHUNK)]
    if not parts:
        return
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        if i == 0:
            head = (
                f"\033_Ga=T,f=100,i={image_id},q=2,C=1,"
                f"c={width_cells},r={height_cells},z={z_index},m={0 if is_last else 1};"
            ).encode()
        else:
            head = f"\033_Gm={0 if is_last else 1},q=2;".encode()
        _write(out, head)
        _write(out, part)
        _write(out, b"\033\\")
    out.flush()


def delete_image(image_id: int, *, out: IO[bytes] | None = None) -> None:
    """Remove a previously transmitted image."""
    out = out or sys.stdout.buffer
    _write(out, f"\033_Ga=d,d=I,i={image_id},q=2\033\\".encode())
    out.flush()


def delete_all(out: IO[bytes] | None = None) -> None:
    out = out or sys.stdout.buffer
    _write(out, b"\033_Ga=d,d=A,q=2\033\\")
    out.flush()


def move_cursor(row: int, col: int, out: IO[bytes] | None = None) -> None:
    """1-indexed. (1,1) is top-left."""
    out = out or sys.stdout.buffer
    _write(out, f"\033[{row};{col}H".encode())


def clear_screen(out: IO[bytes] | None = None) -> None:
    out = out or sys.stdout.buffer
    _write(out, b"\033[2J\033[H")
    out.flush()


def reset_terminal_modes(out: IO[bytes] | None = None) -> None:
    """Disable mouse tracking, focus reporting, bracketed paste, show cursor.

    mpv and other TUIs may turn these on and not always restore them on
    exit (or crash mid-frame). The escape sequences are no-ops if the
    mode wasn't enabled, so this is safe to call defensively.
    """
    out = out or sys.stdout.buffer
    seqs = [
        b"\033[?1000l",  # X10 mouse compatibility off
        b"\033[?1002l",  # cell-motion mouse tracking off
        b"\033[?1003l",  # all-motion mouse tracking off
        b"\033[?1006l",  # SGR extended mouse off
        b"\033[?1015l",  # urxvt mouse off
        b"\033[?1004l",  # focus-in/out reporting off
        b"\033[?2004l",  # bracketed paste off
        b"\033[?25h",    # show cursor
    ]
    _write(out, b"".join(seqs))
    out.flush()
