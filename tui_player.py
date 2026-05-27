#!/usr/bin/env python3.11
"""Pure-Python TUI video player. No mpv.

Pipeline:
  ffmpeg (subprocess) → MJPEG frames on stdout → parsed here → kitty
    graphics protocol escape sequences → kitty terminal renders pixels.
  ffmpeg #2 → s16le PCM 48 kHz stereo → aplay (ALSA).

Sync: time-of-day clock vs frame_no/fps. Late frames are dropped.
Quit: `q` or Ctrl-C.

This is a proof-of-concept of "play YouTube HD video in a terminal
without mpv". Plays any video file ffmpeg can decode. For YouTube use,
chain it with player.py's bootstrap+download path, or pass a yt-dlp
URL — but for clarity this script just takes a local file path.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path


PNG_SIG = b"\x89PNG\r\n\x1a\n"  # PNG file signature, 8 bytes
PNG_IEND = b"IEND\xaeB`\x82"   # IEND chunk type + CRC (PNG always ends here)
KITTY_IMAGE_ID = 1


def need(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        print(f"[tui] required tool `{tool}` not in PATH", file=sys.stderr)
        sys.exit(1)
    return p


def probe(path: Path) -> dict:
    """Use ffprobe to get fps and dimensions."""
    ffprobe = need("ffprobe")
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,duration",
        "-of", "json",
        str(path),
    ]
    out = subprocess.check_output(cmd).decode()
    data = json.loads(out)["streams"][0]
    # avg_frame_rate is "num/den"
    fr = data.get("avg_frame_rate", "30/1")
    num, _, den = fr.partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    return {
        "width": int(data.get("width") or 0),
        "height": int(data.get("height") or 0),
        "fps": fps,
        "duration": float(data.get("duration") or 0),
    }


def png_stream(stdout):
    """Yield complete PNG frames from a byte stream.

    Each PNG starts with the 8-byte signature and ends with the
    IEND chunk (`IEND` + 4-byte CRC). We scan for IEND, slice out the
    full PNG, and continue.
    """
    buf = bytearray()
    while True:
        chunk = stdout.read(65536)
        if not chunk:
            return
        buf.extend(chunk)
        while True:
            sig = buf.find(PNG_SIG)
            if sig < 0:
                if len(buf) > 8:
                    del buf[:-8]
                break
            iend = buf.find(PNG_IEND, sig + 8)
            if iend < 0:
                if sig > 0:
                    del buf[:sig]
                break
            png_end = iend + len(PNG_IEND)
            png = bytes(buf[sig:png_end])
            del buf[:png_end]
            yield png


def kitty_send(png: bytes, image_id: int = KITTY_IMAGE_ID) -> None:
    """Transmit + display a PNG via kitty graphics protocol.

    Chunks at 4096 base64 chars (≈3072 raw bytes) to respect terminal IO
    buffer limits. Uses `q=2` (silent), `C=1` (don't move cursor), `a=T`
    (transmit and display). Reusing the same image_id makes kitty replace
    the previous frame in place, giving smooth playback.
    """
    enc = base64.standard_b64encode(png)
    chunk = 4096
    parts = [enc[i:i + chunk] for i in range(0, len(enc), chunk)]
    out = sys.stdout.buffer
    for i, p in enumerate(parts):
        m = 1 if i < len(parts) - 1 else 0
        if i == 0:
            head = f"\033_Ga=T,f=100,i={image_id},q=2,C=1,m={m};".encode()
        else:
            head = f"\033_Gm={m},q=2;".encode()
        out.write(head)
        out.write(p)
        out.write(b"\033\\")
    out.flush()


def kitty_delete(image_id: int = KITTY_IMAGE_ID) -> None:
    sys.stdout.buffer.write(f"\033_Ga=d,d=I,i={image_id},q=2\033\\".encode())
    sys.stdout.flush()


class KeyWatcher:
    """Raw-mode stdin reader running in a thread. Sets `event` on quit keys."""

    def __init__(self):
        self.event = threading.Event()
        self.pause = threading.Event()
        self._old_termios = None
        self._thread = None

    def start(self):
        if not sys.stdin.isatty():
            return
        self._old_termios = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self.event.is_set():
            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if not r:
                continue
            ch = sys.stdin.read(1)
            if ch in ("q", "Q", "\x03", "\x04"):
                self.event.set()
                return
            if ch == " ":
                if self.pause.is_set():
                    self.pause.clear()
                else:
                    self.pause.set()

    def stop(self):
        if self._old_termios is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_termios)


def parse_args():
    p = argparse.ArgumentParser(description="Pure TUI video player (no mpv)")
    p.add_argument("input", help="Video file (mp4, mkv, etc.)")
    p.add_argument("--height", type=int, default=720,
                   help="Render height (width auto-scaled). Default 720.")
    p.add_argument("--png-compression", type=int, default=1,
                   help="PNG compression 0=fastest…9=smallest (ffmpeg -compression_level). Default 1 for speed.")
    p.add_argument("--no-audio", action="store_true", help="Mute audio")
    return p.parse_args()


def main():
    args = parse_args()
    ffmpeg = need("ffmpeg")
    if not args.no_audio:
        need("aplay")
    src = Path(args.input)
    if not src.exists():
        print(f"[tui] no such file: {src}", file=sys.stderr)
        return 2

    meta = probe(src)
    fps = meta["fps"] or 30.0
    print(f"[tui] {src.name}: {meta['width']}x{meta['height']} {fps:.2f} fps, "
          f"{meta['duration']:.1f}s — render at {args.height}p", file=sys.stderr)

    # Decoder for video: PNG bytes on stdout. kitty graphics protocol
    # accepts PNG (f=100) but NOT JPEG, so we encode to PNG here.
    video_cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vf", f"scale=-2:{args.height}",
        "-c:v", "png",
        "-compression_level", str(args.png_compression),
        "-f", "image2pipe", "-",
    ]
    video = subprocess.Popen(video_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    # Audio chain: ffmpeg → aplay
    audio_play = None
    audio_dec = None
    if not args.no_audio:
        audio_cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-vn", "-f", "s16le", "-ar", "48000", "-ac", "2", "-",
        ]
        audio_dec = subprocess.Popen(audio_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        audio_play = subprocess.Popen(
            ["aplay", "-q", "-f", "S16_LE", "-r", "48000", "-c", "2"],
            stdin=audio_dec.stdout, stderr=subprocess.DEVNULL,
        )
        audio_dec.stdout.close()  # let audio_dec receive SIGPIPE if aplay dies

    keys = KeyWatcher()
    keys.start()

    # Reserve area so the picture has room above whatever shell scrollback
    # was there. Move cursor to home before each transmit.
    sys.stdout.write("\033[2J\033[H")  # clear screen, home
    sys.stdout.flush()

    start = time.time()
    pause_accumulated = 0.0
    pause_started = None
    frame_no = 0
    dropped = 0
    rendered = 0
    last_status_at = 0.0

    try:
        for png in png_stream(video.stdout):
            if keys.event.is_set():
                break

            # Pause handling — don't burn frames while paused
            while keys.pause.is_set() and not keys.event.is_set():
                if pause_started is None:
                    pause_started = time.time()
                    if audio_play:
                        audio_play.send_signal(signal.SIGSTOP)
                time.sleep(0.05)
            if pause_started is not None:
                pause_accumulated += time.time() - pause_started
                pause_started = None
                if audio_play:
                    audio_play.send_signal(signal.SIGCONT)
                start_effective_shift = pause_accumulated  # used below

            target = start + pause_accumulated + frame_no / fps
            now = time.time()
            delta = target - now
            if delta > 0:
                time.sleep(delta)
            elif delta < -0.2:
                # Behind by >200ms: drop frame
                dropped += 1
                frame_no += 1
                continue

            sys.stdout.write("\033[H")  # cursor home
            sys.stdout.flush()
            try:
                kitty_send(png, image_id=KITTY_IMAGE_ID)
            except BrokenPipeError:
                break
            rendered += 1
            frame_no += 1

            # Tiny status every ~2s, below the image
            if time.time() - last_status_at > 2.0:
                t_now = (time.time() - start - pause_accumulated)
                sys.stdout.write(
                    f"\033[s\033[999;1H"  # save cursor, goto bottom
                    f"\033[2K"  # clear bottom line
                    f"  t={t_now:5.1f}s / {meta['duration']:.1f}s  "
                    f"rendered={rendered}  dropped={dropped}  "
                    f"[q quit · space pause]"
                    f"\033[u"  # restore cursor
                )
                sys.stdout.flush()
                last_status_at = time.time()
    finally:
        keys.event.set()
        kitty_delete()
        try:
            video.terminate()
        except Exception:
            pass
        if audio_play:
            try:
                audio_play.terminate()
            except Exception:
                pass
        if audio_dec:
            try:
                audio_dec.terminate()
            except Exception:
                pass
        keys.stop()
        # Clear screen for clean shell prompt
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        print(f"[tui] rendered {rendered} frames, dropped {dropped}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
