#!/usr/bin/env python3.11
"""End-to-end cookie-auth watchtime test.

  1. Load cookies from cache/yt_cookies.txt
  2. GET /watch?v=VID with cookies → parse ytInitialPlayerResponse → grab
     videostatsPlaybackUrl + videostatsWatchtimeUrl (these are bound to
     OUR cookie session, so YT will attribute pings to our account)
  3. SAPISIDHASH-authed sequence: playback start + 5 watchtime pings
     spaced 10s apart (real-watch heartbeat pattern)
  4. User runs verify_history.py to confirm attribution.

Usage:  python3.11 test_cookieping.py <video_id>
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import re
import secrets
import string
import sys
import time
from pathlib import Path

import httpx


def gen_cpn() -> str:
    """16-char client playback nonce — same alphabet YT web JS uses."""
    alpha = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alpha) for _ in range(16))

COOKIES_PATH = Path(__file__).resolve().parent / "cache" / "yt_cookies.txt"
ORIGIN = "https://www.youtube.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
      "Gecko/20100101 Firefox/128.0")

_RE_PR = [
    re.compile(r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;\s*var", re.S),
    re.compile(r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;\s*</script>", re.S),
]


def load_jar():
    jar = http.cookiejar.MozillaCookieJar(str(COOKIES_PATH))
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def sapisid_hash(sapisid: str) -> str:
    ts = int(time.time())
    sha1 = hashlib.sha1(f"{ts} {sapisid} {ORIGIN}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{sha1}"


def get_sapisid(jar) -> str | None:
    for name in ("__Secure-3PAPISID", "SAPISID"):
        for c in jar:
            if c.name == name and ".youtube.com" in (c.domain or ""):
                return c.value
    return None


def fetch_pr(s, vid: str) -> dict | None:
    r = s.get(f"{ORIGIN}/watch?v={vid}",
              headers={"User-Agent": UA, "Accept": "text/html"})
    if r.status_code != 200:
        print(f"  /watch status: {r.status_code}")
        return None
    for rx in _RE_PR:
        m = rx.search(r.text)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: test_cookieping.py <video_id>", file=sys.stderr)
        return 2
    vid = sys.argv[1]

    jar = load_jar()
    sapisid = get_sapisid(jar)
    if not sapisid:
        print("no SAPISID in jar", file=sys.stderr); return 1

    with httpx.Client(cookies=jar, http2=True, timeout=20.0,
                      follow_redirects=False) as s:
        print(f"[1] GET /watch?v={vid} for session-bound tracking URLs")
        pr = fetch_pr(s, vid)
        if not pr:
            print("  failed to parse ytInitialPlayerResponse"); return 1
        st = pr.get("playabilityStatus", {}).get("status")
        print(f"  playabilityStatus: {st}")
        if st != "OK":
            print(f"  reason: {pr.get('playabilityStatus', {}).get('reason')}")
            return 1
        pt = pr.get("playbackTracking", {})
        pb_url = pt.get("videostatsPlaybackUrl", {}).get("baseUrl")
        wt_url = pt.get("videostatsWatchtimeUrl", {}).get("baseUrl")
        if not (pb_url and wt_url):
            print("  no playback/watchtime URLs"); return 1
        print(f"  got watchtime URL ({len(wt_url)} chars)")

        ref = f"{ORIGIN}/watch?v={vid}"
        cpn = gen_cpn()
        print(f"  cpn: {cpn}")
        ping_headers = {
            "User-Agent": UA,
            "X-Origin": ORIGIN,
            "Origin": ORIGIN,
            "Referer": ref,
            "Accept": "*/*",
        }

        # ---- 2) initial playback ping ----
        print("[2] playback ping (state=playing, cmt=0)")
        url = (f"{pb_url}&ver=2&cpn={cpn}&cmt=0&et=0&state=playing"
               f"&rt=0&fmt=243&afmt=251")
        r = s.get(url, headers={**ping_headers,
                                "Authorization": sapisid_hash(sapisid)})
        print(f"  status: {r.status_code}")

        # ---- 3) watchtime heartbeat: real-watch cadence ----
        # Real YT web sends pings at 10s, 20s, 30s wall-clock then every 40s,
        # carrying the SAME cpn the whole time. We send 10 over ~5 min.
        print("[3] watchtime heartbeat (10 pings over ~5 min, same cpn)")
        st_prev = 0.0
        deltas = [10.0, 20.0, 30.0, 60.0, 90.0, 120.0, 180.0, 240.0, 280.0, 280.0]
        for n, sec in enumerate(deltas, 1):
            time.sleep(20 if n > 5 else 10)
            url = (f"{wt_url}&ver=2&cpn={cpn}&cmt={sec:.3f}&st={st_prev:.3f}"
                   f"&et={sec:.3f}&state=playing&rt={int(time.monotonic())}"
                   f"&fmt=243&afmt=251")
            r = s.get(url, headers={**ping_headers,
                                    "Authorization": sapisid_hash(sapisid)})
            print(f"  ping {n}/{len(deltas)}: status={r.status_code}  cmt={sec}")
            st_prev = sec

    print()
    print("Done. Wait ~15s, then:")
    print(f"  .venv/bin/python verify_history.py")
    print(f"  '{vid}' should be at the top")
    return 0


if __name__ == "__main__":
    sys.exit(main())
