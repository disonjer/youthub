#!/usr/bin/env python3.11
"""Generic protobuf dumper.

Reads a raw protobuf message (no schema) and prints a tree, attempting to
recursively decode LEN fields as nested messages when they parse cleanly and
otherwise showing them as bytes/utf-8.

Run on two captured SABR POST bodies to compare structure.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Protobuf wire types
WT_VARINT = 0
WT_I64 = 1
WT_LEN = 2
WT_SGROUP = 3  # deprecated
WT_EGROUP = 4  # deprecated
WT_I32 = 5
WT_NAME = {0: "VARINT", 1: "I64", 2: "LEN", 5: "I32"}


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("varint EOF")
        b = buf[pos]
        val |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            return val, pos
        shift += 7
        if shift >= 64:
            raise ValueError("varint too long")


def looks_like_protobuf(buf: bytes) -> bool:
    """Heuristic: try to parse, return True if structure looks like protobuf."""
    if not buf:
        return False
    try:
        pos = 0
        fields = 0
        while pos < len(buf):
            tag, pos = read_varint(buf, pos)
            wt = tag & 7
            if wt not in (0, 1, 2, 5):
                return False
            field_no = tag >> 3
            if field_no == 0 or field_no > 100_000:
                return False
            if wt == WT_VARINT:
                _, pos = read_varint(buf, pos)
            elif wt == WT_I64:
                pos += 8
            elif wt == WT_LEN:
                length, pos = read_varint(buf, pos)
                if length < 0 or pos + length > len(buf):
                    return False
                pos += length
            elif wt == WT_I32:
                pos += 4
            fields += 1
            if fields > 200:
                break
        return pos == len(buf) and fields >= 1
    except Exception:
        return False


def is_printable_utf8(buf: bytes) -> bool:
    if not buf:
        return False
    try:
        s = buf.decode("utf-8")
        return all(c.isprintable() or c in "\t\n\r " for c in s) and len(s) > 0
    except Exception:
        return False


def dump(buf: bytes, indent: int = 0, max_bytes_preview: int = 32) -> str:
    out_lines: list[str] = []
    pad = "  " * indent
    pos = 0
    field_idx = 0
    while pos < len(buf):
        try:
            tag, pos = read_varint(buf, pos)
        except Exception as e:
            out_lines.append(f"{pad}<parse error @{pos}: {e}>")
            break
        wt = tag & 7
        field_no = tag >> 3
        wt_name = WT_NAME.get(wt, f"WT?{wt}")

        if wt == WT_VARINT:
            try:
                val, pos = read_varint(buf, pos)
            except Exception as e:
                out_lines.append(f"{pad}<varint err @{pos}: {e}>")
                break
            out_lines.append(f"{pad}#{field_idx} field={field_no} {wt_name} = {val}")
        elif wt == WT_I64:
            val = int.from_bytes(buf[pos : pos + 8], "little")
            pos += 8
            out_lines.append(f"{pad}#{field_idx} field={field_no} I64 = 0x{val:016x} / {val} / signed={int.from_bytes(buf[pos-8:pos],'little',signed=True)}")
        elif wt == WT_I32:
            val = int.from_bytes(buf[pos : pos + 4], "little")
            pos += 4
            out_lines.append(f"{pad}#{field_idx} field={field_no} I32 = 0x{val:08x} / {val}")
        elif wt == WT_LEN:
            try:
                length, pos = read_varint(buf, pos)
            except Exception as e:
                out_lines.append(f"{pad}<len err @{pos}: {e}>")
                break
            if pos + length > len(buf):
                out_lines.append(f"{pad}#{field_idx} field={field_no} LEN length={length} <TRUNCATED>")
                break
            data = buf[pos : pos + length]
            pos += length
            if looks_like_protobuf(data):
                out_lines.append(f"{pad}#{field_idx} field={field_no} LEN len={length} [nested msg] {{")
                out_lines.append(dump(data, indent + 1, max_bytes_preview))
                out_lines.append(f"{pad}}}")
            elif is_printable_utf8(data):
                s = data.decode("utf-8")
                out_lines.append(f"{pad}#{field_idx} field={field_no} LEN len={length} str = {s!r}")
            else:
                preview = data[:max_bytes_preview].hex()
                more = "..." if len(data) > max_bytes_preview else ""
                out_lines.append(f"{pad}#{field_idx} field={field_no} LEN len={length} bytes = {preview}{more}")
        else:
            out_lines.append(f"{pad}#{field_idx} field={field_no} UNKNOWN wire_type {wt}")
            break
        field_idx += 1
    return "\n".join(out_lines)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: proto_dump.py <file.bin>", file=sys.stderr)
        return 2
    for path_str in sys.argv[1:]:
        path = Path(path_str)
        buf = path.read_bytes()
        print(f"=== {path.name} ({len(buf):,} bytes) ===")
        print(dump(buf))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
