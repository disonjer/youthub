#!/usr/bin/env python3.11
"""SABR client + UMP parser.

Reverse-engineered against captured Chromium <-> googlevideo exchanges
on 2026-05-21. Field numbers documented inline.

Public API:
  SabrClient(url, init_body_bytes).stream()  # yields (track_id, fmp4_bytes)

Bootstrap (URL + init_body) is captured separately via Chromium (see
bootstrap.py) and persisted to disk; this module is browser-free.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Iterable, Iterator

import urllib.request
import urllib.error

import proto_edit as pe

# ---------------------------------------------------------------------------
# UMP container parsing
# ---------------------------------------------------------------------------

UMP_MEDIA_HEADER = 20
UMP_MEDIA = 21
UMP_MEDIA_END = 22
UMP_NEXT_REQUEST_POLICY = 35
UMP_FORMAT_INITIALIZATION_METADATA = 39
UMP_SABR_REDIRECT = 40
UMP_SABR_ERROR = 41
UMP_STREAM_PROTECTION_STATUS = 55
UMP_SABR_ACK = 58
UMP_END_OF_TRACK = 59


def _ump_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """UMP uses a custom varint: leading 1-bits in the first byte indicate
    the total varint size (1–5 bytes), and the remaining bytes are stored
    little-endian (per LuanRT/googlevideo's reverse engineering).

    For values < 128 it's a single byte = the value, so the BE-vs-LE
    distinction only matters for larger numbers.
    """
    if pos >= len(buf):
        raise IndexError("ump_varint EOF")
    b0 = buf[pos]
    # Count leading 1-bits to get size (1 + count)
    size = 1
    mask = 0x80
    while size <= 4 and (b0 & mask):
        size += 1
        mask >>= 1
    if size == 1:
        return b0, pos + 1
    shift = 8 - size
    value = b0 & ((1 << shift) - 1)
    for i in range(1, size):
        if size == 5 and i == 1:
            # 5-byte form: first byte is purely the size marker (no value bits)
            shift = 0  # all 32 value bits come from bytes 1..4
            value = 0
        value |= buf[pos + i] << (shift + 8 * (i - 1))
    return value, pos + size


def parse_ump(buf: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (type, payload) for every UMP part. Stops on malformed tail."""
    pos = 0
    while pos < len(buf):
        try:
            t, pos = _ump_varint(buf, pos)
            length, pos = _ump_varint(buf, pos)
        except Exception:
            return
        if length < 0 or pos + length > len(buf):
            return
        yield t, buf[pos : pos + length]
        pos += length


# ---------------------------------------------------------------------------
# MEDIA_HEADER (type 20) — describes the chunk that follows in MEDIA (21)
# ---------------------------------------------------------------------------

@dataclass
class MediaHeader:
    header_id: int = 0
    video_id: str = ""
    itag: int = 0
    lmt: int = 0
    audio_track_id: str = ""
    start_data_range: int = 0
    start_time_ms: int = 0
    duration_ms: int = 0
    content_length: int = 0


def parse_media_header(payload: bytes) -> MediaHeader:
    msg = pe.parse(payload)
    h = MediaHeader()
    for f in msg.fields:
        if f.number == 1 and f.wire_type == pe.WT_VARINT:
            h.header_id = f.value
        elif f.number == 2 and f.wire_type == pe.WT_LEN:
            v = f.value if isinstance(f.value, (bytes, bytearray)) else b""
            h.video_id = bytes(v).decode("utf-8", errors="replace")
        elif f.number == 3 and f.wire_type == pe.WT_VARINT:
            h.itag = f.value
        elif f.number == 4 and f.wire_type == pe.WT_VARINT:
            h.lmt = f.value
        elif f.number == 5 and f.wire_type == pe.WT_LEN:
            v = f.value if isinstance(f.value, (bytes, bytearray)) else b""
            h.audio_track_id = bytes(v).decode("utf-8", errors="replace")
        elif f.number == 6 and f.wire_type == pe.WT_VARINT:
            h.start_data_range = f.value
        elif f.number == 11 and f.wire_type == pe.WT_VARINT:
            h.start_time_ms = f.value
        elif f.number == 12 and f.wire_type == pe.WT_VARINT:
            h.duration_ms = f.value
        elif f.number == 14 and f.wire_type == pe.WT_VARINT:
            h.content_length = f.value
    return h


# ---------------------------------------------------------------------------
# NEXT_REQUEST_POLICY (type 35)
# ---------------------------------------------------------------------------

@dataclass
class NextRequestPolicy:
    target_audio_readahead_ms: int = 0
    target_video_readahead_ms: int = 0
    backoff_time_ms: int = 0


def parse_next_request_policy(payload: bytes) -> NextRequestPolicy:
    msg = pe.parse(payload)
    p = NextRequestPolicy()
    for f in msg.fields:
        if f.wire_type != pe.WT_VARINT:
            continue
        if f.number == 1:
            p.target_audio_readahead_ms = f.value
        elif f.number == 2:
            p.target_video_readahead_ms = f.value
        elif f.number == 3:
            p.backoff_time_ms = f.value
    return p


# ---------------------------------------------------------------------------
# Request body construction
# ---------------------------------------------------------------------------

@dataclass
class TrackKey:
    itag: int
    lmt: int
    audio_track_id: str = ""

    def __hash__(self):
        return hash((self.itag, self.lmt, self.audio_track_id))


def _format_id_msg(key: TrackKey) -> pe.Message:
    """The {itag,lmt,audio_track} sub-message used in many places."""
    m = pe.Message()
    m.fields.append(pe.Field(1, pe.WT_VARINT, key.itag))
    m.fields.append(pe.Field(2, pe.WT_VARINT, key.lmt))
    # audio_track_id (string) is optional; only include if non-empty
    if key.audio_track_id:
        m.fields.append(pe.Field(3, pe.WT_LEN, key.audio_track_id.encode("utf-8")))
    else:
        # Captured requests sometimes include an empty len=0 field; harmless either way
        pass
    return m


def _buffered_range_msg(key: TrackKey, end_ms: int, segment_idx: int, track_no: int) -> pe.Message:
    """top-level field 3 sub-message: format_id, start_ms, end_ms, ?, segment_idx."""
    m = pe.Message()
    m.fields.append(pe.Field(1, pe.WT_LEN, _format_id_msg(key)))
    m.fields.append(pe.Field(2, pe.WT_VARINT, 0))      # start_ms
    m.fields.append(pe.Field(3, pe.WT_VARINT, end_ms)) # end_ms
    m.fields.append(pe.Field(4, pe.WT_VARINT, 1))      # observed constant
    m.fields.append(pe.Field(5, pe.WT_VARINT, track_no))
    return m


def build_request_body(
    template: pe.Message,
    selected: dict[TrackKey, int],  # key -> buffered_end_ms
    playhead_ms: int = 0,
    bandwidth_bps: int | None = None,
    player_width: int | None = None,
    player_height: int | None = None,
    max_height: int | None = None,
) -> bytes:
    """Take the bootstrap-captured template and overwrite the per-step state.

    Currently overwrites:
      - top-level field 2 (repeated, selected formats):  one per track
      - top-level field 3 (repeated, buffered ranges):   one per track
      - field 1 / sub-fields 18, 19 (player_width, player_height) if provided
      - field 1 / sub-field 23 (bandwidth_bps) if provided
      - field 1 / sub-field 59 (max_height) if provided
      - field 1 / sub-fields 28, 29, 36, 39 (playhead-related — set together)
    """
    # Clone via re-parse
    body0 = pe.serialize(template)
    msg = pe.parse(body0)

    # Strip existing top-level 2 and 3 entries (we'll repopulate)
    msg.fields = [f for f in msg.fields if f.number not in (2, 3)]

    # Player context tweaks
    ctx = msg.first(1)
    if ctx and isinstance(ctx.value, pe.Message):
        if player_width is not None:
            ctx.value.set_varint(18, player_width)
        if player_height is not None:
            ctx.value.set_varint(19, player_height)
        if bandwidth_bps is not None:
            ctx.value.set_varint(23, bandwidth_bps)
        if max_height is not None:
            ctx.value.set_varint(59, max_height)
        # Advance every playhead-candidate field. Server uses these to decide
        # when we've consumed enough buffer to deserve more data.
        for field_no in (28, 29, 36, 39):
            ctx.value.set_varint(field_no, playhead_ms)

    # Insert top-level field 2 (selected formats) and field 3 (buffered ranges)
    keys = list(selected.keys())
    sel_fields: list[pe.Field] = []
    buf_fields: list[pe.Field] = []
    for idx, key in enumerate(keys, start=1):
        # field 2 entry
        f2 = pe.Field(2, pe.WT_LEN, _format_id_msg(key))
        sel_fields.append(f2)
        # field 3 entry — only when we have any buffer
        end_ms = selected[key]
        f3 = pe.Field(3, pe.WT_LEN, _buffered_range_msg(key, end_ms, segment_idx=0, track_no=idx + 1))
        buf_fields.append(f3)

    # Re-insert preserving the original ordering convention (1, 2..., 3..., 5, 16..., 17..., 19, ...)
    new_fields: list[pe.Field] = []
    inserted_2 = False
    inserted_3 = False
    for f in msg.fields:
        # Keep field 1 first; insert 2s right after; then 3s; then everything else
        if not inserted_2 and f.number > 1:
            new_fields.extend(sel_fields)
            new_fields.extend(buf_fields)
            inserted_2 = inserted_3 = True
        new_fields.append(f)
    if not inserted_2:
        new_fields.extend(sel_fields)
        new_fields.extend(buf_fields)
    msg.fields = new_fields

    return pe.serialize(msg)


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


def http_post(url: str, body: bytes, timeout: float = 25.0) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://www.youtube.com/",
            "Origin": "https://www.youtube.com",
            "Content-Type": "application/x-protobuf",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------------------
# SABR client
# ---------------------------------------------------------------------------

@dataclass
class TrackState:
    key: TrackKey
    header_id: int = -1
    buffered_end_ms: int = 0
    total_bytes: int = 0
    ended: bool = False
    # Buffer assembled by absolute byte offset, so out-of-order chunks
    # (server can return overlapping ranges) end up as one contiguous file.
    buf: bytearray = field(default_factory=bytearray)
    # Per-response cursor: header_id → next write offset for this response
    # (reset by client between responses)
    cursor_offset: int = 0


@dataclass
class _HidState:
    key: TrackKey
    write_offset: int
    remaining: int


@dataclass
class SabrClient:
    url: str
    init_body: bytes              # bootstrap request body (req1-style)

    # HD-request hints — applied to BOTH bootstrap and continuation bodies.
    # These influence what the server thinks the client can play.
    bandwidth_bps: int = 10_000_000     # 10 Mbps, plenty for 1080p
    player_width: int = 1920
    player_height: int = 1080
    max_height: int = 1080

    template: pe.Message = field(init=False)
    tracks: dict[TrackKey, TrackState] = field(default_factory=dict)
    # Persistent across responses — server can continue a chunk into the next
    # response without re-announcing the MEDIA_HEADER.
    hid_state: dict[int, _HidState] = field(default_factory=dict)
    sent_bootstrap: bool = field(default=False)

    def __post_init__(self):
        self.template = pe.parse(self.init_body)

    def _patch_context(self, body_bytes: bytes) -> bytes:
        """Rewrite the bootstrap body's player-context fields with our HD hints."""
        msg = pe.parse(body_bytes)
        ctx = msg.first(1)
        if ctx and isinstance(ctx.value, pe.Message):
            ctx.value.set_varint(18, self.player_width)
            ctx.value.set_varint(19, self.player_height)
            ctx.value.set_varint(23, self.bandwidth_bps)
            ctx.value.set_varint(59, self.max_height)
        return pe.serialize(msg)

    def _do_bootstrap(self) -> Iterable[tuple[TrackKey, int, bytes]]:
        """Send the captured init body with HD context patched in."""
        body = self._patch_context(self.init_body)
        print(
            f"[sabr] bootstrap POST {len(body)}B "
            f"(HD: {self.player_width}x{self.player_height} @ {self.bandwidth_bps//1_000_000}Mbps, max_height={self.max_height})",
            file=sys.stderr,
        )
        status, resp = http_post(self.url, body)
        if status != 200:
            raise RuntimeError(f"bootstrap failed: HTTP {status}")
        print(f"[sabr]   resp {len(resp):,}B", file=sys.stderr)
        yield from self._consume(resp)

    def _consume(self, body: bytes) -> Iterable[tuple[TrackKey, int, bytes]]:
        """Walk a UMP body, including nested UMP inside MEDIA payloads.

        A MEDIA part's payload is:
          [hid byte][content_length bytes of raw fMP4][nested UMP stream]
        where content_length comes from the immediately-preceding MEDIA_HEADER
        for this hid. After the chunk bytes, the rest is another UMP stream
        with its own MEDIA_END / MEDIA_HEADER / MEDIA parts.

        State (hid → chunk write cursor) is shared between the outer and
        nested streams — a nested MEDIA_HEADER can re-bind a hid to a new
        chunk."""
        local_state: dict[int, _HidState] = {}
        produced = [0]

        def process(stream: bytes, depth: int):
            for t, payload in parse_ump(stream):
                if t == UMP_MEDIA_HEADER:
                    h = parse_media_header(payload)
                    key = TrackKey(itag=h.itag, lmt=h.lmt, audio_track_id=h.audio_track_id)
                    st = self.tracks.get(key)
                    if st is None:
                        st = TrackState(key=key, header_id=h.header_id)
                        self.tracks[key] = st
                        print(
                            f"[sabr] discovered track itag={key.itag} "
                            f"audio_track={key.audio_track_id!r} hid={h.header_id}",
                            file=sys.stderr,
                        )
                    local_state[h.header_id] = _HidState(
                        key=key,
                        write_offset=h.start_data_range,
                        remaining=h.content_length or (1 << 31),
                    )
                    new_end = h.start_time_ms + h.duration_ms
                    if new_end > st.buffered_end_ms:
                        st.buffered_end_ms = new_end
                elif t == UMP_MEDIA:
                    if not payload:
                        continue
                    hid = payload[0]
                    media_bytes = payload[1:]
                    state = local_state.get(hid)
                    if state is None:
                        print(
                            f"[sabr] ! MEDIA hid={hid} no header in this scope; dropping {len(media_bytes)}B",
                            file=sys.stderr,
                        )
                        continue
                    take = min(len(media_bytes), state.remaining)
                    if take > 0:
                        offset = state.write_offset
                        state.write_offset += take
                        state.remaining -= take
                        st = self.tracks[state.key]
                        end = offset + take
                        if end > len(st.buf):
                            st.buf.extend(b"\x00" * (end - len(st.buf)))
                        st.buf[offset:end] = bytes(media_bytes[:take])
                        st.total_bytes = len(st.buf)
                        produced[0] += take
                    # If there's tail after content_length, recurse — it's
                    # another UMP stream with possibly new MEDIA_HEADERs.
                    if len(media_bytes) > take:
                        process(bytes(media_bytes[take:]), depth + 1)
                elif t == UMP_END_OF_TRACK:
                    msg = pe.parse(payload)
                    hid = msg.get_varint(1) or 0
                    state = local_state.get(hid)
                    if state:
                        self.tracks[state.key].ended = True
                        print(f"[sabr] end of track itag={state.key.itag}", file=sys.stderr)
                elif t == UMP_SABR_ERROR:
                    print(f"[sabr] !! SABR_ERROR {payload.hex()[:80]}", file=sys.stderr)
                elif t == UMP_SABR_REDIRECT:
                    print(f"[sabr] SABR_REDIRECT received (len={len(payload)})", file=sys.stderr)

        process(body, depth=0)

        # Yield is a no-op now; mutation lives in self.tracks.buf directly.
        # We still iterate so callers expecting a generator don't choke.
        if produced[0] == 0:
            print("[sabr]   (no media this response)", file=sys.stderr)
        if False:
            yield  # pragma: no cover

    def run(self, max_iters: int = 1000) -> None:
        """Run the SABR loop until tracks end or we hit a quiet streak.
        Track buffers (self.tracks[*].buf) are populated in place."""
        list(self._do_bootstrap())  # discards (no longer yields)

        empty_streak = 0
        playhead_ms = 0
        for i in range(max_iters):
            if all(t.ended for t in self.tracks.values()) and self.tracks:
                print("[sabr] all tracks ended", file=sys.stderr)
                return
            selected = {key: st.buffered_end_ms for key, st in self.tracks.items() if not st.ended}
            if not selected:
                return
            min_buf = min(selected.values())
            playhead_ms = max(0, min_buf - 1000)
            body = build_request_body(
                self.template,
                selected,
                playhead_ms=playhead_ms,
                bandwidth_bps=self.bandwidth_bps,
                player_width=self.player_width,
                player_height=self.player_height,
                max_height=self.max_height,
            )
            before = {k: len(s.buf) for k, s in self.tracks.items()}
            status, resp = http_post(self.url, body)
            sel_summary = ", ".join(f"i{k.itag}:{ms}ms" for k, ms in selected.items())
            print(
                f"[sabr] iter {i:3} POST {len(body)}B ph={playhead_ms} [{sel_summary}] → HTTP {status} resp {len(resp):,}B",
                file=sys.stderr,
            )
            if status != 200:
                raise RuntimeError(f"SABR HTTP {status}")
            list(self._consume(resp))  # mutates self.tracks
            after = {k: len(s.buf) for k, s in self.tracks.items()}
            grew = any(after[k] > before.get(k, 0) for k in after)
            if not grew:
                empty_streak += 1
                playhead_ms += 1000
                if empty_streak >= 10:
                    print("[sabr] 10 empty responses in a row — assuming EOF",
                          file=sys.stderr)
                    return
                time.sleep(0.5)
            else:
                empty_streak = 0


def main(argv: list[str]) -> int:
    """Standalone test: take a dump dir, replay against its URL+req1.bin,
    dump each track's media bytes to <out>.h<N>.bin."""
    if len(argv) < 3:
        print("usage: sabr.py <dump_dir> <out_prefix>", file=sys.stderr)
        return 2
    dump_dir = Path(argv[1])
    out_prefix = Path(argv[2])
    import json
    ex = json.load((dump_dir / "exchanges.json").open())
    first = next(iter(ex.values()))
    url = first["request"]["url"]
    init_body_name = first["request_body_file"]
    init_body = (dump_dir / init_body_name).read_bytes()
    print(f"[main] url={url[:80]}...", file=sys.stderr)
    print(f"[main] init body: {init_body_name} ({len(init_body)}B)", file=sys.stderr)

    client = SabrClient(url=url, init_body=init_body)
    client.run(max_iters=200)

    for key, st in client.tracks.items():
        kind = "audio" if key.audio_track_id else "video"
        out = out_prefix.with_suffix(f".i{key.itag}.{kind}.fmp4")
        out.write_bytes(bytes(st.buf))
        print(f"[main] wrote {out} ({len(st.buf):,}B; end_ms={st.buffered_end_ms})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
