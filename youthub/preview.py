"""Hover preview — cycles through the 4 sampled frames of a video.

YouTube provides four sampled frames per video at i.ytimg.com under
mq0..mq3.jpg (cover, 1/4, 2/4, 3/4 through). We can't get an actual
animated WebP any more (the `an_webp/<id>/mqdefault_6s.webp` endpoint
returns 404 since YouTube switched to streaming previews) but cycling
through these four frames gives a recognisable slideshow effect that
suggests video content without the cost of decoding real frames.

To hide the hard cuts between unrelated photos we generate a few
in-between alpha-blended frames per transition. PIL's `Image.blend()`
is cheap; we precompute the blends once when a preview starts and
then just feed them to the display callback at the right rhythm.

A `PreviewPlayer` runs the cycle in a background thread. The UI thread
gives it a frame-display callback (which must be thread-safe — typically
a closure that acquires the screen lock and transmits via kitty).
"""
from __future__ import annotations

import io
import threading
import time
from typing import Callable, Optional

from PIL import Image

from . import thumbnails


# How long each "real" frame stays fully on screen.
FRAME_HOLD = 0.85
# Length of the crossfade between adjacent frames.
TRANSITION = 0.20
# How many alpha-blended frames we insert per transition. More = smoother,
# but each one is a PNG encode + a kitty transmit.
BLEND_STEPS = 3
# Frames included in the cycle. 0 is the cover (already on screen as the
# static thumb) — we include it so the preview periodically returns to
# the canonical shot.
FRAMES = (1, 2, 3, 0)


FrameCb = Callable[[bytes], None]


def _blend_pngs(png_a: bytes, png_b: bytes,
                steps: int = BLEND_STEPS) -> list[bytes]:
    """Return `steps` intermediate PNG frames cross-fading from A to B.

    Alpha goes 1/(N+1) .. N/(N+1) — never 0 or 1, those are the
    surrounding "real" frames themselves.
    """
    a = Image.open(io.BytesIO(png_a)).convert("RGB")
    b = Image.open(io.BytesIO(png_b)).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)
    out: list[bytes] = []
    for i in range(1, steps + 1):
        alpha = i / (steps + 1)
        blended = Image.blend(a, b, alpha)
        buf = io.BytesIO()
        blended.save(buf, format="PNG", compress_level=1)
        out.append(buf.getvalue())
    return out


class PreviewPlayer:
    """One per app. Start it when focus settles, stop it when focus moves.

    The instance is reusable: each `start()` call cancels any previous
    playback and begins a new one.
    """

    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current_id: Optional[str] = None
        self._lock = threading.Lock()

    def is_playing_for(self, video_id: str) -> bool:
        return (self._current_id == video_id
                and self._thread is not None
                and self._thread.is_alive())

    def start(self, video_id: str, frame_cb: FrameCb) -> None:
        with self._lock:
            if self.is_playing_for(video_id):
                return
            self._stop_locked()
            self._current_id = video_id
            self._stop = threading.Event()
            t = threading.Thread(
                target=self._loop,
                args=(video_id, frame_cb, self._stop),
                daemon=True,
            )
            self._thread = t
        t.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._thread and self._thread.is_alive():
            self._stop.set()
            t = self._thread
            # Release lock briefly so worker can finish current cb call.
            self._thread = None
            self._current_id = None
            self._lock.release()
            try:
                t.join(timeout=0.3)
            finally:
                self._lock.acquire()
        else:
            self._thread = None
            self._current_id = None

    def _loop(self, video_id: str, frame_cb: FrameCb,
              stop_evt: threading.Event) -> None:
        # Pre-fetch + convert all 4 frames sequentially. The JPEGs are
        # tiny and the thumbnails module shares a connection so this is
        # usually <300ms total.
        pngs: list[bytes] = []
        for n in FRAMES:
            if stop_evt.is_set():
                return
            png = thumbnails.get_preview_frame_png(video_id, n)
            if png is not None:
                pngs.append(png)
        if not pngs or stop_evt.is_set():
            return

        # Precompute blend frames once. This costs ~50-150ms total, paid
        # at preview start. After that we only do kitty transmits.
        sequence: list[tuple[bytes, float]] = []  # (png, duration_seconds)
        n_frames = len(pngs)
        per_blend = TRANSITION / max(1, BLEND_STEPS) if n_frames > 1 else 0

        for i in range(n_frames):
            a = pngs[i]
            sequence.append((a, FRAME_HOLD))
            if n_frames > 1:
                b = pngs[(i + 1) % n_frames]
                for blend in _blend_pngs(a, b):
                    if stop_evt.is_set():
                        return
                    sequence.append((blend, per_blend))

        if not sequence or stop_evt.is_set():
            return

        # First iteration: instead of cutting from the on-screen static
        # thumbnail (mq0) straight to the first cycle frame (hold mq1),
        # we start at the blend that *leads into* the cycle from mq0.
        # That's the mq0→mq1 transition the cycle naturally contains as
        # its last step. Starting there gives a smooth fade-in matching
        # what the user already sees, and the cycle wraps cleanly without
        # any duplicated transition.
        idx = 0
        if n_frames > 1 and 0 in FRAMES:
            pos = FRAMES.index(0)
            # Each segment = 1 hold + BLEND_STEPS blends. Skip the hold
            # of mq0 (it's identical to what's already on screen) and
            # land on the first blend after it.
            idx = pos * (1 + BLEND_STEPS) + 1
        while not stop_evt.is_set():
            png, hold = sequence[idx]
            try:
                frame_cb(png)
            except Exception:
                # Drawing errors shouldn't kill the worker — UI may be
                # mid-resize. Skip this frame and keep going.
                pass
            idx = (idx + 1) % len(sequence)
            deadline = time.time() + hold
            while not stop_evt.is_set() and time.time() < deadline:
                # Short sleeps so stop responds quickly.
                time.sleep(min(0.04, deadline - time.time()))
