// PO Token minter — runs BotGuard in jsdom and mints a session-bound
// WebPO token using bgutils-js. Without this, /videoplayback (SABR)
// returns "attestation required" (sps=3) after ~100s of streaming.
//
// Public API:  await mintSessionPoToken(visitorData) -> { poToken, ttlSecs }
//
// References:
//   https://github.com/LuanRT/BgUtils — the underlying library
//   bgutils-js/README.md   — full protocol writeup
//
// Implementation notes (the reverse-engineered protocol):
//   1. Fetch attestation challenge via InnerTube /att/get. Response has
//      `interpreter_url` (BotGuard JS bundle) + `program` (bytecode).
//   2. Download the JS bundle and eval it inside a fake-browser globalObj.
//      Real browsers run it in window/document; we fake those with jsdom
//      because BotGuard probes things like `window.navigator.userAgent`.
//   3. Hand the program to BG.PoToken.generate; it loads the bytecode,
//      runs the BotGuard VM, fetches an integrity token, and mints a
//      WebPO with our visitorData as the content binding.
//
// The minted token is base64url-encoded and ~140–180 chars long.

import { JSDOM } from 'jsdom';
import { Innertube } from 'youtubei.js';
import { BG } from 'bgutils-js';

// Standard "web" request key. Same one the real player uses; it tells
// the BotGuard server which program descriptor to ship to us.
const REQUEST_KEY = 'O43z0dpjhgX20SCx4KAo';

// Build a globalObj that satisfies BotGuard's "am I in a browser?" probes.
// jsdom gives us window/document/navigator; we copy them onto globalThis
// because bgutils-js's `isBrowser()` checks ownership of `window` there.
function setupBrowserGlobals() {
  // runScripts: 'outside-only' enables `dom.window.eval` and Function()
  // bound to the jsdom window. Without it, the BotGuard interpreter
  // would execute in Node's context and never register itself on the
  // jsdom window that BotGuardClient subsequently inspects.
  const dom = new JSDOM('<!DOCTYPE html><html><head></head><body></body></html>', {
    url: 'https://www.youtube.com/',
    referrer: 'https://www.youtube.com/',
    userAgent:
      'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      + '(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36',
    runScripts: 'outside-only',
  });
  Object.defineProperty(globalThis, 'window',
    { value: dom.window, configurable: true, writable: true });
  Object.defineProperty(globalThis, 'document',
    { value: dom.window.document, configurable: true, writable: true });
  Object.defineProperty(globalThis, 'navigator',
    { value: dom.window.navigator, configurable: true, writable: true });
  // jsdom doesn't ship requestAnimationFrame/matchMedia by default —
  // polyfill them onto window so BotGuard's `isBrowser` probes pass.
  if (!dom.window.requestAnimationFrame) {
    dom.window.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 16);
  }
  if (!dom.window.matchMedia) {
    dom.window.matchMedia = () => ({
      matches: false, addListener() {}, removeListener() {},
    });
  }
  Object.defineProperty(globalThis, 'getComputedStyle',
    { value: dom.window.getComputedStyle.bind(dom.window),
      configurable: true, writable: true });
  Object.defineProperty(globalThis, 'requestAnimationFrame',
    { value: dom.window.requestAnimationFrame,
      configurable: true, writable: true });
  Object.defineProperty(globalThis, 'matchMedia',
    { value: dom.window.matchMedia,
      configurable: true, writable: true });
  Object.defineProperty(globalThis, 'HTMLElement',
    { value: dom.window.HTMLElement,
      configurable: true, writable: true });
  return dom.window;
}

export async function mintSessionPoToken(visitorData) {
  const win = setupBrowserGlobals();

  const innertube = await Innertube.create({
    retrieve_player: false,
    enable_session_cache: false,
    generate_session_locally: true,
    visitor_data: visitorData,
  });

  const challengeResp = await innertube.getAttestationChallenge(
    'ENGAGEMENT_TYPE_UNBOUND');
  const bg = challengeResp.bg_challenge;
  if (!bg) throw new Error('no bg_challenge in /att/get response');

  const interpUrl = bg.interpreter_url
    .private_do_not_access_or_else_trusted_resource_url_wrapped_value;
  const program = bg.program;
  const globalName = bg.global_name;

  const jsRes = await fetch(`https:${interpUrl}`);
  if (!jsRes.ok) throw new Error(`interpreter JS HTTP ${jsRes.status}`);
  const interpJs = await jsRes.text();

  // Execute the BotGuard VM inside the jsdom realm. Going through
  // `dom.window.eval` ensures the script's top-level `var` declarations
  // become properties of dom.window — which is exactly what BotGuardClient
  // looks up via `globalObj[globalName]`. If we use Node's own Function(),
  // the script registers itself in our isolated module scope and is
  // invisible to the BotGuard client.
  win.eval(interpJs);

  const result = await BG.PoToken.generate({
    program,
    globalName,
    bgConfig: {
      fetch,
      globalObj: win,
      identifier: visitorData,
      requestKey: REQUEST_KEY,
    },
  });

  return {
    poToken: result.poToken,
    ttlSecs: result.integrityTokenData.estimatedTtlSecs,
  };
}

// Allow `node po_minter.mjs <visitorData>` for manual testing.
if (import.meta.url === `file://${process.argv[1]}`) {
  const vd = process.argv[2];
  if (!vd) {
    console.error('usage: node po_minter.mjs <visitor_data>');
    process.exit(2);
  }
  mintSessionPoToken(vd).then(
    (r) => { process.stdout.write(JSON.stringify(r) + '\n'); },
    (e) => { console.error(`mint failed: ${e?.stack ?? e}`); process.exit(1); },
  );
}
