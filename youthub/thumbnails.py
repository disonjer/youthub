"""Thumbnail fetcher with disk cache and JPEG→PNG conversion.

YouTube returns thumbnails as JPEG, but the kitty graphics protocol's
`f=100` format is PNG-only. So we cache the JPEG on disk (cheap, small)
and convert to PNG bytes in memory on demand.

Concurrent fetching keeps grid load snappy: a 4×3 grid of thumbnails
parallelised hits the wire once per missing tile, in flight together.
"""
from __future__ import annotations

import concurrent.futures
import io
import threading
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "thumbnails"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Reused HTTP client. Pulls proxy from env automatically.
_http = httpx.Client(http2=True, timeout=15.0, follow_redirects=True)
_png_cache: dict[str, bytes] = {}
_png_lock = threading.Lock()


def _jpeg_path(video_id: str) -> Path:
    return CACHE_DIR / f"{video_id}.jpg"


def _download_jpeg(video_id: str, url: Optional[str] = None) -> Optional[bytes]:
    """Fetch the JPEG. If `url` not given, derive from video_id (mqdefault)."""
    if url is None:
        url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    try:
        r = _http.get(url)
        if r.status_code != 200 or not r.content:
            return None
        return r.content
    except httpx.HTTPError:
        return None


def get_jpeg(video_id: str, url: Optional[str] = None) -> Optional[bytes]:
    """Return JPEG bytes for the thumbnail, using disk cache."""
    p = _jpeg_path(video_id)
    if p.exists():
        return p.read_bytes()
    data = _download_jpeg(video_id, url)
    if data is None:
        return None
    p.write_bytes(data)
    return data


def _convert_to_png(jpeg: bytes, target_size: Optional[tuple[int, int]] = None) -> bytes:
    img = Image.open(io.BytesIO(jpeg))
    if img.mode != "RGB":
        img = img.convert("RGB")
    if target_size is not None:
        # Stretch to exact pixel size; aspect-preserving fit would leave
        # black bars in cells. Tiles are pre-sized to thumbnail aspect.
        img = img.resize(target_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)  # fast over small
    return buf.getvalue()


def get_png(video_id: str, *, url: Optional[str] = None,
            target_size: Optional[tuple[int, int]] = None) -> Optional[bytes]:
    """Return PNG bytes ready to feed kitty. Cached in memory by (id, size)."""
    cache_key = f"{video_id}@{target_size}"
    with _png_lock:
        cached = _png_cache.get(cache_key)
    if cached is not None:
        return cached
    jpeg = get_jpeg(video_id, url)
    if jpeg is None:
        return None
    png = _convert_to_png(jpeg, target_size)
    with _png_lock:
        _png_cache[cache_key] = png
    return png


def prefetch(video_ids: list[str], *, urls: Optional[dict[str, str]] = None,
             max_workers: int = 8) -> None:
    """Download missing JPEGs concurrently. Useful before drawing a grid."""
    missing = [v for v in video_ids if not _jpeg_path(v).exists()]
    if not missing:
        return
    urls = urls or {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        pool.map(lambda v: get_jpeg(v, urls.get(v)), missing)


# --- alternate-frame thumbnails (for hover preview) ------------------------

# YouTube exposes 4 sampled frames per video at
#   https://i.ytimg.com/vi/<id>/mq{0,1,2,3}.jpg  (320x180 each)
# mq0.jpg is the same picture as mqdefault.jpg (cover frame). mq1/2/3 are
# the 1/4, 2/4, 3/4-through frames. We use them for the hover preview.


def _frame_jpeg_path(video_id: str, frame_n: int) -> Path:
    return CACHE_DIR / f"{video_id}_mq{frame_n}.jpg"


def get_preview_frame_png(video_id: str, frame_n: int) -> Optional[bytes]:
    """Return PNG bytes for the Nth preview frame (0..3). Disk + mem cached."""
    if frame_n == 0:
        return get_png(video_id)

    cache_key = f"{video_id}@frame{frame_n}"
    with _png_lock:
        cached = _png_cache.get(cache_key)
    if cached is not None:
        return cached

    p = _frame_jpeg_path(video_id, frame_n)
    if p.exists():
        jpeg = p.read_bytes()
    else:
        url = f"https://i.ytimg.com/vi/{video_id}/mq{frame_n}.jpg"
        try:
            r = _http.get(url)
            if r.status_code != 200 or not r.content:
                return None
            jpeg = r.content
            p.write_bytes(jpeg)
        except httpx.HTTPError:
            return None

    png = _convert_to_png(jpeg)
    with _png_lock:
        _png_cache[cache_key] = png
    return png
