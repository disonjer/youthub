#!/usr/bin/env python3.11
"""Bootstrap the SABR session for a given YouTube watch URL.

Opens the video in headless Chromium (Playwright + CDP), waits for the
browser to issue its first `googlevideo.com/videoplayback` POST, captures
that URL and the binary protobuf request body, and returns them.

The captured URL is valid for ~17 hours (until the `expire=` param in the
URL); the body can be replayed/mutated freely within that window. Both are
cached on disk per-video so we only spin up Chromium once.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# Project-local Playwright browsers (working-dir rule: nothing in $HOME).
# Must be set BEFORE importing playwright so its lazy launch picks it up.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH",
                      str(PROJECT_DIR / ".playwright"))

from playwright.async_api import async_playwright  # noqa: E402

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


@dataclass
class Bootstrap:
    video_id: str
    sabr_url: str
    init_body_path: str   # path to .bin file with binary POST body
    obtained_at: int
    expires_at: int       # epoch seconds — derived from URL `expire=` param
    # Captured directly from the browser's window.ytInitialPlayerResponse.
    # The bridge prefers these over re-fetching /watch (which YT may bot-wall
    # for anonymous Node requests). Optional so old caches keep working.
    player_response_path: Optional[str] = None


def video_id_from_url(url: str) -> str:
    """Extract YouTube video ID from any watch / short URL form."""
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"/(?:embed|shorts)/([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    raise ValueError(f"could not extract video id from URL: {url!r}")


def _resolve_proxy() -> str | None:
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(var)
        if v:
            return v
    return None


def _cache_paths(video_id: str) -> tuple[Path, Path, Path]:
    return (
        CACHE_DIR / f"bootstrap_{video_id}.json",
        CACHE_DIR / f"bootstrap_{video_id}.body.bin",
        CACHE_DIR / f"bootstrap_{video_id}.player.json",
    )


def load_cached(video_id: str) -> Bootstrap | None:
    meta_path, body_path, player_path = _cache_paths(video_id)
    if not (meta_path.exists() and body_path.exists()):
        return None
    try:
        data = json.loads(meta_path.read_text())
    except Exception:
        return None
    if time.time() > data.get("expires_at", 0):
        return None
    return Bootstrap(
        video_id=data["video_id"],
        sabr_url=data["sabr_url"],
        init_body_path=str(body_path),
        obtained_at=data["obtained_at"],
        expires_at=data["expires_at"],
        player_response_path=str(player_path) if player_path.exists() else None,
    )


def _save(b: Bootstrap, body: bytes, player_response: dict | None) -> None:
    meta_path, body_path, player_path = _cache_paths(b.video_id)
    body_path.write_bytes(body)
    if player_response is not None:
        player_path.write_text(json.dumps(player_response, ensure_ascii=False))
        b.player_response_path = str(player_path)
    meta_path.write_text(json.dumps(asdict(b), indent=2))


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7f) << shift
        if (b & 0x80) == 0:
            return result, pos
        shift += 7


def _extract_po_token_len(body: bytes) -> int:
    """Length of StreamerContext.poToken (field 19 → 2) in a SABR POST.

    Used to distinguish the cold-start placeholder (~10B) the browser
    sends in its very first /videoplayback POST from the real BotGuard
    token (~90-180B) it switches to a couple seconds later.
    """
    try:
        pos = 0
        while pos < len(body):
            tag, pos = _read_varint(body, pos)
            field_no = tag >> 3
            wire = tag & 7
            if wire == 0:
                _, pos = _read_varint(body, pos)
            elif wire == 2:
                ln, pos = _read_varint(body, pos)
                if field_no == 19:
                    p2 = 0
                    payload = body[pos:pos + ln]
                    while p2 < len(payload):
                        t2, p2 = _read_varint(payload, p2)
                        f2 = t2 >> 3
                        w2 = t2 & 7
                        if w2 == 2:
                            l2, p2 = _read_varint(payload, p2)
                            if f2 == 2:
                                return l2
                            p2 += l2
                        elif w2 == 0:
                            _, p2 = _read_varint(payload, p2)
                        else:
                            p2 += 4 if w2 == 5 else 8
                pos += ln
            else:
                pos += 4 if wire == 5 else 8
    except Exception:
        pass
    return 0


# Real BotGuard tokens are 80+ bytes; cold-start placeholders are ~10B.
MIN_PO_TOKEN_BYTES = 40


async def _open_browser(pw, engine: str, headless: bool, proxy_arg):
    """Launch the browser engine and return (browser, browser_close_coro).

    `engine`:
      * "chromium" — Playwright's bundled Chromium (headless_shell). Fast
        but increasingly recognised by YouTube as an automated client.
      * "camoufox" — Camoufox (anti-fingerprint Firefox). Slower start
        but evades the "Sign in to confirm you're not a bot" wall.
    """
    if engine == "camoufox":
        from camoufox.async_api import AsyncCamoufox  # lazy import
        # Pass the proxy through as-is from HTTPS_PROXY. See memory:
        # project_proxy.md — never auto-convert the scheme.
        #
        # `geoip=False`: Camoufox's geoip lookup makes its own request
        # through the proxy before the browser launches; we've seen
        # that probe burn a proxy connection slot and cause subsequent
        # `page.goto` to get NS_ERROR_PROXY_CONNECTION_REFUSED. We
        # don't actually need spoofed geolocation for SABR bootstrap.
        cm = AsyncCamoufox(
            headless=headless, humanize=False, os="linux",
            proxy=proxy_arg, geoip=False,
            i_know_what_im_doing=True,
        )
        browser = await cm.__aenter__()
        async def _close():
            await cm.__aexit__(None, None, None)
        return browser, _close

    kwargs = {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required",
        ],
    }
    if proxy_arg:
        kwargs["proxy"] = proxy_arg
    browser = await pw.chromium.launch(**kwargs)
    async def _close():
        await browser.close()
    return browser, _close


async def _capture_on_page(page, youtube_url: str, video_id: str, *,
                            wait_seconds: int = 60, verbose: bool = False,
                            start_at_s: float = 0.0, engine: str = "chromium",
                            ) -> tuple[str, bytes, Optional[dict]]:
    """Capture one /videoplayback POST on an already-open page.

    Used by both `_capture` (one-shot, fresh browser each call) and the
    daemon (one persistent browser, many sequential calls). Removes its
    request listener on exit so the page can be reused.
    """
    captured: dict = {"url": None, "body": None, "po_len": 0}
    captured["seek_done"] = start_at_s <= 0

    request_counter = {"all": 0, "google": 0, "videoplayback": 0}

    def on_request(req):
            # Synchronous handler — we look at the POST body inline
            # since Playwright lets us read it without an extra round
            # trip (unlike CDP's getRequestPostData).
            request_counter["all"] += 1
            url = req.url
            if "google" in url:
                request_counter["google"] += 1
            if "googlevideo.com/videoplayback" not in url:
                return
            request_counter["videoplayback"] += 1
            if req.method != "POST":
                return
            if not captured.get("seek_done"):
                return
            if captured["url"]:
                return
            try:
                body = req.post_data_buffer
            except Exception:
                body = None
            if not body:
                return
            po_len = _extract_po_token_len(body)
            if verbose:
                print(f"[bootstrap:videoplayback] POST body={len(body)}B "
                      f"po_token={po_len}B  url[:80]={url[:80]}",
                      file=sys.stderr)
            if po_len >= MIN_PO_TOKEN_BYTES:
                captured["url"] = url
                captured["body"] = body
                captured["po_len"] = po_len

    page.on("request", on_request)
    try:
        # Retry page.goto on transient connection issues. The user's
        # local SOCKS proxy occasionally refuses a new connection right
        # after a previous bootstrap closed its session — retry on
        # NS_ERROR_PROXY_CONNECTION_REFUSED and similar transient errors.
        goto_ok = False
        for attempt in range(3):
            try:
                await page.goto(youtube_url, wait_until="domcontentloaded",
                                timeout=60_000)
                goto_ok = True
                break
            except Exception as e:
                msg = str(e)
                print(f"[bootstrap] page.goto attempt {attempt + 1}: {msg[:120]}",
                      file=sys.stderr)
                if attempt < 2 and (
                    "PROXY_CONNECTION_REFUSED" in msg
                    or "NS_ERROR_PROXY" in msg
                    or "ECONNREFUSED" in msg
                    or "net::ERR_PROXY_CONNECTION_FAILED" in msg
                ):
                    await asyncio.sleep(2.0)
                    continue
                break

        # Bot-wall fast-fail. YT serves us either the real player or a
        # stripped page that needs login ("Sign in to confirm you're
        # not a bot"). Wait UP TO 8 s for *either* a meaningful title
        # OR a /videoplayback POST to land. Whichever comes first
        # decides: real title → continue normally; only bot-wall
        # markers visible → abort.
        if goto_ok:
            try:
                bot_wall = False
                wall_reason = ""
                deadline = time.time() + 8.0
                title = ""
                while time.time() < deadline:
                    title = await page.title()
                    body_text = await page.evaluate(
                        "() => document.body ? document.body.innerText.slice(0,500) : ''"
                    )
                    lt = (title or "").lower().strip()
                    lb = (body_text or "").lower()
                    has_real_title = (
                        lt and lt not in ("- youtube", "youtube")
                        and "youtube" in lt
                    )
                    has_post = request_counter["videoplayback"] > 0
                    if has_real_title or has_post:
                        break
                    if ("not a bot" in lb or "confirm you" in lb
                            or "подтвердите" in lb or "не робот" in lb):
                        bot_wall = True
                        wall_reason = "body has bot-wall text"
                        break
                    await asyncio.sleep(0.3)
                else:
                    bot_wall = True
                    wall_reason = f"no real title in 8 s (title={title!r})"
                if verbose:
                    print(f"[bootstrap] page title: {title!r}  "
                          f"bot_wall={bot_wall}  videoplayback="
                          f"{request_counter['videoplayback']}",
                          file=sys.stderr)
                if bot_wall:
                    print(f"[bootstrap] bot wall on {engine}: {wall_reason}",
                          file=sys.stderr)
                    try:
                        shot = CACHE_DIR / f"bootstrap_{video_id}_debug.png"
                        await page.screenshot(path=str(shot), full_page=False)
                    except Exception:
                        pass
                    raise RuntimeError(f"bot wall ({engine})")
            except RuntimeError:
                raise
            except Exception as e:
                print(f"[bootstrap] diag eval failed: {e}", file=sys.stderr)

        # Force playback (autoplay may be muted/blocked)
        try:
            await page.evaluate(
                "() => { const v = document.querySelector('video'); "
                "if (v) { v.muted = true; v.play().catch(()=>{}); } }"
            )
        except Exception:
            pass

        if start_at_s > 0:
            try:
                await page.wait_for_function(
                    "() => { const v = document.querySelector('video'); "
                    "return v && v.readyState >= 1; }",
                    timeout=15_000,
                )
                await page.evaluate(
                    f"() => {{ const v = document.querySelector('video'); "
                    f"if (v) {{ v.currentTime = {start_at_s}; "
                    f"v.play().catch(()=>{{}}); }} }}"
                )
                if verbose:
                    print(f"[bootstrap] seeked to {start_at_s}s",
                          file=sys.stderr)
            except Exception as e:
                print(f"[bootstrap] seek failed: {e}", file=sys.stderr)
            captured["seek_done"] = True

        # Wait for the request listener to capture a real-token POST.
        deadline = time.time() + wait_seconds
        last_log = 0.0
        while time.time() < deadline and captured["url"] is None:
            await asyncio.sleep(0.25)
            if verbose and time.time() - last_log > 5:
                last_log = time.time()
                print(f"[bootstrap] waiting… requests={request_counter['all']} "
                      f"google={request_counter['google']} "
                      f"videoplayback={request_counter['videoplayback']}",
                      file=sys.stderr)
        if captured["url"] is None:
            try:
                shot = CACHE_DIR / f"bootstrap_{video_id}_debug.png"
                await page.screenshot(path=str(shot), full_page=False)
                print(f"[bootstrap] debug screenshot saved to {shot}",
                      file=sys.stderr)
            except Exception as e:
                print(f"[bootstrap] screenshot failed: {e}", file=sys.stderr)
            if verbose:
                try:
                    body_text = await page.evaluate(
                        "() => document.body ? document.body.innerText.slice(0, 500) : ''"
                    )
                    print(f"[bootstrap] final body[:500]: {body_text!r}",
                          file=sys.stderr)
                except Exception:
                    pass
            raise RuntimeError(
                f"no SABR POST observed within timeout "
                f"(saw {request_counter['all']} requests, "
                f"{request_counter['google']} to google, "
                f"{request_counter['videoplayback']} to videoplayback)"
            )

        body = captured["body"]
        if verbose:
            print(f"[bootstrap] selected POST with po_token="
                  f"{captured['po_len']}B", file=sys.stderr)

        # Grab the full playerResponse so the bridge skips re-fetching
        # /watch. window.ytInitialPlayerResponse first; HTML regex as
        # Camoufox-Firefox sometimes hides the JS global.
        pr_obj = None
        try:
            pr_obj = await page.evaluate(
                "() => window.ytInitialPlayerResponse || null")
        except Exception as e:
            if verbose:
                print(f"[bootstrap] page.evaluate PR failed: {e}",
                      file=sys.stderr)
        if not pr_obj:
            try:
                html = await page.content()
                m = re.search(
                    r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;\s*(?:var|</script>)",
                    html, re.S)
                if m:
                    pr_obj = json.loads(m.group(1))
                    if verbose:
                        print(f"[bootstrap] extracted playerResponse from HTML "
                              f"({len(m.group(1))} chars)", file=sys.stderr)
            except Exception as e:
                if verbose:
                    print(f"[bootstrap] page.content fallback failed: {e}",
                          file=sys.stderr)
        if pr_obj and verbose:
            print(f"[bootstrap] captured playerResponse "
                  f"({len(json.dumps(pr_obj))} chars, "
                  f"status={pr_obj.get('playabilityStatus', {}).get('status')})",
                  file=sys.stderr)

        if not body:
            raise RuntimeError(
                "captured SABR URL but failed to retrieve its body")
        return captured["url"], body, pr_obj
    finally:
        try: page.remove_listener("request", on_request)
        except Exception: pass


async def _capture(youtube_url: str, video_id: str, *,
                   wait_seconds: int = 60,
                   headless: bool = True,
                   verbose: bool = False,
                   start_at_s: float = 0.0,
                   engine: str = "chromium",
                   ) -> tuple[str, bytes, Optional[dict]]:
    """Open a fresh browser, capture one /videoplayback POST, close.

    Used by the legacy one-shot path. The daemon path uses
    `_capture_on_page` directly against its persistent page.
    """
    proxy = _resolve_proxy()
    proxy_arg = {"server": proxy} if proxy else None

    async with async_playwright() as pw:
        browser, browser_close = await _open_browser(
            pw, engine, headless, proxy_arg)
        try:
            ctx_kwargs = {} if engine == "camoufox" else {"user_agent": UA}
            context = await browser.new_context(**ctx_kwargs)
            await context.add_cookies(
                [
                    {"name": "CONSENT", "value": "YES+",
                     "domain": ".youtube.com", "path": "/"},
                    {"name": "SOCS", "value": "CAI",
                     "domain": ".youtube.com", "path": "/"},
                ]
            )
            page = await context.new_page()
            if verbose:
                page.on("console", lambda m: print(
                    f"[bootstrap:console] {m.type}: {m.text[:200]}",
                    file=sys.stderr))
                page.on("pageerror", lambda e: print(
                    f"[bootstrap:pageerror] {e}", file=sys.stderr))
            return await _capture_on_page(
                page, youtube_url, video_id,
                wait_seconds=wait_seconds, verbose=verbose,
                start_at_s=start_at_s, engine=engine,
            )
        finally:
            try: await browser_close()
            except Exception: pass


# Camoufox is the default: it's the anti-fingerprint Firefox build that
# reliably gets past YT's "Sign in to confirm you're not a bot" wall.
# Plain Chromium gets walled intermittently on the same network. Override
# via BOOTSTRAP_ENGINE env if you need the faster Chromium path.
DEFAULT_ENGINE = os.environ.get("BOOTSTRAP_ENGINE", "camoufox")


def _try_engine(youtube_url: str, vid: str, engine: str,
                headless: bool, verbose: bool,
                start_at_s: float):
    if start_at_s > 0:
        print(f"[bootstrap] launching {engine} for {vid} "
              f"(seek to {start_at_s:.1f}s)…", file=sys.stderr)
    else:
        print(f"[bootstrap] launching {engine} for {vid}…", file=sys.stderr)
    return asyncio.run(
        _capture(youtube_url, vid,
                 headless=headless, verbose=verbose,
                 start_at_s=start_at_s, engine=engine))


def _env_headless_default() -> bool:
    # BOOTSTRAP_HEADLESS=0 / false / no → visible browser. Useful when
    # diagnosing proxy issues so the user can see Firefox's error page.
    v = os.environ.get("BOOTSTRAP_HEADLESS", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def _use_daemon() -> bool:
    # OFF by default — the main pipeline (bridge_player → sabr_bridge)
    # no longer needs bootstrap at all (PO Token comes from po_token.mjs
    # via bgutils, /watch fetch from inside the bridge). The daemon is
    # left as opt-in (`BOOTSTRAP_DAEMON=1`) for debugging only.
    v = os.environ.get("BOOTSTRAP_DAEMON", "").strip().lower()
    return v in ("1", "true", "yes", "on")


# -------------------------- daemon: server side --------------------------

DAEMON_SOCK = CACHE_DIR / "bootstrap_daemon.sock"
DAEMON_PID = CACHE_DIR / "bootstrap_daemon.pid"
DAEMON_LOG = CACHE_DIR / "bootstrap_daemon.log"


def _daemon_log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    sys.stderr.write(line)
    sys.stderr.flush()


async def _daemon_main() -> None:
    """Long-running visible Camoufox + Unix-socket request handler.

    Headless Camoufox on this host can't reach the proxy (Firefox-specific
    NS_ERROR_PROXY_CONNECTION_REFUSED). Visible Camoufox works. So we
    keep ONE visible Camoufox alive and serve many videos through it —
    each `page.goto()` takes ~3-5 s vs ~12-15 s for a fresh cold start.
    """
    proxy = _resolve_proxy()
    proxy_arg = {"server": proxy} if proxy else None

    # Cleanup any stale socket/pid before starting
    for p in (DAEMON_SOCK, DAEMON_PID):
        try: p.unlink()
        except FileNotFoundError: pass

    _daemon_log("launching camoufox (visible)…")
    async with async_playwright() as pw:
        browser, browser_close = await _open_browser(
            pw, engine="camoufox", headless=False, proxy_arg=proxy_arg)
        try:
            context = await browser.new_context()
            await context.add_cookies(
                [
                    {"name": "CONSENT", "value": "YES+",
                     "domain": ".youtube.com", "path": "/"},
                    {"name": "SOCS", "value": "CAI",
                     "domain": ".youtube.com", "path": "/"},
                ]
            )
            page = await context.new_page()
            _daemon_log("camoufox up; serving requests")

            # One in-flight capture at a time. Concurrent navigations
            # would race the request listener.
            req_lock = asyncio.Lock()
            stop_evt = asyncio.Event()

            async def handle_client(reader, writer):
                try:
                    data = await asyncio.wait_for(
                        reader.readline(), timeout=5.0)
                    line = data.decode("utf-8", "ignore").strip()
                    _daemon_log(f"client cmd: {line[:120]!r}")
                    if line == "QUIT":
                        writer.write(b'{"status":"ok"}\n')
                        await writer.drain()
                        stop_evt.set()
                        return
                    if line == "PING":
                        writer.write(b'{"pong":true}\n')
                        await writer.drain()
                        return
                    if not line.startswith("GET "):
                        writer.write(b'{"error":"unknown command"}\n')
                        await writer.drain()
                        return
                    url = line[4:].strip()
                    try:
                        vid = video_id_from_url(url)
                    except Exception as e:
                        writer.write(json.dumps(
                            {"error": f"bad url: {e}"}).encode() + b"\n")
                        await writer.drain()
                        return
                    async with req_lock:
                        _daemon_log(f"capturing {vid}")
                        try:
                            sabr_url, body, pr = await _capture_on_page(
                                page, url, vid,
                                wait_seconds=60, verbose=True,
                                start_at_s=0.0, engine="camoufox",
                            )
                        except Exception as e:
                            _daemon_log(f"capture failed {vid}: {e}")
                            writer.write(json.dumps(
                                {"error": f"{type(e).__name__}: {e}"}
                            ).encode() + b"\n")
                            await writer.drain()
                            return
                    payload = {
                        "sabr_url": sabr_url,
                        "body_b64": base64.b64encode(body).decode(),
                        "player_response": pr,
                    }
                    writer.write(json.dumps(payload).encode() + b"\n")
                    await writer.drain()
                    _daemon_log(f"served {vid} ({len(body)}B body)")
                except asyncio.TimeoutError:
                    _daemon_log("client read timeout")
                except Exception as e:
                    _daemon_log(f"client handler error: {e}")
                finally:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception: pass

            server = await asyncio.start_unix_server(
                handle_client, path=str(DAEMON_SOCK))
            try:
                os.chmod(DAEMON_SOCK, 0o600)
            except Exception: pass
            DAEMON_PID.write_text(str(os.getpid()))
            _daemon_log(f"listening on {DAEMON_SOCK}")
            try:
                async with server:
                    await stop_evt.wait()
            finally:
                _daemon_log("server stopped")
        finally:
            try: await browser_close()
            except Exception: pass
            for p in (DAEMON_SOCK, DAEMON_PID):
                try: p.unlink()
                except FileNotFoundError: pass


# -------------------------- daemon: client side --------------------------

def _daemon_pid() -> Optional[int]:
    try:
        return int(DAEMON_PID.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _daemon_alive() -> bool:
    pid = _daemon_pid()
    if pid is None or not DAEMON_SOCK.exists():
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # Verify socket is responsive
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(str(DAEMON_SOCK))
        s.sendall(b"PING\n")
        reply = s.recv(64)
        s.close()
        return b'"pong"' in reply
    except Exception:
        return False


def _spawn_daemon() -> None:
    # Stale state cleanup
    for p in (DAEMON_SOCK, DAEMON_PID):
        try: p.unlink()
        except FileNotFoundError: pass
    DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(DAEMON_LOG, "a")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--daemon"],
        cwd=str(PROJECT_DIR),
        stdout=log_fp, stderr=log_fp, stdin=subprocess.DEVNULL,
        start_new_session=True, env=os.environ.copy(),
    )
    print(f"[bootstrap] spawned daemon pid={proc.pid} log={DAEMON_LOG}",
          file=sys.stderr)
    # Wait for daemon to be ready. Camoufox launch can take 5-15s on
    # cold cache, plus the first context/page setup.
    deadline = time.time() + 60.0
    while time.time() < deadline:
        if _daemon_alive():
            return
        if proc.poll() is not None:
            raise RuntimeError(
                f"daemon exited with rc={proc.returncode}; see {DAEMON_LOG}")
        time.sleep(0.3)
    raise RuntimeError(
        f"daemon didn't become ready within 60s; see {DAEMON_LOG}")


def _kill_daemon() -> bool:
    pid = _daemon_pid()
    if pid is None:
        return False
    # Polite QUIT first
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(DAEMON_SOCK))
        s.sendall(b"QUIT\n")
        s.recv(64)
        s.close()
    except Exception: pass
    # Force-terminate the process group; Playwright's browser child
    # processes need an explicit kill to exit cleanly.
    try:
        os.killpg(os.getpgid(pid), 15)
        time.sleep(0.5)
        os.killpg(os.getpgid(pid), 9)
    except (ProcessLookupError, PermissionError):
        pass
    for p in (DAEMON_SOCK, DAEMON_PID):
        try: p.unlink()
        except FileNotFoundError: pass
    return True


def _bootstrap_via_daemon(youtube_url: str, *, timeout: float = 90.0
                          ) -> tuple[str, bytes, Optional[dict]]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(DAEMON_SOCK))
    s.sendall(f"GET {youtube_url}\n".encode())
    # Response is one JSON line, can be ~500KB+ (player_response).
    buf = b""
    while True:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk: break
        buf += chunk
        if b"\n" in buf: break
    try: s.close()
    except Exception: pass
    if not buf:
        raise RuntimeError("daemon closed connection without reply")
    line = buf.split(b"\n", 1)[0]
    data = json.loads(line.decode("utf-8", "replace"))
    if "error" in data:
        raise RuntimeError(f"daemon: {data['error']}")
    return (
        data["sabr_url"],
        base64.b64decode(data["body_b64"]),
        data.get("player_response"),
    )


def bootstrap(youtube_url: str, *, force: bool = False,
              headless: Optional[bool] = None, verbose: bool = False,
              start_at_s: float = 0.0,
              engine: Optional[str] = None) -> Bootstrap:
    """Return Bootstrap for the given YouTube URL.

    `engine`: "chromium" (fast, may hit bot wall) or "camoufox"
    (slower but reliable; default). On failure with the primary
    engine we automatically retry with the other one — the user
    shouldn't have to know which YT is in the mood to bot-wall today.
    Override default via BOOTSTRAP_ENGINE env var.
    """
    if headless is None:
        headless = _env_headless_default()
    primary = engine or DEFAULT_ENGINE
    secondary = "chromium" if primary == "camoufox" else "camoufox"
    vid = video_id_from_url(youtube_url)
    if not force and start_at_s <= 0:
        cached = load_cached(vid)
        if cached:
            print(f"[bootstrap] cache hit for {vid}", file=sys.stderr)
            return cached

    # Daemon path: reuse one persistent visible Camoufox across many
    # videos. Skipped for seek-aware bootstrap (start_at_s>0) — that
    # path is rare and the daemon's flow assumes 0-time captures.
    sabr_url = body = player_response = None
    if (primary == "camoufox" and _use_daemon() and start_at_s <= 0):
        for attempt in (1, 2):
            try:
                if not _daemon_alive():
                    _spawn_daemon()
                print(f"[bootstrap] daemon GET {vid} (attempt {attempt})",
                      file=sys.stderr)
                sabr_url, body, player_response = \
                    _bootstrap_via_daemon(youtube_url)
                break
            except Exception as e:
                print(f"[bootstrap] daemon attempt {attempt} failed: {e}",
                      file=sys.stderr)
                # The daemon is dead or wedged — kill it and try again
                # once with a fresh spawn before giving up on this path.
                _kill_daemon()
                if attempt == 2:
                    print(f"[bootstrap] giving up on daemon, "
                          "falling back to one-shot", file=sys.stderr)

    if sabr_url is None:
        try:
            sabr_url, body, player_response = _try_engine(
                youtube_url, vid, primary, headless, verbose, start_at_s)
        except RuntimeError as e:
            print(f"[bootstrap] {primary} failed ({e}); retrying with "
                  f"{secondary}…", file=sys.stderr)
            sabr_url, body, player_response = _try_engine(
                youtube_url, vid, secondary, headless, verbose, start_at_s)

    q = parse_qs(urlparse(sabr_url).query)
    expire = int(q.get("expire", [0])[0])
    if not expire:
        expire = int(time.time()) + 6 * 3600  # conservative fallback

    b = Bootstrap(
        video_id=vid,
        sabr_url=sabr_url,
        init_body_path=str(_cache_paths(vid)[1]),
        obtained_at=int(time.time()),
        expires_at=expire,
    )
    _save(b, body, player_response)
    print(f"[bootstrap] captured {len(body)}B body, URL expires at "
          f"{time.strftime('%H:%M', time.localtime(expire))}"
          + (" + playerResponse" if player_response else ""),
          file=sys.stderr)
    return b


def main() -> int:
    args = sys.argv[1:]

    if "--daemon" in args:
        # Run as long-lived PO-Token server.
        try:
            asyncio.run(_daemon_main())
        except KeyboardInterrupt:
            pass
        return 0

    if "--kill-daemon" in args:
        ok = _kill_daemon()
        print("daemon killed" if ok else "no daemon running")
        return 0

    if not args:
        print("usage: bootstrap.py <youtube_url> [--force] [--verbose] "
              "[--no-headless] [--engine=chromium|camoufox]\n"
              "       bootstrap.py --daemon       # run daemon\n"
              "       bootstrap.py --kill-daemon  # stop daemon",
              file=sys.stderr)
        return 2
    force = "--force" in args
    verbose = "--verbose" in args
    no_headless = "--no-headless" in args
    engine = None
    for a in args:
        if a.startswith("--engine="):
            engine = a.split("=", 1)[1]
    url = next(a for a in args if not a.startswith("--"))
    b = bootstrap(url, force=force, verbose=verbose,
                  headless=False if no_headless else None,
                  engine=engine)
    print(json.dumps(asdict(b), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
