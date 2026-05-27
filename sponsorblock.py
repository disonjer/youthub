#!/usr/bin/env python3.11
"""SponsorBlock client — fetch + cache skip segments for a video.

API: https://wiki.sponsor.ajay.app/w/API_Docs
We hit `GET /api/skipSegments`; on 200 we get a list of segments,
each with `segment: [start, end]` (seconds) and `category`. On 404
the video simply has no segments (cache an empty list so we don't
re-ping for every play).

Cache lives next to the rest of the per-video state at
`cache/sponsorblock_<video_id>.json`. 24 hour TTL — segments rarely
change, and most users would rather skip an ad based on slightly-stale
data than wait for a fresh fetch.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable

import httpx

PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_DIR / "cache"
CACHE_TTL_SEC = 24 * 3600

# Categories we skip by default. Full list from the API:
#   sponsor, selfpromo, interaction, intro, outro, preview,
#   music_offtopic, filler, exclusive_access, poi_highlight
DEFAULT_CATEGORIES = ("sponsor", "selfpromo", "interaction")


def _cache_path(video_id: str) -> Path:
    return CACHE_DIR / f"sponsorblock_{video_id}.json"


def get_segments(
    video_id: str,
    *,
    categories: Iterable[str] = DEFAULT_CATEGORIES,
    timeout: float = 8.0,
) -> list[tuple[float, float, str]]:
    """Return list of (start_sec, end_sec, category) tuples, sorted by start."""
    cp = _cache_path(video_id)
    if cp.exists() and (time.time() - cp.stat().st_mtime) < CACHE_TTL_SEC:
        try:
            data = json.loads(cp.read_text())
            return [tuple(x) for x in data]
        except Exception:
            pass

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    segments: list[tuple[float, float, str]] = []
    try:
        r = httpx.get(
            "https://sponsor.ajay.app/api/skipSegments",
            params={
                "videoID": video_id,
                "categories": json.dumps(list(categories)),
            },
            timeout=timeout,
        )
        if r.status_code == 200:
            for s in r.json():
                seg = s.get("segment")
                if not seg or len(seg) != 2:
                    continue
                # Only skip-action segments; "mute" / "poi" / etc. are
                # not actually skips and would confuse our skip logic.
                if s.get("actionType", "skip") != "skip":
                    continue
                segments.append((float(seg[0]), float(seg[1]),
                                 s.get("category", "?")))
        elif r.status_code != 404:
            # Treat anything other than "no segments" as a transient
            # error — don't cache, retry next time.
            return []
    except Exception:
        return []

    segments.sort(key=lambda x: x[0])
    try:
        cp.write_text(json.dumps(segments))
    except Exception:
        pass
    return segments


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: sponsorblock.py <video_id>")
        sys.exit(2)
    segs = get_segments(sys.argv[1])
    if not segs:
        print("no segments")
    for s, e, c in segs:
        print(f"  {c:15s}  {s:8.1f}  →  {e:8.1f}   (skip {e-s:.1f}s)")
