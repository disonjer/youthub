#!/usr/bin/env python3.11
"""End-to-end smoke test: pair via PIN if needed, fetch home feed, dump JSON.

Run once. First time you'll see the PIN-pairing banner — open the URL,
type the code, wait. After that the refresh_token in cache/oauth.json
lets future runs skip the prompt.

Output:
  cache/innertube_home.json  — full raw response for inspection
  stdout                     — top-level structure preview
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from youthub import innertube

CACHE = Path(__file__).resolve().parent / "cache"


def shape(obj, depth=0, max_depth=3, max_keys=6):
    """Print a JSON skeleton — keys + types — to make structure visible."""
    pad = "  " * depth
    if isinstance(obj, dict):
        if depth >= max_depth:
            print(f"{pad}{{...{len(obj)} keys...}}")
            return
        keys = list(obj.keys())[:max_keys]
        for k in keys:
            v = obj[k]
            if isinstance(v, (dict, list)):
                print(f"{pad}{k}:")
                shape(v, depth + 1, max_depth, max_keys)
            else:
                preview = repr(v)
                if len(preview) > 60:
                    preview = preview[:57] + "..."
                print(f"{pad}{k} = {preview}")
        if len(obj) > max_keys:
            print(f"{pad}... +{len(obj) - max_keys} more keys")
    elif isinstance(obj, list):
        if depth >= max_depth:
            print(f"{pad}[...{len(obj)} items...]")
            return
        print(f"{pad}[{len(obj)} items]")
        if obj:
            shape(obj[0], depth + 1, max_depth, max_keys)


def main():
    with innertube.InnerTube() as it:
        print("[test] fetching home feed (FEwhat_to_watch)…")
        data = it.home()

    CACHE.mkdir(exist_ok=True)
    out = CACHE / "innertube_home.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"[test] dumped to {out} ({out.stat().st_size:,} bytes)")
    print()
    print("--- response shape (top 3 levels) ---")
    shape(data)
    print()
    # quick check: is there a "contents" field? That's where renderers live.
    has_contents = "contents" in data
    print(f"[test] has top-level 'contents' field: {has_contents}")
    if "responseContext" in data:
        ctx = data["responseContext"]
        if "visitorData" in ctx:
            print(f"[test] visitorData present (good): {ctx['visitorData'][:20]}…")


if __name__ == "__main__":
    sys.exit(main() or 0)
