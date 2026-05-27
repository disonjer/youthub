#!/usr/bin/env python3.11
"""Verifier: dumps the last 10 videos from FEhistory of the OAuth-paired
account. Run before/after watching to confirm watch attribution.

Usage:
    python3.11 verify_history.py
"""
from __future__ import annotations

import sys

from youthub import auth, innertube


def _txt(node) -> str:
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node:
            return node["simpleText"] or ""
        return "".join(r.get("text", "")
                       for r in (node.get("runs") or [])
                       if isinstance(r, dict))
    return ""


def _walk_for_videos(node, out):
    if isinstance(node, dict):
        # TVHTML5: tileRenderer with onSelectCommand.watchEndpoint.videoId
        tvr = node.get("tileRenderer")
        if tvr:
            vid = ((tvr.get("onSelectCommand") or {})
                   .get("watchEndpoint") or {}).get("videoId")
            title = _txt((tvr.get("metadata", {})
                         .get("tileMetadataRenderer", {}).get("title")))
            if vid:
                out.append((vid, title))
        # WEB / mobile schemas
        cvr = node.get("compactVideoRenderer") or node.get("videoRenderer")
        if cvr:
            vid = cvr.get("videoId")
            title = _txt(cvr.get("title"))
            if vid:
                out.append((vid, title))
        for v in node.values():
            _walk_for_videos(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_for_videos(v, out)


def main() -> int:
    try:
        tokens = auth.get_tokens()
    except Exception as e:
        print(f"no OAuth tokens: {e}", file=sys.stderr)
        return 1
    if tokens is None:
        print("no OAuth tokens (run: python3.11 -m youthub.auth)", file=sys.stderr)
        return 1

    with innertube.InnerTube(tokens) as it:
        data = it.browse("FEhistory")

    videos: list[tuple[str, str]] = []
    _walk_for_videos(data, videos)
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for v in videos:
        if v[0] in seen:
            continue
        seen.add(v[0])
        unique.append(v)

    print(f"Last {min(10, len(unique))} videos in your YouTube history:")
    print()
    for i, (vid, title) in enumerate(unique[:10], 1):
        title = title[:70]
        print(f"  {i:>2}. {vid}  {title}")
    if not unique:
        print("  (history is empty — or FEhistory schema changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
