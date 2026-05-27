#!/usr/bin/env python3.11
"""Probe whether a fully-loaded Chromium ever sends a real (long) PO Token
in its SABR /videoplayback POSTs. Captures every POST for a fixed window
and prints the size of field 19.2 (poToken) in each body.

If we see growing sizes (10 → 100+ bytes), the real PO Token appears in
a later request and we should capture *that one* instead of the first.
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH",
                      str(PROJECT_DIR / ".playwright"))

from playwright.async_api import async_playwright  # noqa: E402


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7f) << shift
        if (b & 0x80) == 0:
            return result, pos
        shift += 7


def extract_po_token(body: bytes) -> bytes | None:
    pos = 0
    while pos < len(body):
        try:
            tag, pos = read_varint(body, pos)
        except IndexError:
            return None
        field_no = tag >> 3
        wire = tag & 7
        if wire == 0:
            _, pos = read_varint(body, pos)
        elif wire == 2:
            ln, pos = read_varint(body, pos)
            payload = body[pos:pos + ln]
            if field_no == 19:
                p2 = 0
                while p2 < len(payload):
                    t2, p2 = read_varint(payload, p2)
                    f2 = t2 >> 3
                    w2 = t2 & 7
                    if w2 == 2:
                        l2, p2 = read_varint(payload, p2)
                        if f2 == 2:
                            return payload[p2:p2 + l2]
                        p2 += l2
                    elif w2 == 0:
                        _, p2 = read_varint(payload, p2)
                    else:
                        p2 += 4 if w2 == 5 else 8
            pos += ln
        else:
            pos += 4 if wire == 5 else 8
    return None


async def main(video_url: str, wait_secs: int = 30) -> None:
    captures: list[tuple[float, str, bytes]] = []  # (t, url, body)

    proxy = os.environ.get("HTTPS_PROXY")
    proxy_arg = {"server": proxy} if proxy else None

    async with async_playwright() as pw:
        kwargs = {
            "headless": True,
            "args": ["--no-sandbox", "--autoplay-policy=no-user-gesture-required"],
        }
        if proxy_arg:
            kwargs["proxy"] = proxy_arg
        browser = await pw.chromium.launch(**kwargs)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"))
        await ctx.add_cookies([
            {"name": "CONSENT", "value": "YES+", "domain": ".youtube.com", "path": "/"},
            {"name": "SOCS", "value": "CAI", "domain": ".youtube.com", "path": "/"},
        ])
        page = await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)
        await cdp.send("Network.enable", {})

        pending: dict[str, dict] = {}

        def on_req(ev):
            req = ev.get("request") or {}
            url = req.get("url", "")
            if "googlevideo.com/videoplayback" not in url:
                return
            if not req.get("hasPostData", False):
                return
            pending[ev["requestId"]] = {"url": url, "t": time.time()}

        cdp.on("Network.requestWillBeSent", on_req)

        await page.goto(video_url, wait_until="domcontentloaded", timeout=60_000)
        await page.evaluate("() => { const v=document.querySelector('video'); "
                            "if (v) { v.muted=true; v.play().catch(()=>{});} }")

        t0 = time.time()
        seen: set[str] = set()
        while time.time() - t0 < wait_secs:
            await asyncio.sleep(0.5)
            for rid, info in list(pending.items()):
                if rid in seen:
                    continue
                try:
                    res = await cdp.send("Network.getRequestPostData",
                                         {"requestId": rid})
                    blob = res.get("postData", "")
                    if not blob:
                        continue
                    try:
                        body = base64.b64decode(blob, validate=True)
                    except Exception:
                        body = blob.encode("latin-1")
                    seen.add(rid)
                    captures.append((info["t"] - t0, info["url"], body))
                except Exception:
                    pass

        await browser.close()

    captures.sort(key=lambda x: x[0])
    print(f"captured {len(captures)} videoplayback POSTs")
    for i, (t, url, body) in enumerate(captures):
        po = extract_po_token(body)
        po_len = len(po) if po else 0
        po_head = po[:8].hex() if po else "—"
        print(f"  #{i:02d} t={t:5.1f}s body={len(body):5d}B poToken={po_len:4d}B "
              f"head={po_head}")
        if po and po_len > 0:
            Path(f"cache/probe_{i:02d}_t{int(t)}_po{po_len}.bin").write_bytes(body)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else \
        "https://www.youtube.com/watch?v=0D659u9OBo0"
    wait = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    asyncio.run(main(url, wait))
