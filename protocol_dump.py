#!/usr/bin/env python3.11
"""Reverse-engineering aid: capture full SABR videoplayback exchange via CDP.

Opens the user-specified YouTube video in a real Chromium (via Playwright +
CDP), waits for the page to actually play a few seconds, and dumps:
  - For each `googlevideo.com/videoplayback` request: URL, request headers,
    raw POST body bytes (binary protobuf for SABR is fine).
  - For each response: status, headers, full body bytes.

Output goes to `dump/<timestamp>/...` so we can analyze offline.

Usage: protocol_dump.py [<video_url>]
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

PROJECT_DIR = Path(__file__).resolve().parent
DUMP_ROOT = PROJECT_DIR / "dump"
DUMP_ROOT.mkdir(exist_ok=True)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


def resolve_proxy() -> str:
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    raise RuntimeError("Set HTTPS_PROXY env var (e.g. socks5://127.0.0.1:1080)")


async def run(video_url: str) -> int:
    proxy = resolve_proxy()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_dir = DUMP_ROOT / stamp
    dump_dir.mkdir(parents=True, exist_ok=True)
    print(f"[dump] writing to {dump_dir}", file=sys.stderr)
    print(f"[dump] proxy: {proxy}", file=sys.stderr)
    print(f"[dump] url:   {video_url}", file=sys.stderr)

    # Tracks per-CDP-requestId: metadata accumulator
    rec: dict[str, dict] = {}
    response_bodies_to_fetch: list[str] = []
    request_post_to_fetch: dict[str, str] = {}  # cdp_request_id -> path

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            proxy={"server": proxy},
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        context = await browser.new_context(user_agent=UA)
        await context.add_cookies(
            [
                {"name": "CONSENT", "value": "YES+", "domain": ".youtube.com", "path": "/"},
                {"name": "SOCS", "value": "CAI", "domain": ".youtube.com", "path": "/"},
            ]
        )
        page = await context.new_page()
        cdp = await context.new_cdp_session(page)

        await cdp.send("Network.enable", {"maxTotalBufferSize": 200_000_000, "maxResourceBufferSize": 100_000_000})

        def is_target(url: str) -> bool:
            return "googlevideo.com/videoplayback" in url

        def on_will_be_sent(ev):
            req = ev.get("request") or {}
            url = req.get("url", "")
            if not is_target(url):
                return
            rid = ev["requestId"]
            entry = rec.setdefault(rid, {})
            entry.update(
                {
                    "request": {
                        "url": url,
                        "method": req.get("method"),
                        "headers": req.get("headers"),
                        "has_post_data": req.get("hasPostData", False),
                        "post_data_entries_meta": req.get("postDataEntries"),
                    },
                    "type": ev.get("type"),
                    "frame_id": ev.get("frameId"),
                }
            )
            print(f"[dump] requestWillBeSent rid={rid} url={url[:90]}...", file=sys.stderr)

        def on_extra_request(ev):
            rid = ev["requestId"]
            if rid not in rec:
                return
            rec[rid].setdefault("request_extra", []).append(ev.get("headers"))

        def on_response(ev):
            rid = ev["requestId"]
            if rid not in rec:
                return
            r = ev.get("response", {})
            rec[rid]["response"] = {
                "status": r.get("status"),
                "status_text": r.get("statusText"),
                "url": r.get("url"),
                "headers": r.get("headers"),
                "mime_type": r.get("mimeType"),
            }
            print(f"[dump] responseReceived rid={rid} status={r.get('status')}", file=sys.stderr)

        def on_data_received(ev):
            rid = ev["requestId"]
            if rid not in rec:
                return
            rec[rid].setdefault("data_lengths", []).append(ev.get("dataLength", 0))

        def on_finished(ev):
            rid = ev["requestId"]
            if rid not in rec:
                return
            rec[rid]["finished"] = True
            rec[rid]["encoded_data_length"] = ev.get("encodedDataLength")
            response_bodies_to_fetch.append(rid)
            print(f"[dump] loadingFinished rid={rid}", file=sys.stderr)

        cdp.on("Network.requestWillBeSent", on_will_be_sent)
        cdp.on("Network.requestWillBeSentExtraInfo", on_extra_request)
        cdp.on("Network.responseReceived", on_response)
        cdp.on("Network.dataReceived", on_data_received)
        cdp.on("Network.loadingFinished", on_finished)

        try:
            await page.goto(video_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"[dump] page.goto failed: {e}", file=sys.stderr)

        # Trigger playback
        try:
            await page.evaluate(
                "() => { const v = document.querySelector('video'); "
                "if (v) { v.muted = true; v.play().catch(()=>{}); } }"
            )
        except Exception:
            pass

        # Wait some real playback time so we capture multiple SABR exchanges
        print("[dump] watching for 25 seconds...", file=sys.stderr)
        await asyncio.sleep(25)

        # Now fetch post data + response bodies via CDP
        print(f"[dump] fetching {len(rec)} captured exchanges...", file=sys.stderr)
        for rid, entry in rec.items():
            req = entry.get("request") or {}
            if req.get("has_post_data"):
                try:
                    res = await cdp.send("Network.getRequestPostData", {"requestId": rid})
                    body_b64 = res.get("postData", "")
                    if body_b64:
                        body_bytes = body_b64.encode("latin-1") if isinstance(body_b64, str) else body_b64
                        # CDP returns it as string; might be base64 or raw
                        try:
                            decoded = base64.b64decode(body_b64, validate=True)
                            # Use base64 only if not direct string
                            body_bytes = decoded
                        except Exception:
                            body_bytes = body_b64.encode("latin-1")
                        path = dump_dir / f"req_{rid}.bin"
                        path.write_bytes(body_bytes)
                        entry["request_body_file"] = path.name
                        entry["request_body_size"] = len(body_bytes)
                        entry["request_body_hex_preview"] = body_bytes[:64].hex()
                        print(f"[dump]   rid={rid} wrote req body {len(body_bytes)}B", file=sys.stderr)
                except Exception as e:
                    entry["request_body_error"] = str(e)
                    print(f"[dump]   rid={rid} req body err: {e}", file=sys.stderr)
            if entry.get("response", {}).get("status") and entry.get("finished"):
                try:
                    res = await cdp.send("Network.getResponseBody", {"requestId": rid})
                    body = res.get("body") or ""
                    if res.get("base64Encoded"):
                        body_bytes = base64.b64decode(body)
                    else:
                        body_bytes = body.encode("utf-8")
                    path = dump_dir / f"resp_{rid}.bin"
                    path.write_bytes(body_bytes)
                    entry["response_body_file"] = path.name
                    entry["response_body_size"] = len(body_bytes)
                    entry["response_body_hex_preview"] = body_bytes[:64].hex()
                    print(f"[dump]   rid={rid} wrote resp body {len(body_bytes)}B", file=sys.stderr)
                except Exception as e:
                    entry["response_body_error"] = str(e)
                    print(f"[dump]   rid={rid} resp body err: {e}", file=sys.stderr)

        # Also capture cookies + visitor_data for analysis
        cookies = await context.cookies()
        try:
            vdata = await page.evaluate(
                "() => (window.ytcfg && (ytcfg.data_?.VISITOR_DATA || ytcfg.get?.('VISITOR_DATA'))) || null"
            )
        except Exception:
            vdata = None

        (dump_dir / "cookies.json").write_text(json.dumps(cookies, indent=2))
        (dump_dir / "visitor_data.txt").write_text(vdata or "")
        (dump_dir / "exchanges.json").write_text(json.dumps(rec, indent=2, default=str))

        await browser.close()

    print(f"[dump] done. {len(rec)} exchanges in {dump_dir}", file=sys.stderr)
    print(f"[dump] inspect: ls {dump_dir}", file=sys.stderr)
    return 0


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=TjJ5X6l2OiQ"
    return asyncio.run(run(url))


if __name__ == "__main__":
    sys.exit(main())
