#!/usr/bin/env node
/*
 * Re-applies the googlevideo SabrStream.js patch that gives us
 * `options.startAtMs` (initial playback offset) — needed so the bridge
 * can restart streaming at an arbitrary timestamp on user seek instead
 * of always playing from 0. Run after `npm install` or whenever the
 * dependency was reinstalled.
 *
 * Idempotent: if the patch is already applied, the script exits 0 and
 * reports "already patched".
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const TARGET = path.join(ROOT,
  'node_modules/googlevideo/dist/src/core/SabrStream.js');

const ORIG_INIT = `            let playerTimeMs = 0;
            if (options.state && this.restoreState(videoFormat, audioFormat, options.state)) {`;
const NEW_INIT = `            /* ffplay-yt patch: options.startAtMs lets the caller begin
             * a fresh session at an arbitrary playback offset (used
             * for seek-anywhere — restart the bridge with --start-at
             * and SabrStream asks the server for segments at that
             * timestamp instead of from 0). */
            const _startOffsetMs = Number(options.startAtMs) || 0;
            let playerTimeMs = _startOffsetMs;
            if (options.state && this.restoreState(videoFormat, audioFormat, options.state)) {`;

const ORIG_LOOP = `                abrState.playerTimeMs = this.mainFormat ? getTotalDownloadedDuration(this.mainFormat) : 0;`;
const NEW_LOOP = `                /* ffplay-yt patch: add startOffsetMs so playerTimeMs
                 * stays in absolute coordinates (the server uses it as
                 * "where am I" not "how much have I downloaded"). */
                abrState.playerTimeMs = _startOffsetMs + (this.mainFormat ? getTotalDownloadedDuration(this.mainFormat) : 0);`;

const src = fs.readFileSync(TARGET, 'utf8');
if (src.includes('ffplay-yt patch')) {
  console.log(`[patch_googlevideo] ${TARGET} already patched`);
  process.exit(0);
}

let out = src;
let applied = 0;
if (out.includes(ORIG_INIT)) { out = out.replace(ORIG_INIT, NEW_INIT); applied++; }
if (out.includes(ORIG_LOOP)) { out = out.replace(ORIG_LOOP, NEW_LOOP); applied++; }

if (applied !== 2) {
  console.error(`[patch_googlevideo] failed to apply patch ` +
                `(${applied}/2 substitutions matched). The googlevideo ` +
                `version may have changed. Open ${TARGET} and re-derive ` +
                `the patch manually.`);
  process.exit(1);
}

fs.writeFileSync(TARGET, out);
console.log(`[patch_googlevideo] applied (${applied} substitutions)`);
