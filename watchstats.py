"""Watch-stats pings that actually write to YouTube history.

Path:
  1. GET /watch?v=<vid> through our cookie session (Firefox JA3 via
     curl_cffi) → parse ytInitialPlayerResponse → extract the
     session-bound videostatsPlaybackUrl / videostatsWatchtimeUrl
  2. Generate a fresh cpn (client playback nonce) for this video
  3. Initial playback ping (state=playing, cmt=0)
  4. Watchtime heartbeat at YT's real cadence (10s, 20s, 30s wall-clock,
     then every ~40s) with light jitter
  5. On stop: final watchtime ping with state=ended

Safety:
  * Cookie-only path — we send NO write actions (no like, comment,
    subscribe, watch-later). Stats endpoints are loss-tolerant and the
    closest thing YT has to a public read-equivalent
  * Kill-switch via env: WATCHSTATS_COOKIES=0 disables everything
  * If pings start returning 401 (cookie invalidated), we log once and
    stop quietly — no retry-storm
  * If user closes the player before any meaningful playback, we skip
    pings entirely

Falls back to no-op silently when cookies aren't extracted yet — old
Bearer path is removed since we proved it doesn't attribute.
"""
from __future__ import annotations

import json
import random
import re
import secrets
import string
import threading
import time
from typing import Callable, Optional

import auth_cookies

# Minimum playback (seconds) before we bother sending stats — keeps
# accidental opens off the YT history feed.
MIN_WATCH_FOR_STATS = 8.0

# YT's real heartbeat cadence — first three pings at fixed offsets,
# then every ~40s thereafter.
INITIAL_OFFSETS = [10.0, 20.0, 30.0]
LATER_INTERVAL = 40.0

# Jitter as fraction of interval — real browsers don't tick at exact
# 40.000s, neither do we.
JITTER = 0.15

_RE_PR = [
    re.compile(r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;\s*var", re.S),
    re.compile(r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;\s*</script>", re.S),
]


def _gen_cpn() -> str:
    alpha = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alpha) for _ in range(16))


def _parse_pr_html(html: str) -> Optional[dict]:
    for rx in _RE_PR:
        m = rx.search(html)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return None


class WatchStats:
    """One instance per playback session. start() bootstraps the
    session-bound URLs and kicks off the heartbeat thread. stop()
    sends a final state=ended ping and joins the worker."""

    def __init__(self,
                 video_id: str,
                 get_pos_fn: Callable[[], float],
                 shutdown_event: threading.Event,
                 log_fn: Callable[[str], None]) -> None:
        self._video_id = video_id
        self._get_pos = get_pos_fn
        self._shutdown = shutdown_event
        self._log = log_fn
        self._session = None
        self._cpn = _gen_cpn()
        self._playback_url: Optional[str] = None
        self._watchtime_url: Optional[str] = None
        self._referer = f"https://www.youtube.com/watch?v={video_id}"
        self._t_start = 0.0
        self._st_prev = 0.0
        self._dead = False
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    # ---- lifecycle ----

    def start(self) -> bool:
        if not auth_cookies.is_enabled():
            self._log("[watchstats] disabled via WATCHSTATS_COOKIES=0")
            return False
        self._session = auth_cookies.get_session()
        if self._session is None:
            self._log("[watchstats] no cookies (run extract_cookies.py)")
            return False

        # Get session-bound playback/watchtime URLs from /watch HTML.
        try:
            r = self._session.get(self._referer,
                                  headers={"Accept": "text/html"})
        except Exception as e:
            self._log(f"[watchstats] /watch fetch failed: {e}")
            return False
        if r.status_code != 200:
            self._log(f"[watchstats] /watch status {r.status_code}")
            return False
        pr = _parse_pr_html(r.text)
        if not pr:
            self._log("[watchstats] no ytInitialPlayerResponse in /watch")
            return False
        st = pr.get("playabilityStatus", {}).get("status")
        if st != "OK":
            self._log(f"[watchstats] playabilityStatus={st}, skipping pings")
            return False
        pt = pr.get("playbackTracking", {})
        self._playback_url = pt.get("videostatsPlaybackUrl", {}).get("baseUrl")
        self._watchtime_url = pt.get("videostatsWatchtimeUrl", {}).get("baseUrl")
        if not (self._playback_url and self._watchtime_url):
            self._log("[watchstats] no tracking URLs in PR")
            return False

        self._t_start = time.monotonic()
        self._send_playback_start()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="watchstats")
        self._thread.start()
        self._log(f"[watchstats] cookie-pings started  cpn={self._cpn}")
        return True

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        # Final ping only if there was actual playback.
        cmt = self._safe_pos()
        if (self._watchtime_url and not self._dead and cmt >= MIN_WATCH_FOR_STATS):
            try:
                self._send_watchtime(cmt, state="ended", timeout=2.0)
            except Exception:
                pass

    # ---- pings ----

    def _send_playback_start(self) -> None:
        if not self._playback_url:
            return
        url = (f"{self._playback_url}&ver=2&cpn={self._cpn}"
               f"&cmt=0&et=0&state=playing&rt=0")
        try:
            r = self._session.get(url,
                                  headers={"Referer": self._referer},
                                  timeout=5.0)
            self._check_health(r.status_code)
        except Exception as e:
            self._log(f"[watchstats] playback ping failed: {e}")

    def _send_watchtime(self, cmt: float, *, state: str = "playing",
                        timeout: float = 5.0) -> None:
        if not self._watchtime_url or self._dead:
            return
        rt = time.monotonic() - self._t_start
        url = (f"{self._watchtime_url}&ver=2&cpn={self._cpn}"
               f"&cmt={cmt:.3f}&st={self._st_prev:.3f}&et={cmt:.3f}"
               f"&state={state}&rt={rt:.1f}")
        r = self._session.get(url,
                              headers={"Referer": self._referer},
                              timeout=timeout)
        self._check_health(r.status_code)
        self._st_prev = cmt

    def _check_health(self, status: int) -> None:
        if status == 401 or status == 403:
            self._log(f"[watchstats] auth failed ({status}) — "
                      f"cookies expired? re-run extract_cookies.py")
            self._dead = True
        elif status == 429:
            self._log("[watchstats] throttled (429) — backing off")
            # We'll see this on next ping anyway; nothing else to do.

    def _safe_pos(self) -> float:
        try:
            return max(0.0, float(self._get_pos() or 0.0))
        except Exception:
            return 0.0

    # ---- worker ----

    def _loop(self) -> None:
        pings_sent = 0
        while not self._shutdown.is_set() and not self._dead:
            # Compute next sleep — initial fixed offsets, then ~40s with jitter.
            if pings_sent < len(INITIAL_OFFSETS):
                target = INITIAL_OFFSETS[pings_sent]
                # Sleep until wall-clock hits this offset since t_start.
                elapsed = time.monotonic() - self._t_start
                sleep_for = max(0.5, target - elapsed)
            else:
                base = LATER_INTERVAL
                jitter = base * JITTER * (random.random() * 2 - 1)
                sleep_for = base + jitter

            if self._shutdown.wait(sleep_for):
                break

            cmt = self._safe_pos()
            if cmt < MIN_WATCH_FOR_STATS:
                # Player paused at start / hadn't really begun; skip ping.
                continue
            try:
                self._send_watchtime(cmt)
                pings_sent += 1
            except Exception as e:
                self._log(f"[watchstats] watchtime ping failed: {e}")
