#!/usr/bin/env python3.11
"""Streaming SABR → ffmpeg → ffplay pipeline.

Architecture:

    SabrClient (background thread)
        │  fills client.tracks[key].buf with bytes per track
        ▼
    Per-track writer threads
        │  open their fifo for write (blocks until ffmpeg opens
        │  the corresponding read end), then continuously copy
        │  buf[emitted:end] → fifo until the track ends.
        ▼
    ffmpeg
        │  reads both fifos, remuxes into "live" matroska on stdout
        ▼
    ffplay  (the X11 window the user sees)

The SABR loop and the writer threads are decoupled: SABR keeps
filling buffers regardless of writer pacing, and each writer drains
its track at whatever rate ffmpeg consumes. This is what lets ffmpeg
parse the video header (which arrives early in our buf) before it
even tries to open the audio fifo.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

import bootstrap as bs_mod  # noqa: E402
import sabr as sabr_mod  # noqa: E402


# ---------------------------------------------------------------------------
# SABR loop — just fills buffers, doesn't emit
# ---------------------------------------------------------------------------

def run_sabr_loop(client: sabr_mod.SabrClient,
                  stop_event: threading.Event,
                  max_iters: int = 100000) -> None:
    """Run SABR fetching loop, mutating client.tracks[*].buf in place."""
    list(client._do_bootstrap())
    empty_streak = 0
    playhead_ms = 0
    for i in range(max_iters):
        if stop_event.is_set():
            return
        tracks = client.tracks
        if tracks and all(t.ended for t in tracks.values()):
            print("[sabr] all tracks ended", file=sys.stderr)
            return
        selected = {
            key: st.buffered_end_ms for key, st in tracks.items()
            if not st.ended
        }
        if not selected:
            return
        min_buf = min(selected.values())
        playhead_ms = max(0, min_buf - 1000)
        body = sabr_mod.build_request_body(
            client.template, selected, playhead_ms=playhead_ms,
            bandwidth_bps=client.bandwidth_bps,
            player_width=client.player_width,
            player_height=client.player_height,
            max_height=client.max_height,
        )
        before = {k: len(s.buf) for k, s in tracks.items()}
        status, resp = sabr_mod.http_post(client.url, body)
        if status != 200:
            print(f"[sabr] HTTP {status} — stopping", file=sys.stderr)
            return
        list(client._consume(resp))
        after = {k: len(s.buf) for k, s in tracks.items()}
        grew = any(after[k] > before.get(k, 0) for k in after)
        if not grew:
            empty_streak += 1
            playhead_ms += 1000
            if empty_streak >= 30:
                print("[sabr] 30 empty responses — assuming EOF",
                      file=sys.stderr)
                return
            if empty_streak <= 3:
                time.sleep(0.5)
            elif empty_streak <= 10:
                time.sleep(1.5)
            else:
                time.sleep(3.0)
        else:
            empty_streak = 0


# ---------------------------------------------------------------------------
# Writer thread — copies buf[emitted:end] of one track into a fifo
# ---------------------------------------------------------------------------

def writer_thread(track_state: sabr_mod.TrackState,
                  fifo_path: Path,
                  stop_event: threading.Event,
                  label: str,
                  log) -> None:
    """Open fifo for write (blocks until ffmpeg opens the read end),
    then drain `track_state.buf` into it until the track ends or
    `stop_event` fires."""
    log.write(f"[writer:{label}] opening {fifo_path.name} for write…\n")
    log.flush()
    try:
        fd = os.open(str(fifo_path), os.O_WRONLY)
    except Exception as e:
        log.write(f"[writer:{label}] open failed: {e}\n")
        return
    log.write(f"[writer:{label}] fifo connected — draining\n")
    log.flush()

    emitted = 0
    try:
        while not stop_event.is_set():
            buf_end = len(track_state.buf)
            if buf_end > emitted:
                chunk = bytes(track_state.buf[emitted:buf_end])
                try:
                    view = memoryview(chunk)
                    while view:
                        n = os.write(fd, view)
                        view = view[n:]
                except BrokenPipeError:
                    log.write(f"[writer:{label}] reader closed\n")
                    stop_event.set()
                    return
                emitted = buf_end
            elif track_state.ended:
                log.write(f"[writer:{label}] track ended at {emitted}B\n")
                return
            else:
                # Wait for SABR to put more data in buf
                time.sleep(0.05)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def stream_video(video_id: str, *,
                 log_path: Optional[Path] = None) -> int:
    """Bootstrap → start SABR + writers + ffmpeg + ffplay → wait."""
    log = open(log_path, "a") if log_path else open(os.devnull, "w")
    v_fifo = a_fifo = None
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        log.write(f"\n=== stream_video {video_id} ===\n")
        log.flush()

        # 1. Bootstrap (Playwright)
        boot = bs_mod.bootstrap(url)
        init_body = Path(boot.init_body_path).read_bytes()
        log.write(f"[stream] SABR URL ok, init body {len(init_body)}B\n")
        log.flush()

        # 2. Build SABR client and pre-fetch the first response so the
        # buffers contain the init segment + early media chunks. This
        # makes track discovery deterministic before we set up writers.
        client = sabr_mod.SabrClient(
            url=boot.sabr_url, init_body=init_body,
            bandwidth_bps=10_000_000,
            player_width=1920, player_height=1080, max_height=1080,
        )
        list(client._do_bootstrap())
        log.write(f"[stream] tracks discovered: "
                  f"{[ (k.itag, bool(k.audio_track_id)) for k in client.tracks]}\n")
        log.flush()

        # Identify which track is video and which is audio.
        video_key = audio_key = None
        for key in client.tracks:
            if key.audio_track_id:
                audio_key = key
            else:
                video_key = key
        if not video_key or not audio_key:
            raise RuntimeError(
                f"SABR didn't deliver both tracks: "
                f"video={video_key}, audio={audio_key}")

        # 3. Make fifos
        tmp = Path("/tmp") / f"yt-stream-{os.getpid()}"
        tmp.mkdir(exist_ok=True)
        v_fifo = tmp / "video.fifo"
        a_fifo = tmp / "audio.fifo"
        for p in (v_fifo, a_fifo):
            if p.exists():
                p.unlink()
            os.mkfifo(p)

        # 4. ffmpeg: read both fifos, remux to live matroska on stdout
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "warning",
            "-f", "mp4", "-i", str(v_fifo),
            "-f", "matroska", "-i", str(a_fifo),
            "-c", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-f", "matroska", "-live", "1",
            "pipe:1",
        ]
        ffmpeg = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=log,
        )

        # 5. ffplay reads ffmpeg's matroska on its stdin
        ffplay_cmd = [
            "ffplay",
            "-hide_banner", "-loglevel", "warning",
            "-autoexit", "-alwaysontop",
            "-window_title", f"YouTube — {video_id}",
            "-f", "matroska", "-i", "pipe:0",
        ]
        ffplay = subprocess.Popen(
            ffplay_cmd,
            stdin=ffmpeg.stdout,
            stdout=log, stderr=log,
        )
        ffmpeg.stdout.close()  # type: ignore[union-attr]

        # 6. Start writer threads — they drain client.tracks[*].buf to
        # the fifos as fast as ffmpeg can consume. Open of the fifo
        # blocks until ffmpeg opens the read end.
        stop_event = threading.Event()
        writers = [
            threading.Thread(
                target=writer_thread,
                args=(client.tracks[video_key], v_fifo, stop_event,
                      "video", log),
                daemon=True,
            ),
            threading.Thread(
                target=writer_thread,
                args=(client.tracks[audio_key], a_fifo, stop_event,
                      "audio", log),
                daemon=True,
            ),
        ]
        for t in writers:
            t.start()

        # 7. Background watcher: when ffplay quits, signal stop so the
        # main-thread SABR loop wakes up and exits.
        def watch_ffplay() -> None:
            ffplay.wait()
            stop_event.set()

        watcher = threading.Thread(target=watch_ffplay, daemon=True)
        watcher.start()

        # 8. SABR loop — IN THE MAIN THREAD. Running the loop in a
        # worker thread produced empty server responses (the response
        # to iter 0 was 125B "no media" even though identical code in
        # batch mode gets ~5MB). The exact cause is unclear (urllib
        # interaction with subprocess.fork that happened just before?
        # GIL state?), but running it in the main thread cleanly
        # restores the batch-mode behavior.
        log.write("[sabr] main-thread loop starting\n")
        log.flush()
        empty_streak = 0
        playhead_ms = 0
        try:
            for i in range(100000):
                if stop_event.is_set():
                    break
                tracks = client.tracks
                if tracks and all(t.ended for t in tracks.values()):
                    break
                selected = {
                    key: st.buffered_end_ms
                    for key, st in tracks.items()
                    if not st.ended
                }
                if not selected:
                    break
                min_buf = min(selected.values())
                playhead_ms = max(0, min_buf - 1000)
                body = sabr_mod.build_request_body(
                    client.template, selected,
                    playhead_ms=playhead_ms,
                    bandwidth_bps=client.bandwidth_bps,
                    player_width=client.player_width,
                    player_height=client.player_height,
                    max_height=client.max_height,
                )
                before = {k: len(s.buf) for k, s in tracks.items()}
                status, resp = sabr_mod.http_post(client.url, body)
                log.write(f"[sabr] iter {i} POST {len(body)}B "
                          f"ph={playhead_ms} → HTTP {status} "
                          f"resp {len(resp)}B\n")
                log.flush()
                if status != 200:
                    break
                list(client._consume(resp))
                after = {k: len(s.buf) for k, s in tracks.items()}
                grew = any(after[k] > before.get(k, 0) for k in after)
                if not grew:
                    empty_streak += 1
                    playhead_ms += 5000
                    if empty_streak >= 40:
                        log.write("[sabr] 40 empty responses — EOF\n")
                        break
                    # Wait long enough for the buffer to "drain" from
                    # the server's perspective. ffmpeg/ffplay still has
                    # the previously-fetched buffer to chew through, so
                    # playback continues during these waits.
                    if empty_streak == 1:
                        wait = 1.0
                    elif empty_streak <= 5:
                        wait = 5.0
                    else:
                        wait = 10.0
                    # Wake early if ffplay died.
                    if stop_event.wait(timeout=wait):
                        break
                else:
                    if empty_streak > 0:
                        log.write(f"[sabr] iter {i} broke "
                                  f"{empty_streak}-empty streak\n")
                        log.flush()
                    empty_streak = 0
        except Exception as e:
            log.write(f"[sabr] main loop error: {e}\n")
        finally:
            # Mark all tracks ended → writers will drain remaining buf
            # and exit; ffmpeg sees EOF; ffplay finishes naturally.
            for st in client.tracks.values():
                st.ended = True

        # 9. Wait for ffplay (already may have exited)
        rc = ffplay.wait()
        stop_event.set()

        # Teardown
        try:
            ffmpeg.terminate()
            ffmpeg.wait(timeout=2)
        except Exception:
            try:
                ffmpeg.kill()
            except Exception:
                pass
        for t in writers:
            t.join(timeout=2)

        log.write(f"[stream] ffplay rc={rc}\n")
        return rc
    finally:
        for p in (v_fifo, a_fifo):
            if p is not None:
                try:
                    p.unlink()
                except OSError:
                    pass
        log.close()


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--log", default=str(PROJECT_DIR / "cache" / "stream.log"))
    args = ap.parse_args()
    vid = args.video
    if "://" in vid:
        vid = bs_mod.video_id_from_url(vid)
    return stream_video(vid, log_path=Path(args.log))


if __name__ == "__main__":
    sys.exit(main())
