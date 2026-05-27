#!/usr/bin/env python3.11
"""Pair via OAuth Authorization Code (loopback) using the SECOND
client_id from youtube.com/tv ($a in their JS):

    client_id    = 861556708454-912i5jlic99ecvu3ro5kqirg0hldli5t
    client_secret = ju2WuMJMOjilz_h_1dPgFdeU

Opens a local server on 127.0.0.1:8765 to catch Google's redirect.

Touch nothing in existing cache/oauth.json — saves to oauth_tv.json.

Usage:
    python3.11 test_tvpair.py             # pair (prints URL to open)
    python3.11 test_tvpair.py --check     # try OAuthLogin with cached tokens
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import httpx

# Google Hangouts first-party client. Hangups (the Python lib) has been
# using this for ~10 years. Known to support oob redirect AND OAuthLogin
# cookie-exchange. Scope can be minimal — the cookies we extract via
# OAuthLogin → MergeSession are account-level (SAPISID, HSID, SSID...) and
# work for ALL Google services including YouTube, regardless of the OAuth
# scope this was minted with.
CLIENT_ID = "936475272427.apps.googleusercontent.com"
CLIENT_SECRET = "KWsJlkaMn1jGLxQpWxMnOox-"
SCOPE = "https://www.google.com/accounts/OAuthLogin email profile"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

REDIRECT_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}"
REDIRECT_OOB = "urn:ietf:wg:oauth:2.0:oob"
CACHE = Path(__file__).resolve().parent / "cache" / "oauth_tv.json"


_received_code: str | None = None
_received_error: str | None = None
_expected_state: str = ""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _received_code, _received_error
        u = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(u.query)
        if params.get("state", [""])[0] != _expected_state:
            self._reply(400, "state mismatch")
            return
        if "code" in params:
            _received_code = params["code"][0]
            self._reply(200, "OK — token received. You can close this tab.")
        else:
            _received_error = params.get("error", ["unknown"])[0]
            self._reply(400, f"error: {_received_error}")

    def _reply(self, code: int, body: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body><h2>{body}</h2></body></html>".encode())

    def log_message(self, *args, **kwargs):
        pass


def pair() -> int:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_OOB,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    })

    print()
    print("Open this URL in any browser (one-time consent):")
    print()
    print(f"  {auth_url}")
    print()
    print("After clicking 'Allow', Google will show an auth code on screen.")
    print("Copy and paste it below.")
    print()
    code = input("Paste code: ").strip()
    if not code:
        print("empty code"); return 1

    r = httpx.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_OOB,
    }, timeout=15)
    if r.status_code != 200:
        print(f"token exchange failed: {r.status_code}  {r.text}")
        return 1
    tok = r.json()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": int(time.time() + tok.get("expires_in", 3600)),
        "client_id": CLIENT_ID,
    }, indent=2))
    print(f"saved → {CACHE}")
    return check()


def check() -> int:
    if not CACHE.exists():
        print(f"no cached tokens at {CACHE} — run without --check first")
        return 1
    d = json.loads(CACHE.read_text())
    access = d["access_token"]
    print(f"access_token: {access[:30]}...")

    r = httpx.get(
        "https://accounts.google.com/accounts/OAuthLogin",
        params={"source": "youtube", "issueuberauth": "1"},
        headers={"Authorization": f"Bearer {access}"},
        timeout=15,
    )
    print(f"OAuthLogin status: {r.status_code}")
    print(f"body ({len(r.text)} chars): {r.text[:200]}")
    if r.status_code == 200 and len(r.text) > 30 and "Error" not in r.text:
        print()
        print("✓ uberauth received — this client_id is WHITELISTED!")
        return 0
    print()
    print("✗ still badauth — would need real TV reverse")
    return 1


def main() -> int:
    if "--check" in sys.argv:
        return check()
    return pair()


if __name__ == "__main__":
    sys.exit(main())
