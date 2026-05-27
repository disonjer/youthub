#!/usr/bin/env python3.11
"""Persistent-Chromium streaming player.

The hard part of SABR is the *continuation* request — the browser's
player JS computes a per-request signed payload (field 19.3.20 etc.)
that our hand-rolled SabrClient can't replicate. Instead of guessing
those fields we let a headless Chromium play the video for us and
intercept every videoplayback POST + RESPONSE pair via CDP. The
response bodies are already valid UMP streams — we parse them with
our existing sabr._consume() to fill per-track buffers, then writer
threads stream those buffers into ffmpeg, which remuxes to live
matroska for ffplay.

Pros:
  - Universal: any video, any length. The browser knows how to fetch.
  - No re-bootstrap dance — same Chromium session runs for the whole
    movie, refreshing URLs / sessions as the player JS sees fit.
  - Seek support is just `video.currentTime = X` in the page.

Cons:
  - One headless Chromium per playback session (~300 MB RAM).
  - Network goes through the proxy twice (once for our process when
    we use SABR for previews, once for Chromium for the stream).
"""
from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path(__file__).resolve().parent
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", str(PROJECT_DIR / ".playwright"))
sys.path.insert(0, str(PROJECT_DIR))

import sabr as sabr_mod  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


def _resolve_proxy() -> Optional[str]:
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(var)
        if v:
            return v
    return None


# ---------------------------------------------------------------------------
# SABR consumer — holds parsed track buffers shared with writer threads
# ---------------------------------------------------------------------------

class SabrConsumer:
    """Wraps a SabrClient just for its _consume() / .tracks state.

    We never call its run() — the browser is what issues requests.
    We just feed every captured /videoplayback response body to
    _consume() and the existing parser populates self.tracks.
    """

    def __init__(self):
        # We need a SabrClient to host the parsing state. Pass dummy
        # url + init_body — we never actually use them for fetching.
        self.client = sabr_mod.SabrClient(
            url="x", init_body=b"", bandwidth_bps=10_000_000,
            player_width=1920, player_height=1080, max_height=1080,
        )
        # Avoid __post_init__ trying to parse empty init_body for
        # template; if it fails just bail to an empty Message.
        try:
            import proto_edit as pe
            self.client.template = pe.parse(b"") if not self.client.template else self.client.template
        except Exception:
            pass
        self._lock = threading.Lock()
        # Track if we've seen at least one response (to know when to
        # spawn writers).
        self.bootstrapped = threading.Event()

    def feed(self, response_bytes: bytes) -> None:
        if not response_bytes:
            return
        with self._lock:
            list(self.client._consume(response_bytes))
        if not self.bootstrapped.is_set() and self.client.tracks:
            self.bootstrapped.set()

    @property
    def tracks(self):
        return self.client.tracks


# ---------------------------------------------------------------------------
# Playwright driver — runs the browser in its own thread, captures responses
# ---------------------------------------------------------------------------

async def _drive_browser(video_id: str, consumer: SabrConsumer,
                         stop_event: threading.Event, log) -> None:
    proxy = _resolve_proxy()
    url = f"https://www.youtube.com/watch?v={video_id}"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            proxy={"server": proxy} if proxy else None,
            args=[
                "--no-sandbox",
                "--autoplay-policy=no-user-gesture-required",
                # Without a real audio sink the player can stall — point
                # Chromium at a null ALSA device so audio "works" silently.
                "--alsa-output-device=null",
                "--use-fake-ui-for-media-stream",
            ],
        )
        ctx = await browser.new_context(user_agent=UA)
        await ctx.add_cookies([
            {"name": "CONSENT", "value": "YES+",
             "domain": ".youtube.com", "path": "/"},
            {"name": "SOCS", "value": "CAI",
             "domain": ".youtube.com", "path": "/"},
        ])
        page = await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)
        await cdp.send("Network.enable", {
            "maxTotalBufferSize": 200_000_000,
            "maxResourceBufferSize": 50_000_000,
        })

        # Track active requestIds for /videoplayback so we know which
        # responses to grab. requestIds are short-lived.
        watched: set[str] = set()

        def on_will_be_sent(ev):
            req = ev.get("request") or {}
            u = req.get("url", "")
            if "googlevideo.com/videoplayback" in u and req.get("hasPostData"):
                watched.add(ev["requestId"])

        cdp.on("Network.requestWillBeSent", on_will_be_sent)

        # When a response from a watched POST finishes loading, fetch
        # its body and feed to the SABR parser.
        async def on_loading_finished(ev):
            rid = ev.get("requestId")
            if rid not in watched:
                return
            watched.discard(rid)
            try:
                resp = await cdp.send(
                    "Network.getResponseBody", {"requestId": rid})
                body = resp.get("body", "")
                if resp.get("base64Encoded"):
                    raw = base64.b64decode(body)
                else:
                    raw = body.encode("latin-1")
                log.write(f"[live] response rid={rid} {len(raw)}B\n")
                log.flush()
                consumer.feed(raw)
            except Exception as e:
                log.write(f"[live] getResponseBody({rid}) failed: {e}\n")

        cdp.on("Network.loadingFinished",
               lambda ev: asyncio.create_task(on_loading_finished(ev)))

        await page.goto(url, wait_until="domcontentloaded")
        # Mute and start playback. Real player gets autoplay-on-mute.
        await page.evaluate(
            "() => { const v = document.querySelector('video'); "
            "if (v) { v.muted = true; v.play().catch(()=>{}); } }"
        )
        log.write("[live] browser playing\n")
        log.flush()

        # Hold here until told to stop. Periodically nudge the page in
        # case it stalled (paused after losing focus, etc.).
        last_nudge = 0.0
        while not stop_event.is_set():
            await asyncio.sleep(0.5)
            now = time.time()
            if now - last_nudge > 10:
                try:
                    info = await page.evaluate(
                        "() => { const v = document.querySelector('video'); "
                        "if (!v) return null; "
                        "return {ct: v.currentTime, paused: v.paused, "
                        "ended: v.ended, buf: v.buffered.length>0 ? "
                        "v.buffered.end(v.buffered.length-1) : 0}; }"
                    )
                    log.write(f"[live] nudge {info}\n")
                    log.flush()
                    if info and info.get("paused"):
                        await page.evaluate(
                            "() => document.querySelector('video').play()"
                        )
                except Exception:
                    pass
                last_nudge = now
        await browser.close()


# ---------------------------------------------------------------------------
# Writer threads + ffmpeg/ffplay (same pattern as stream_player.py)
# ---------------------------------------------------------------------------

def writer_thread(track_state, fifo_path: Path, stop_event: threading.Event,
                  label: str, log) -> None:
    log.write(f"[writer:{label}] open {fifo_path.name}\n")
    log.flush()
    try:
        fd = os.open(str(fifo_path), os.O_WRONLY)
    except Exception as e:
        log.write(f"[writer:{label}] open failed: {e}\n")
        return
    log.write(f"[writer:{label}] connected\n")
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
                    stop_event.set()
                    return
                emitted = buf_end
            else:
                time.sleep(0.05)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def stream_video(video_id: str, *, log_path: Optional[Path] = None) -> int:
    log = open(log_path, "a") if log_path else open(os.devnull, "w")
    v_fifo = a_fifo = None
    try:
        log.write(f"\n=== live_stream {video_id} ===\n")
        log.flush()

        consumer = SabrConsumer()
        stop_event = threading.Event()

        # Launch Playwright driver in its own asyncio loop / thread.
        loop_holder: dict = {}

        def browser_thread() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop_holder["loop"] = loop
            try:
                loop.run_until_complete(
                    _drive_browser(video_id, consumer, stop_event, log))
            except Exception as e:
                log.write(f"[live] browser thread error: {e}\n")
            finally:
                loop.close()

        bt = threading.Thread(target=browser_thread, daemon=True)
        bt.start()

        # Wait for first response (bootstrap) so we know what tracks
        # exist before opening fifos / launching ffmpeg.
        log.write("[live] waiting for first response …\n")
        log.flush()
        if not consumer.bootstrapped.wait(timeout=60):
            log.write("[live] no response in 60s — aborting\n")
            stop_event.set()
            return 1

        # Discover keys
        video_key = audio_key = None
        for key in consumer.tracks:
            if key.audio_track_id:
                audio_key = key
            else:
                video_key = key
        log.write(f"[live] tracks: video={video_key} audio={audio_key}\n")
        log.flush()
        if not video_key or not audio_key:
            stop_event.set()
            return 1

        # Fifos + ffmpeg + ffplay (same as stream_player.py)
        tmp = Path("/tmp") / f"yt-live-{os.getpid()}"
        tmp.mkdir(exist_ok=True)
        v_fifo = tmp / "video.fifo"
        a_fifo = tmp / "audio.fifo"
        for p in (v_fifo, a_fifo):
            if p.exists():
                p.unlink()
            os.mkfifo(p)

        ffmpeg = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "mp4", "-i", str(v_fifo),
            "-f", "matroska", "-i", str(a_fifo),
            "-c", "copy", "-map", "0:v:0", "-map", "1:a:0",
            "-f", "matroska", "-live", "1", "pipe:1",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log)

        ffplay_env = dict(os.environ)
        ffplay_env["DISPLAY"] = ":0"
        ffplay = subprocess.Popen([
            "ffplay", "-hide_banner", "-loglevel", "warning",
            "-autoexit", "-alwaysontop",
            "-window_title", f"YouTube — {video_id}",
            "-f", "matroska", "-i", "pipe:0",
        ], stdin=ffmpeg.stdout, stdout=log, stderr=log, env=ffplay_env)
        ffmpeg.stdout.close()  # type: ignore[union-attr]

        # Writers
        writers = []
        for key, name, fifo in [(video_key, "video", v_fifo),
                                 (audio_key, "audio", a_fifo)]:
            t = threading.Thread(
                target=writer_thread,
                args=(consumer.tracks[key], fifo, stop_event, name, log),
                daemon=True)
            t.start()
            writers.append(t)

        rc = ffplay.wait()
        stop_event.set()
        try:
            ffmpeg.terminate(); ffmpeg.wait(timeout=2)
        except Exception:
            try: ffmpeg.kill()
            except: pass
        for w in writers:
            w.join(timeout=2)
        bt.join(timeout=3)
        log.write(f"[live] ffplay rc={rc}\n")
        return rc
    finally:
        for p in (v_fifo, a_fifo):
            if p is not None:
                try: p.unlink()
                except OSError: pass
        log.close()


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--log", default=str(PROJECT_DIR / "cache" / "live.log"))
    args = ap.parse_args()
    vid = args.video
    if "://" in vid:
        import re
        m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", vid)
        if m:
            vid = m.group(1)
    return stream_video(vid, log_path=Path(args.log))


if __name__ == "__main__":
    sys.exit(main())
