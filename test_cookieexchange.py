#!/usr/bin/env python3.11
"""Probe: convert our OAuth bearer into Google session cookies via
accounts.google.com OAuthLogin + MergeSession flow (the path hangups
used). If this returns SAPISID/HSID/SSID we have what YT needs for
history attribution.

Usage:
    python3.11 test_cookieexchange.py
"""
from __future__ import annotations

import sys

import httpx

from youthub import auth


def main() -> int:
    tokens = auth.get_tokens()
    if tokens is None:
        print("no OAuth tokens", file=sys.stderr); return 1
    if not tokens.is_fresh():
        tokens = auth.refresh(tokens)

    with httpx.Client(timeout=20.0, follow_redirects=False) as s:
        # ---- step 1: get uberauth ----
        r = s.get(
            "https://accounts.google.com/accounts/OAuthLogin",
            params={"source": "youtube", "issueuberauth": "1"},
            headers={"Authorization": f"Bearer {tokens.access_token}"},
        )
        print(f"[1/2] OAuthLogin  status={r.status_code}  body_len={len(r.text)}")
        if r.status_code != 200:
            print(f"  body: {r.text[:300]}")
            return 1
        uberauth = r.text.strip()
        print(f"  uberauth: {uberauth[:60]}...{'...' if len(uberauth) > 60 else ''}  (len={len(uberauth)})")

        # ---- step 2: merge session, collect cookies ----
        r = s.get(
            "https://accounts.google.com/MergeSession",
            params={
                "service": "youtube",
                "continue": "https://www.youtube.com",
                "uberauth": uberauth,
            },
            headers={"Authorization": f"Bearer {tokens.access_token}"},
        )
        print(f"[2/2] MergeSession  status={r.status_code}")
        # Follow redirects manually to walk through the cookie-setting chain
        for _ in range(8):
            if r.status_code not in (301, 302, 303, 307):
                break
            nxt = r.headers.get("location")
            if not nxt:
                break
            if nxt.startswith("/"):
                nxt = "https://accounts.google.com" + nxt
            print(f"  redirect → {nxt[:100]}")
            r = s.get(nxt, headers={"Authorization": f"Bearer {tokens.access_token}"})

        # ---- show the cookies we got ----
        cookies_g = {c.name: c.value for c in s.cookies.jar if ".google.com" in (c.domain or "")}
        cookies_y = {c.name: c.value for c in s.cookies.jar if "youtube" in (c.domain or "")}
        all_jar = {c.name: f"{c.value[:20]}... (domain={c.domain})" for c in s.cookies.jar}

        print()
        print(f"  cookies on .google.com: {list(cookies_g.keys())}")
        print(f"  cookies on .youtube.com: {list(cookies_y.keys())}")
        print(f"  total in jar: {len(all_jar)}")
        for name, v in list(all_jar.items())[:15]:
            print(f"    {name}: {v}")

        # Critical cookies for SAPISID-Hash auth
        critical = ["SAPISID", "HSID", "SSID", "APISID", "SID", "LOGIN_INFO", "__Secure-3PSID", "__Secure-3PAPISID"]
        got = [c for c in critical if any(j.name == c for j in s.cookies.jar)]
        missing = [c for c in critical if c not in got]
        print()
        print(f"  CRITICAL cookies present: {got}")
        print(f"  missing: {missing}")
        if "SAPISID" in got or "__Secure-3PAPISID" in got:
            print("  ✓ we have SAPISID — can compute SAPISIDHASH for authenticated requests")
        else:
            print("  ✗ no SAPISID — cannot compute SAPISIDHASH")

    return 0


if __name__ == "__main__":
    sys.exit(main())
