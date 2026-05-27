"""Background-extending video feed — turns TVHTML5's bounded home into
an effectively-infinite scroll.

TVHTML5 `home` returns 4 shelves × 5 = 20 videos with no continuation.
The `/next` pivot of any video gives ~30 more contextual recs. Chain
that recursively (pick an unseen video, fetch its pivot, add unseen
results, repeat) and you get unbounded variety, sourced from YouTube's
actual recommendation graph.

The main thread asks `snapshot()` for the current videos list, and
`maybe_extend()` whenever it's nearing the end of what's loaded. A
single worker thread handles the fetch so the UI doesn't block.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Optional

from . import feed as feed_mod
from . import innertube
from . import thumbnails


class FeedLoader:
    """Holds a growing list of videos. Single background worker extends it."""

    def __init__(self, initial_videos: list[feed_mod.Video],
                 initial_shelf_of: list[str]):
        self._videos: list[feed_mod.Video] = list(initial_videos)
        self._shelf_of: list[str] = list(initial_shelf_of)
        self._seen: set[str] = {v.video_id for v in initial_videos}
        self._lock = threading.Lock()
        self._fetching = threading.Event()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._tube: Optional[innertube.InnerTube] = None

    def snapshot(self) -> tuple[list[feed_mod.Video], list[str]]:
        """Return a copy of the current feed (safe to iterate from UI thread)."""
        with self._lock:
            return list(self._videos), list(self._shelf_of)

    def __len__(self) -> int:
        with self._lock:
            return len(self._videos)

    def is_fetching(self) -> bool:
        return self._fetching.is_set()

    def maybe_extend(self) -> None:
        """Kick off a background fetch if none is in flight."""
        if (self._stop.is_set() or self._fetching.is_set()
                or self._paused.is_set()):
            return
        self._fetching.set()
        threading.Thread(target=self._extend_worker, daemon=True).start()

    def pause(self, wait_timeout: float = 30.0) -> None:
        """Block new extends and wait for any in-flight one to finish.

        Call this before launching mpv: PNG conversions in our worker
        thread steal CPU from the video decoder, and parallel HTTP
        through the shared proxy steals bandwidth from the stream.
        """
        self._paused.set()
        deadline = time.time() + wait_timeout
        while self._fetching.is_set() and time.time() < deadline:
            time.sleep(0.05)

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> None:
        self._stop.set()
        if self._tube is not None:
            try:
                self._tube.close()
            except Exception:
                pass

    def replace(self, videos: list[feed_mod.Video],
                shelf_of: list[str]) -> None:
        """Swap the entire feed for a new one (used by reload/search).

        Resets the dedupe set too — otherwise a later `maybe_extend` would
        skip videos the user just searched up because they happened to
        appear in a prior session.
        """
        with self._lock:
            self._videos = list(videos)
            self._shelf_of = list(shelf_of)
            self._seen = {v.video_id for v in videos}

    # --- worker ----------------------------------------------------------

    def _seed(self) -> Optional[str]:
        """Pick a videoId to expand from — bias toward recently-added so
        results stay related to whatever we're showing the user now."""
        with self._lock:
            if not self._videos:
                return None
            tail = self._videos[-30:] if len(self._videos) > 30 else self._videos
        return random.choice(tail).video_id

    def _extend_worker(self) -> None:
        try:
            seed = self._seed()
            if seed is None:
                return
            if self._tube is None:
                self._tube = innertube.InnerTube()
            raw = self._tube.next(seed)
            pivot = feed_mod.parse_next_pivot(raw)
            new_videos: list[feed_mod.Video] = []
            with self._lock:
                for sh in pivot.shelves:
                    label = sh.title.strip() or "More like this"
                    for v in sh.videos:
                        if v.video_id in self._seen:
                            continue
                        self._seen.add(v.video_id)
                        self._videos.append(v)
                        self._shelf_of.append(label)
                        new_videos.append(v)
            # Warm thumbnail caches so the UI doesn't stall when the user
            # eventually scrolls to these tiles. JPEG download concurrent,
            # PNG conversion sequential (Pillow is GIL-bound but quick).
            if new_videos:
                ids = [v.video_id for v in new_videos]
                thumbnails.prefetch(ids)
                for vid in ids:
                    thumbnails.get_png(vid)
        except Exception:
            # Don't crash the UI on transient API errors — just stop
            # extending. Next call to maybe_extend will retry.
            pass
        finally:
            self._fetching.clear()
