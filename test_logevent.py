#!/usr/bin/env python3.11
"""Probe: send a TVHTML5 log_event watchtime ping and report what YT
returns. Doesn't touch the player code. Used to find a schema that
makes the test video show up in FEhistory.

Usage:
    python3.11 test_logevent.py <video_id>
"""
from __future__ import annotations

import json
import random
import string
import sys

from youthub import auth, innertube


def _cpn() -> str:
    """11-char URL-safe random nonce, same alphabet Cobalt uses."""
    alpha = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(alpha) for _ in range(16))


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: test_logevent.py <video_id>\n")
        return 2
    vid = sys.argv[1]
    cpn = _cpn()

    tokens = auth.get_tokens()
    if tokens is None:
        print("no OAuth tokens", file=sys.stderr)
        return 1

    it = innertube.InnerTube(tokens)
    import time as _time

    referrer = f"https://www.youtube.com/watch?v={vid}"

    # 1) Establish watch context — TV apps always call /next first.
    print(f"[1/N] /next for {vid}  → watch context")
    try:
        it.next(vid)
    except Exception as e:
        print(f"  FAIL: {e}")
        return 1
    print("  ok")

    # 2) Initial playback event.
    t0 = int(_time.time() * 1000)
    body = {"events": [{
        "videostatsPlaybackEntity": {
            "videoId": vid, "cpn": cpn, "mediaTime": "0",
            "ctime": str(t0), "state": 11,
            "referrer": referrer, "isHttps": True,
        }
    }]}
    print(f"[2/N] playbackEntity  cpn={cpn}")
    try:
        it._post("log_event", body)
        print("  ok")
    except Exception as e:
        print(f"  FAIL: {e}"); return 1

    # 3) Spaced watchtime pings — mimic a real TV streaming 60+ seconds.
    for n, ms in enumerate([10000, 20000, 35000, 50000, 70000], start=3):
        _time.sleep(8)  # wall-clock spacing between requests
        ctime_ms = int(_time.time() * 1000)
        body = {"events": [{
            "videostatsWatchtimeEntity": {
                "videoId": vid, "cpn": cpn,
                "mediaTime": str(ms),
                "totalElapsedMediaTime": f"{ms/1000:.1f}",
                "ctime": str(ctime_ms), "state": 11,
                "referrer": referrer, "isHttps": True,
            }
        }]}
        print(f"[{n}/N] watchtimeEntity  mediaTime={ms}ms")
        try:
            it._post("log_event", body)
            print("  ok")
        except Exception as e:
            print(f"  FAIL: {e}"); return 1

    # Final ended event.
    _time.sleep(2)
    ctime_ms = int(_time.time() * 1000)
    body = {"events": [{
        "videostatsWatchtimeEntity": {
            "videoId": vid, "cpn": cpn,
            "mediaTime": "75000",
            "totalElapsedMediaTime": "75.0",
            "ctime": str(ctime_ms), "state": 3,   # 3 = ended
            "referrer": referrer, "isHttps": True,
        }
    }]}
    print("[final] watchtimeEntity state=ended")
    try:
        it._post("log_event", body)
        print("  ok")
    except Exception as e:
        print(f"  FAIL: {e}")

    print()
    print(f"check  →  .venv/bin/python verify_history.py")
    print(f"      look for '{vid}' at the top")
    return 0


if __name__ == "__main__":
    sys.exit(main())
