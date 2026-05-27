#!/usr/bin/env python3.11
"""Quick & dirty UMP parser to see what's inside a captured response."""
import sys
from collections import Counter
from pathlib import Path


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read UMP varint. Returns (value, new_pos).

    UMP varints are LEB128-like, low-byte-first.
    """
    # UMP uses a 1-5 byte varint where first byte's high bits indicate length
    # See: https://github.com/davidzeng0/innertube/blob/main/docs/youtube/sabr.md
    if pos >= len(buf):
        return 0, pos
    b0 = buf[pos]
    if b0 < 0x80:
        return b0, pos + 1
    if b0 < 0xC0:
        return ((b0 & 0x3F) << 8) | buf[pos + 1], pos + 2
    if b0 < 0xE0:
        return ((b0 & 0x1F) << 16) | (buf[pos + 1] << 8) | buf[pos + 2], pos + 3
    if b0 < 0xF0:
        return (
            ((b0 & 0x0F) << 24) | (buf[pos + 1] << 16) | (buf[pos + 2] << 8) | buf[pos + 3],
            pos + 4,
        )
    return (
        (buf[pos + 1] << 24) | (buf[pos + 2] << 16) | (buf[pos + 3] << 8) | buf[pos + 4],
        pos + 5,
    )


# Known UMP part types (collected from public reverse-engineering work)
KNOWN = {
    10: "ONESIE_HEADER",
    11: "ONESIE_DATA",
    12: "ONESIE_ENCRYPTED_MEDIA",
    20: "MEDIA_HEADER",
    21: "MEDIA",
    22: "MEDIA_END",
    31: "LIVE_METADATA",
    32: "HOSTNAME_CHANGE_HINT",
    33: "LIVE_METADATA_PROMISE",
    34: "LIVE_METADATA_PROMISE_CANCELLATION",
    35: "NEXT_REQUEST_POLICY",
    36: "USTREAMER_VIDEO_AND_FORMAT_DATA",
    37: "FORMAT_SELECTION_CONFIG",
    38: "USTREAMER_SELECTED_MEDIA_STREAM",
    39: "FORMAT_INITIALIZATION_METADATA",
    40: "SABR_REDIRECT",
    41: "SABR_ERROR",
    42: "SABR_SEEK",
    43: "RELOAD_PLAYER_RESPONSE",
    44: "PLAYBACK_START_POLICY",
    45: "ALLOWED_CACHED_FORMATS",
    46: "START_BW_SAMPLING_HINT",
    47: "PAUSE_BW_SAMPLING_HINT",
    48: "SELECTABLE_FORMATS",
    49: "REQUEST_IDENTIFIER",
    50: "REQUEST_CANCELLATION_POLICY",
    51: "ONESIE_PREFETCH_REJECTION",
    52: "TIMELINE_CONTEXT",
    53: "REQUEST_PIPELINING",
    54: "SABR_CONTEXT_UPDATE",
    55: "STREAM_PROTECTION_STATUS",
    56: "SABR_CONTEXT_SENDING_POLICY",
    57: "LAWNMOWER_POLICY",
    58: "SABR_ACK",
    59: "END_OF_TRACK",
    60: "CACHE_LOAD_POLICY",
    61: "LAWNMOWER_MESSAGING_POLICY",
    62: "PREWARM_CONNECTION",
    63: "PLAYBACK_DEBUG_INFO",
    64: "SNACKBAR_MESSAGE",
}


def parse(buf: bytes) -> list[tuple[int, str, int, bytes]]:
    """Return list of (type, name, length, data_preview)."""
    out = []
    pos = 0
    while pos < len(buf):
        try:
            t, pos = read_varint(buf, pos)
            length, pos = read_varint(buf, pos)
            data = buf[pos : pos + length]
            pos += length
            out.append((t, KNOWN.get(t, f"?type{t}"), length, data))
        except Exception as e:
            out.append((-1, f"PARSE_ERR@{pos}: {e}", 0, b""))
            break
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: ump_inspect.py <file.bin> [file2.bin ...]", file=sys.stderr)
        sys.exit(2)
    for path_str in sys.argv[1:]:
        path = Path(path_str)
        buf = path.read_bytes()
        print(f"=== {path.name} ({len(buf):,} bytes) ===")
        parts = parse(buf)
        type_counts = Counter()
        total_media = 0
        for i, (t, name, length, data) in enumerate(parts):
            type_counts[(t, name)] += 1
            if name == "MEDIA":
                total_media += length
            if i < 20 or name in ("MEDIA_HEADER", "FORMAT_INITIALIZATION_METADATA", "SABR_ERROR"):
                preview = data[:32].hex() if data else ""
                print(f"  [{i:3}] type={t:>3} {name:<35} len={length:>7} data[0:32]={preview}")
        print(f"  ... total parts: {len(parts)}, media bytes: {total_media:,}")
        print(f"  type histogram:")
        for (t, n), c in type_counts.most_common():
            print(f"    {n} (t={t}): {c}x")


if __name__ == "__main__":
    main()
