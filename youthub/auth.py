"""OAuth 2.0 Device Authorization Grant for YouTube (TV pairing flow).

The "go to youtube.com/activate and enter XXXX-XXXX" path. After one
pairing we keep a long-lived refresh_token in cache/oauth.json; all
InnerTube requests then carry `Authorization: Bearer <access_token>`,
and YouTube treats us as the signed-in account.

The client_id below is the long-standing TV/Kodi pair embedded in
Kodi-youtube, SmartTubeNext, NewPipe-Legacy and friends. Replace with
your own from Google Cloud Console ("TVs and Limited Input devices")
if you prefer your own.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

CLIENT_ID = "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"
SCOPE = "http://gdata.youtube.com https://www.googleapis.com/auth/youtube"

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"

TOKEN_FILE = Path(__file__).resolve().parent.parent / "cache" / "oauth.json"


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float

    def is_fresh(self, leeway: int = 60) -> bool:
        return time.time() + leeway < self.expires_at

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Tokens":
        return cls(
            access_token=d["access_token"],
            refresh_token=d["refresh_token"],
            expires_at=float(d["expires_at"]),
        )


def _save(tokens: Tokens) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens.to_dict(), indent=2))
    TOKEN_FILE.chmod(0o600)


def _load() -> Optional[Tokens]:
    if not TOKEN_FILE.exists():
        return None
    try:
        return Tokens.from_dict(json.loads(TOKEN_FILE.read_text()))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _client() -> httpx.Client:
    # Proxy comes from HTTPS_PROXY env automatically.
    return httpx.Client(http2=True, timeout=20.0)


def _request_device_code(client: httpx.Client) -> dict:
    r = client.post(DEVICE_CODE_URL, data={
        "client_id": CLIENT_ID,
        "scope": SCOPE,
    })
    r.raise_for_status()
    return r.json()


def _poll_token(client: httpx.Client, device_code: str, interval: int) -> dict:
    while True:
        time.sleep(interval)
        r = client.post(TOKEN_URL, data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": device_code,
            "grant_type": "http://oauth.net/grant_type/device/1.0",
        })
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Non-JSON token response: {r.status_code} {r.text!r}")
        if r.status_code == 200 and "access_token" in data:
            return data
        err = data.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err in ("access_denied", "expired_token"):
            raise RuntimeError(f"OAuth aborted by Google: {err}")
        raise RuntimeError(f"Unexpected OAuth response ({r.status_code}): {data!r}")


def login_interactive() -> Tokens:
    """Show the PIN code and block until user authorizes (or Ctrl-C)."""
    with _client() as client:
        d = _request_device_code(client)
    user_code = d["user_code"]
    url = d.get("verification_url") or d.get("verification_uri") or "https://www.google.com/device"
    interval = int(d.get("interval", 5))
    device_code = d["device_code"]

    print()
    print("  +-- YouTube TV pairing " + "-" * 38)
    print(f"  |  Open:  {url}")
    print(f"  |  Code:  {user_code}")
    print(f"  |  Waiting for activation... (Ctrl-C to cancel)")
    print("  +" + "-" * 60)
    print()
    sys.stdout.flush()

    with _client() as client:
        td = _poll_token(client, device_code, interval)

    tokens = Tokens(
        access_token=td["access_token"],
        refresh_token=td["refresh_token"],
        expires_at=time.time() + int(td.get("expires_in", 3600)),
    )
    _save(tokens)
    print("  [auth] paired successfully — refresh_token cached")
    return tokens


def refresh(tokens: Tokens) -> Tokens:
    with _client() as client:
        r = client.post(TOKEN_URL, data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": tokens.refresh_token,
            "grant_type": "refresh_token",
        })
        r.raise_for_status()
        data = r.json()
    new = Tokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", tokens.refresh_token),
        expires_at=time.time() + int(data.get("expires_in", 3600)),
    )
    _save(new)
    return new


def get_tokens() -> Tokens:
    """Return valid tokens; auto-refresh or trigger pairing as needed."""
    cached = _load()
    if cached is None:
        return login_interactive()
    if cached.is_fresh():
        return cached
    try:
        return refresh(cached)
    except httpx.HTTPError:
        return login_interactive()


def logout() -> None:
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()


if __name__ == "__main__":
    t = get_tokens()
    print(f"OK — access_token expires in {int(t.expires_at - time.time())}s")
