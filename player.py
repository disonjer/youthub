#!/usr/bin/env python3.11
"""Console YouTube player.

Usage: player.py <youtube_url> [--force-bootstrap] [--force-download] [--keep-window]

Pipeline:
  1. Bootstrap (Chromium): capture SABR URL + base POST body for the video.
     Cached per-video; only re-runs if cache stale or `--force-bootstrap`.
  2. SABR download: pull HD video + audio fMP4 buffers (≈60s window per
     bootstrap; longer playback would need a fresh bootstrap).
  3. ffmpeg mux: combine video + audio into a single .mp4 container.
  4. mpv --vo=kitty: play the result in the kitty terminal.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)
BIN_MPV = PROJECT_DIR / "bin" / "mpv"
PLAYWRIGHT_BROWSERS = PROJECT_DIR / ".playwright"

# Make Playwright + flatpak mpv find their stuff regardless of caller env
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PLAYWRIGHT_BROWSERS))

sys.path.insert(0, str(PROJECT_DIR))
import bootstrap as bs  # noqa: E402
import sabr as S        # noqa: E402


def need(tool: str, hint: str | None = None) -> str:
    p = shutil.which(tool)
    if p:
        return p
    msg = f"`{tool}` not found in PATH"
    if hint:
        msg += f" ({hint})"
    print(f"[player] {msg}", file=sys.stderr)
    sys.exit(1)


def video_paths(video_id: str) -> dict[str, Path]:
    d = CACHE_DIR / f"video_{video_id}"
    d.mkdir(exist_ok=True)
    return {
        "dir": d,
        "video": d / "video.fmp4",
        "audio": d / "audio.fmp4",
        "muxed": d / "muxed.mp4",
        "meta": d / "meta.txt",
    }


def download_streams(boot: bs.Bootstrap, paths: dict, max_iters: int = 400) -> tuple[Path, Path]:
    """Run SABR client, write video.fmp4 + audio.fmp4. Returns their paths."""
    init_body = Path(boot.init_body_path).read_bytes()
    print(f"[player] init body {len(init_body)}B; running SABR…", file=sys.stderr)

    client = S.SabrClient(
        url=boot.sabr_url,
        init_body=init_body,
        bandwidth_bps=10_000_000,
        player_width=1920,
        player_height=1080,
        max_height=1080,
    )
    client.run(max_iters=max_iters)

    video_track = audio_track = None
    for key, st in client.tracks.items():
        if key.audio_track_id:
            audio_track = (key, st)
        else:
            video_track = (key, st)

    if video_track is None or audio_track is None:
        raise RuntimeError(
            f"SABR run gave incomplete tracks: video={video_track is not None}, audio={audio_track is not None}"
        )

    vk, vs = video_track
    ak, as_ = audio_track
    paths["video"].write_bytes(bytes(vs.buf))
    paths["audio"].write_bytes(bytes(as_.buf))
    paths["meta"].write_text(
        f"video_itag={vk.itag} ({len(vs.buf):,} B, ~{vs.buffered_end_ms/1000:.1f}s)\n"
        f"audio_itag={ak.itag} ({len(as_.buf):,} B, ~{as_.buffered_end_ms/1000:.1f}s)\n"
        f"obtained_at={int(time.time())}\n"
    )
    print(
        f"[player] downloaded video itag={vk.itag} ({len(vs.buf):,}B) "
        f"+ audio itag={ak.itag} ({len(as_.buf):,}B)",
        file=sys.stderr,
    )
    return paths["video"], paths["audio"]


def mux(video_path: Path, audio_path: Path, out_path: Path) -> Path:
    """ffmpeg-mux video + audio fMP4 → mp4 container (no re-encode)."""
    ffmpeg = need("ffmpeg")
    cmd = [
        ffmpeg, "-y", "-loglevel", "warning",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-c", "copy",
        "-map", "0:v:0",
        "-map", "1:a:0",
        str(out_path),
    ]
    print(f"[player] muxing → {out_path.name}", file=sys.stderr)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg mux failed (exit {r.returncode})")
    return out_path


def play(mp4_path: Path) -> int:
    """Hand the muxed file to fresh mpv with kitty graphics output."""
    if not BIN_MPV.exists():
        print(f"[player] {BIN_MPV} missing (expected flatpak wrapper)", file=sys.stderr)
        return 1
    print(f"[player] launching mpv --vo=kitty {mp4_path.name}", file=sys.stderr)
    print(f"[player] controls: q quit, space pause, ←/→ seek 5s, ↑/↓ seek 60s", file=sys.stderr)
    cmd = [str(BIN_MPV), "--vo=kitty", str(mp4_path)]
    # Inherit stdin/stdout/stderr so kitty graphics writes directly to the terminal
    res = subprocess.run(cmd)
    return res.returncode


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Console YouTube player (HD via reverse-engineered SABR)")
    p.add_argument("url", help="YouTube watch URL")
    p.add_argument("--force-bootstrap", action="store_true",
                   help="Re-run Chromium bootstrap even if cached")
    p.add_argument("--force-download", action="store_true",
                   help="Re-run SABR download even if buffers cached")
    p.add_argument("--no-play", action="store_true",
                   help="Stop after muxing; don't launch mpv")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # 1. Bootstrap (Chromium captures URL + base POST body — cached per-video)
    boot = bs.bootstrap(args.url, force=args.force_bootstrap)
    paths = video_paths(boot.video_id)

    # 2. Download streams (SABR loop — cached per-video)
    if args.force_download or not (paths["video"].exists() and paths["audio"].exists()):
        download_streams(boot, paths)
    else:
        print(f"[player] using cached buffers in {paths['dir']}", file=sys.stderr)

    # 3. Mux video+audio into one mp4
    if args.force_download or not paths["muxed"].exists():
        mux(paths["video"], paths["audio"], paths["muxed"])
    else:
        print(f"[player] using cached muxed file {paths['muxed'].name}", file=sys.stderr)

    print(f"[player] cache: {paths['dir']}", file=sys.stderr)

    if args.no_play:
        print(str(paths["muxed"]))
        return 0

    # 4. Play in kitty
    return play(paths["muxed"])


if __name__ == "__main__":
    sys.exit(main())
