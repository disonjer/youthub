#!/usr/bin/env python3.11
"""Extract MEDIA (type 21) chunks from UMP response files and concat them.

Also splits by MEDIA_HEADER hints if possible (audio vs video track id).
Outputs:
  - <out>.media.bin  — every MEDIA chunk concatenated in order
  - <out>.media.<header_id>.bin — per-header_id streams (e.g. video vs audio)
"""
from __future__ import annotations

import sys
from pathlib import Path


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(buf):
        raise ValueError("EOF")
    b0 = buf[pos]
    if b0 < 0x80:
        return b0, pos + 1
    if b0 < 0xC0:
        return ((b0 & 0x3F) << 8) | buf[pos + 1], pos + 2
    if b0 < 0xE0:
        return ((b0 & 0x1F) << 16) | (buf[pos + 1] << 8) | buf[pos + 2], pos + 3
    if b0 < 0xF0:
        return (
            ((b0 & 0x0F) << 24)
            | (buf[pos + 1] << 16)
            | (buf[pos + 2] << 8)
            | buf[pos + 3],
            pos + 4,
        )
    return (
        (buf[pos + 1] << 24)
        | (buf[pos + 2] << 16)
        | (buf[pos + 3] << 8)
        | buf[pos + 4],
        pos + 5,
    )


# UMP MEDIA has a sub-header: first byte is the "header id" matching the
# preceding MEDIA_HEADER, then the actual mp4 fragment bytes follow. We need
# to strip that header byte to recover clean mp4.
def parse_ump(buf: bytes):
    """Yield (type, payload_bytes) tuples, skipping malformed tails."""
    pos = 0
    while pos < len(buf):
        try:
            t, pos = read_varint(buf, pos)
            length, pos = read_varint(buf, pos)
        except Exception:
            return
        if length < 0 or pos + length > len(buf):
            return
        data = buf[pos : pos + length]
        pos += length
        yield t, data


def extract(files: list[Path], out_prefix: Path):
    all_media = bytearray()
    by_header: dict[int, bytearray] = {}
    current_header_id = None
    total = 0
    media_count = 0

    for f in files:
        buf = f.read_bytes()
        print(f"[extract] {f.name} ({len(buf):,}B)", file=sys.stderr)
        for t, data in parse_ump(buf):
            if t == 20:  # MEDIA_HEADER
                # data is a protobuf; the first varint after a 0x08 tag is the
                # header id. Tag 1 (field 1, wire type 0) = 0x08.
                if data and data[0] == 0x08:
                    try:
                        hid, _ = read_varint(data, 1)
                        current_header_id = hid
                    except Exception:
                        pass
            elif t == 21:  # MEDIA
                # First byte of MEDIA is the header id this chunk belongs to
                if len(data) < 1:
                    continue
                hid = data[0]
                payload = data[1:]
                all_media.extend(payload)
                by_header.setdefault(hid, bytearray()).extend(payload)
                total += len(payload)
                media_count += 1
                print(
                    f"[extract]   MEDIA hid={hid} +{len(payload):,}B "
                    f"(prefix={payload[:8].hex()})",
                    file=sys.stderr,
                )

    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    main_out = out_prefix.with_suffix(".media.bin")
    main_out.write_bytes(bytes(all_media))
    print(f"[extract] wrote {main_out} ({total:,}B, {media_count} chunks)", file=sys.stderr)
    for hid, b in sorted(by_header.items()):
        p = out_prefix.with_suffix(f".media.h{hid}.bin")
        p.write_bytes(bytes(b))
        print(f"[extract]   {p.name} ({len(b):,}B)", file=sys.stderr)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: ump_extract.py <out_prefix> <resp1.bin> [resp2.bin ...]", file=sys.stderr)
        return 2
    out_prefix = Path(sys.argv[1])
    files = sorted(Path(a) for a in sys.argv[2:])
    extract(files, out_prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
