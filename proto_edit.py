#!/usr/bin/env python3.11
"""Minimal protobuf encode/decode with **stable ordering**.

Designed to round-trip: parse a raw protobuf message into a tree, mutate a
field, and re-serialize so the byte representation matches the original
intent (preserves field order, repeated entries, unknown wire types).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import Union, List


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        b = buf[pos]
        val |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            return val, pos
        shift += 7


def _write_varint(out: BytesIO, val: int) -> None:
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.write(bytes([b | 0x80]))
        else:
            out.write(bytes([b]))
            return


WT_VARINT = 0
WT_I64 = 1
WT_LEN = 2
WT_I32 = 5


@dataclass
class Field:
    number: int
    wire_type: int
    # For VARINT/I64/I32: int. For LEN: either bytes or Message (if recursive).
    value: Union[int, bytes, "Message"]


@dataclass
class Message:
    fields: List[Field] = field(default_factory=list)

    # Convenience accessors
    def all(self, field_no: int) -> List[Field]:
        return [f for f in self.fields if f.number == field_no]

    def first(self, field_no: int) -> Field | None:
        for f in self.fields:
            if f.number == field_no:
                return f
        return None

    def get_varint(self, field_no: int) -> int | None:
        f = self.first(field_no)
        if f is None or f.wire_type != WT_VARINT:
            return None
        return f.value  # type: ignore[return-value]

    def set_varint(self, field_no: int, val: int) -> None:
        for f in self.fields:
            if f.number == field_no and f.wire_type == WT_VARINT:
                f.value = val
                return
        self.fields.append(Field(field_no, WT_VARINT, val))


def parse(buf: bytes) -> Message:
    """Parse buf as a protobuf message, recursing into LEN fields when they
    look like nested messages (best-effort)."""
    return _parse(buf, 0, len(buf))


def _parse(buf: bytes, start: int, end: int) -> Message:
    msg = Message()
    pos = start
    while pos < end:
        tag, pos = _read_varint(buf, pos)
        wt = tag & 7
        field_no = tag >> 3
        if wt == WT_VARINT:
            val, pos = _read_varint(buf, pos)
            msg.fields.append(Field(field_no, wt, val))
        elif wt == WT_I64:
            v = buf[pos : pos + 8]
            pos += 8
            msg.fields.append(Field(field_no, wt, v))
        elif wt == WT_I32:
            v = buf[pos : pos + 4]
            pos += 4
            msg.fields.append(Field(field_no, wt, v))
        elif wt == WT_LEN:
            length, pos = _read_varint(buf, pos)
            data = buf[pos : pos + length]
            pos += length
            child: Union[bytes, Message]
            if _looks_like_msg(data):
                try:
                    child = _parse(data, 0, len(data))
                except Exception:
                    child = data
            else:
                child = data
            msg.fields.append(Field(field_no, wt, child))
        else:
            # SGROUP/EGROUP or unknown: bail out silently with what we have so far
            break
    return msg


def _looks_like_msg(buf: bytes) -> bool:
    if not buf:
        return False
    try:
        pos = 0
        count = 0
        while pos < len(buf):
            tag, pos = _read_varint(buf, pos)
            wt = tag & 7
            field_no = tag >> 3
            if wt not in (0, 1, 2, 5) or field_no == 0 or field_no > 100_000:
                return False
            if wt == 0:
                _, pos = _read_varint(buf, pos)
            elif wt == 1:
                pos += 8
            elif wt == 2:
                ln, pos = _read_varint(buf, pos)
                if ln < 0 or pos + ln > len(buf):
                    return False
                pos += ln
            elif wt == 5:
                pos += 4
            count += 1
        return pos == len(buf) and count >= 1
    except Exception:
        return False


def serialize(msg: Message) -> bytes:
    out = BytesIO()
    for f in msg.fields:
        _write_varint(out, (f.number << 3) | f.wire_type)
        if f.wire_type == WT_VARINT:
            _write_varint(out, f.value)  # type: ignore[arg-type]
        elif f.wire_type == WT_I64:
            out.write(f.value if isinstance(f.value, (bytes, bytearray)) else f.value.to_bytes(8, "little"))
        elif f.wire_type == WT_I32:
            out.write(f.value if isinstance(f.value, (bytes, bytearray)) else f.value.to_bytes(4, "little"))
        elif f.wire_type == WT_LEN:
            if isinstance(f.value, Message):
                inner = serialize(f.value)
            else:
                inner = bytes(f.value)
            _write_varint(out, len(inner))
            out.write(inner)
    return out.getvalue()
