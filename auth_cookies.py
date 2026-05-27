"""Cookie-backed YouTube session for stats/watchtime attribution.

Loads the Netscape-format cookies file extract_cookies.py wrote (from
the user's Firefox profile of a dedicated YT account), provides a
curl_cffi session with Firefox-like TLS fingerprint, computes
SAPISIDHASH per request (YT's standard cookie-auth scheme), and
persists Set-Cookie updates back to disk so SID rotations don't
invalidate our session over time.

Strict invariant: cookies are read-only from our perspective except for
the periodic refresh write-back. We send only stats pings — never write
actions (like, comment, subscribe). This keeps anti-abuse exposure
minimal and means even worst-case "you logged in from a new device"
challenges are recoverable with one click.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import os
import threading
import time
from pathlib import Path
from typing import Optional

# curl_cffi gives a real Firefox JA3 — same trick pr_fetch.py uses for
# /watch fetches. Without it our TLS would look like raw Python and YT
# could fingerprint us in a heartbeat. With it we're byte-for-byte
# identical to the real browser the cookies came from.
from curl_cffi import requests as ccffi

PROJECT_DIR = Path(__file__).resolve().parent
COOKIES_PATH = PROJECT_DIR / "cache" / "yt_cookies.txt"
ORIGIN = "https://www.youtube.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
      "Gecko/20100101 Firefox/128.0")
IMPERSONATE = "firefox133"

# Disable the whole cookie path via env if anything ever goes wrong —
# read by watchstats.py and any future caller.
def is_enabled() -> bool:
    return os.environ.get("WATCHSTATS_COOKIES", "1") == "1"


class CookieSession:
    """Thread-safe wrapper around a curl_cffi session with persistent
    cookie jar. Use get()/post() — they automatically merge Set-Cookie
    responses back into the on-disk jar so SID rotations stick."""

    def __init__(self, cookies_path: Path = COOKIES_PATH):
        self._path = cookies_path
        self._jar = http.cookiejar.MozillaCookieJar(str(cookies_path))
        self._jar.load(ignore_discard=True, ignore_expires=True)
        self._lock = threading.Lock()
        self._sapisid = self._extract_sapisid()
        # Single curl_cffi session with Firefox JA3. Cookies are passed
        # per-request via cookies= rather than seeded into the session
        # jar — some YT cookies (ST-*) have weird formats that confuse
        # curl_cffi's internal cookie handling.
        self._sess = ccffi.Session(
            impersonate=IMPERSONATE,
            timeout=20,
        )

    # Only the cookies that matter for auth + telemetry. Adding more
    # bloats the Cookie header past curl's limit (ST-* tracking cookies
    # are per-search noise, YT doesn't need them for stats attribution).
    _RELEVANT = frozenset({
        "SAPISID", "HSID", "SSID", "APISID", "SID",
        "__Secure-1PSID", "__Secure-3PSID",
        "__Secure-1PAPISID", "__Secure-3PAPISID",
        "__Secure-1PSIDTS", "__Secure-3PSIDTS",
        "__Secure-1PSIDCC", "__Secure-3PSIDCC",
        "LOGIN_INFO", "VISITOR_INFO1_LIVE", "VISITOR_PRIVACY_METADATA",
        "PREF", "NID", "YSC", "SIDCC",
        "__Secure-YNID", "__Secure-BUCKET", "__Secure-ROLLOUT_TOKEN",
    })

    def _jar_as_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for c in self._jar:
            d = c.domain or ""
            if c.name in self._RELEVANT and ("youtube" in d or "google" in d):
                out[c.name] = c.value
        return out

    @property
    def signed_in(self) -> bool:
        return self._sapisid is not None

    def _extract_sapisid(self) -> Optional[str]:
        for name in ("__Secure-3PAPISID", "SAPISID"):
            for c in self._jar:
                if c.name == name and ".youtube.com" in (c.domain or ""):
                    return c.value
        return None

    def sapisid_hash(self, origin: str = ORIGIN) -> str:
        """YT's cookie-auth header. Format:
            SAPISIDHASH <unix_ts>_<sha1(unix_ts + " " + SAPISID + " " + origin)>
        Fresh timestamp per request — YT rejects stale ones."""
        if not self._sapisid:
            return ""
        ts = int(time.time())
        sha1 = hashlib.sha1(
            f"{ts} {self._sapisid} {origin}".encode()).hexdigest()
        return f"SAPISIDHASH {ts}_{sha1}"

    def base_headers(self, *, referer: Optional[str] = None) -> dict:
        h = {
            "User-Agent": UA,
            "X-Origin": ORIGIN,
            "Origin": ORIGIN,
            "Authorization": self.sapisid_hash(),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
        }
        if referer:
            h["Referer"] = referer
        return h

    def get(self, url: str, *, headers: Optional[dict] = None,
            timeout: float = 15.0):
        h = self.base_headers()
        if headers:
            h.update(headers)
        cookies = self._jar_as_dict()
        with self._lock:
            r = self._sess.get(url, headers=h, cookies=cookies,
                               timeout=timeout, allow_redirects=False)
        self._absorb_set_cookies(r)
        return r

    def _absorb_set_cookies(self, r) -> None:
        """If the response set new cookies (e.g. rotated SID), persist
        them to disk so the next run uses the fresh values."""
        try:
            sc = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else None
            if sc is None:
                sc = r.headers.get("set-cookie")
                if isinstance(sc, str):
                    sc = [sc]
                elif sc is None:
                    sc = []
            if not sc:
                return
            changed = False
            for line in sc:
                # Parse "name=value; expires=...; path=/; domain=.youtube.com; ..."
                first, *attrs = line.split(";")
                if "=" not in first:
                    continue
                name, value = first.split("=", 1)
                name = name.strip()
                value = value.strip()
                if not name or not value:
                    continue
                domain = ".youtube.com"
                path = "/"
                for a in attrs:
                    a = a.strip()
                    if a.lower().startswith("domain="):
                        domain = a.split("=", 1)[1].strip()
                    elif a.lower().startswith("path="):
                        path = a.split("=", 1)[1].strip()
                cur = self._find(name, domain)
                if cur is None or cur.value != value:
                    c = http.cookiejar.Cookie(
                        version=0, name=name, value=value, port=None,
                        port_specified=False, domain=domain,
                        domain_specified=domain.startswith("."),
                        domain_initial_dot=domain.startswith("."),
                        path=path, path_specified=True,
                        secure=True, expires=None,
                        discard=False, comment=None, comment_url=None,
                        rest={}, rfc2109=False)
                    self._jar.set_cookie(c)
                    changed = True
            if changed:
                with self._lock:
                    self._jar.save(ignore_discard=True, ignore_expires=True)
                # Re-extract SAPISID if it rotated.
                self._sapisid = self._extract_sapisid()
        except Exception:
            # Cookie write-back is best-effort; never break the request
            # path because of disk issues.
            pass

    def _find(self, name: str, domain: str) -> Optional[http.cookiejar.Cookie]:
        for c in self._jar:
            if c.name == name and (c.domain or "") == (domain or ""):
                return c
        return None


# Module-level singleton — created on first access, cheap to share.
_SESSION: Optional[CookieSession] = None
_INIT_LOCK = threading.Lock()


def get_session() -> Optional[CookieSession]:
    """Lazy singleton. Returns None if cookies file is missing or has
    no SAPISID (i.e. user hasn't done extract_cookies.py yet, or the
    Firefox session wasn't signed in)."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    with _INIT_LOCK:
        if _SESSION is not None:
            return _SESSION
        if not COOKIES_PATH.exists():
            return None
        try:
            s = CookieSession()
        except Exception:
            return None
        if not s.signed_in:
            return None
        _SESSION = s
        return _SESSION
