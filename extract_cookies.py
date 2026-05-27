#!/usr/bin/env python3.11
"""Extract YouTube cookies from Firefox into Netscape format.

Reads Firefox's cookies.sqlite directly via browser-cookie3 (handles
profile autodetection + file locking). Writes to cache/yt_cookies.txt
in the standard Netscape cookies format — the same yt-dlp's
--cookies file expects.

Usage:
    python3.11 extract_cookies.py            # default Firefox profile
    python3.11 extract_cookies.py --chrome   # extract from Chrome instead
"""
from __future__ import annotations

import sys
from pathlib import Path

import browser_cookie3

OUT = Path(__file__).resolve().parent / "cache" / "yt_cookies.txt"

# What we need for authed YT requests
CRITICAL = {"SAPISID", "HSID", "SSID", "APISID", "SID", "LOGIN_INFO",
            "__Secure-1PSID", "__Secure-3PSID",
            "__Secure-1PAPISID", "__Secure-3PAPISID",
            "__Secure-1PSIDTS", "__Secure-3PSIDTS",
            "__Secure-1PSIDCC", "__Secure-3PSIDCC"}


def main() -> int:
    use_chrome = "--chrome" in sys.argv
    use_firefox = not use_chrome

    try:
        if use_firefox:
            jar = browser_cookie3.firefox(domain_name=".youtube.com")
        else:
            jar = browser_cookie3.chrome(domain_name=".youtube.com")
    except Exception as e:
        print(f"failed to read cookies: {e}", file=sys.stderr)
        return 1

    cookies = list(jar)
    if not cookies:
        print("no .youtube.com cookies found — did you log in to youtube.com?",
              file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write("# extracted from browser via extract_cookies.py\n\n")
        for c in cookies:
            domain = c.domain or ""
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.path or "/"
            secure = "TRUE" if c.secure else "FALSE"
            expires = int(c.expires) if c.expires else 0
            name = c.name or ""
            value = c.value or ""
            f.write("\t".join([domain, include_sub, path, secure,
                               str(expires), name, value]) + "\n")
    OUT.chmod(0o600)

    names = sorted({c.name for c in cookies})
    have_critical = sorted(set(names) & CRITICAL)
    missing_critical = sorted(CRITICAL - set(names))

    print(f"wrote {len(cookies)} cookies → {OUT}")
    print(f"  perms: 600 (only you can read)")
    print()
    print(f"  cookies present: {names}")
    print()
    print(f"  CRITICAL for auth ({len(have_critical)}/{len(CRITICAL)}): {have_critical}")
    if missing_critical:
        print(f"  MISSING: {missing_critical}")
        print()
        print("  If SAPISID/SID/HSID missing — you're not signed in to youtube.com")
        print("  in this browser, OR using an incognito profile.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
