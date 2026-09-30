"""Lenient byte-level SMF reader that rejects what cannot be repaired.

Downloaded "MIDI" files are often not MIDI (HTML error pages, JSON), are wrapped in RIFF
``RMID`` containers, carry vendor chunks (Yamaha XF) between tracks, overrun their declared
chunk lengths or are simply truncated. NAudio (and so Resonance PatternPrep) throws on all
of these, and mido throws on most. This reader walks the chunks itself, keeps only
``MThd``/``MTrk``, stops a track at the last complete event, and returns absolute-tick
events that ``sanitize`` re-serializes as a canonical file.

Rejected (``InvalidMidi.reason``): ``empty``, ``html``, ``xml``, ``json``, ``no_mthd``,
``bad_header``, ``smpte``, ``format2``, ``no_tracks``, ``no_notes``.

Text-carrying meta events (lyrics, text, markers, ...) are parsed like any other event so
that their tick positions are known, but their payload never leaves this module except as
raw bytes handed straight to ``sanitize``, which reduces them to counts and positions.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Meta event types (SMF 1.0).
META_SEQ_NUMBER = 0x00
META_TEXT = 0x01
META_COPYRIGHT = 0x02
META_TRACK_NAME = 0x03
META_INSTRUMENT = 0x04
META_LYRIC = 0x05
META_MARKER = 0x06
META_CUE = 0x07
META_CHANNEL_PREFIX = 0x20
META_PORT = 0x21
META_END_OF_TRACK = 0x2F
META_TEMPO = 0x51
META_SMPTE_OFFSET = 0x54
META_TIME_SIGNATURE = 0x58
META_KEY_SIGNATURE = 0x59
META_SEQUENCER = 0x7F

_DATA_LEN = {0x80: 2, 0x90: 2, 0xA0: 2, 0xB0: 2, 0xC0: 1, 0xD0: 1, 0xE0: 2}


class InvalidMidi(ValueError):
    """The bytes are not a usable MIDI file; ``reason`` is a short slug."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class Event:
    tick: int          # absolute tick within the track
    status: int        # 0x80..0xEF channel message, 0xF0/0xF7 sysex, 0xFF meta
    data: bytes        # channel: 1-2 data bytes; sysex/meta: payload
    meta: int = -1     # meta type when status == 0xFF


@dataclass
class RawMidi:
    format: int
    ppq: int
    tracks: list[list[Event]]
    warnings: list[str] = field(default_factory=list)

    @property
    def n_note_ons(self) -> int:
        return sum(1 for t in self.tracks for e in t if e.status & 0xF0 == 0x90 and e.data[1] > 0)


def sniff(data: bytes) -> tuple[int, list[str]]:
    """Offset of ``MThd`` after unwrapping, or raise InvalidMidi for non-MIDI bodies."""
    if not data:
        raise InvalidMidi("empty")
    warnings: list[str] = []
    if data[:4] == b"RIFF" and data[8:12] == b"RMID":
        off = data.find(b"MThd", 12)
        if off < 0:
            raise InvalidMidi("no_mthd")
        return off, ["rmid_unwrapped"]
    if data[:4] == b"MThd":
        return 0, warnings
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or b"<html" in head:
        raise InvalidMidi("html")
    if head.startswith(b"<"):
        raise InvalidMidi("xml")
    if head.startswith((b"{", b"[")):
        raise InvalidMidi("json")
    # Some archives prepend a MacBinary or similar header; tolerate a short junk prefix.
    off = data.find(b"MThd", 0, 4096)
    if off < 0:
        raise InvalidMidi("no_mthd")
    return off, ["leading_junk"]


def _vlq(b: bytes, i: int, end: int) -> tuple[int, int]:
    v = 0
    for _ in range(4):
        if i >= end:
            raise IndexError("vlq past end")
        c = b[i]
        i += 1
        v = (v << 7) | (c & 0x7F)
        if not c & 0x80:
            return v, i
    raise ValueError("vlq too long")


def _parse_track(b: bytes, start: int, end: int, warnings: list[str]) -> list[Event]:
    events: list[Event] = []
    i, tick, running = start, 0, 0
    while i < end:
        try:
            delta, j = _vlq(b, i, end)
            if j >= end:
                raise IndexError("event past end")
            st = b[j]
            if st == 0xFF:
                if j + 1 >= end:
                    raise IndexError("meta type past end")
                mtype = b[j + 1]
                ln, k = _vlq(b, j + 2, end)
                if k + ln > end:
                    raise IndexError("meta payload past end")
                ev = Event(tick + delta, 0xFF, bytes(b[k:k + ln]), mtype)
                nxt = k + ln
            elif st in (0xF0, 0xF7):
                ln, k = _vlq(b, j + 1, end)
                if k + ln > end:
                    raise IndexError("sysex payload past end")
                ev = Event(tick + delta, st, bytes(b[k:k + ln]))
                nxt = k + ln
            elif st > 0xF0:
                # System common/real-time bytes are illegal in a file: the rest is garbage.
                warnings.append("illegal_status")
                break
            else:
                if st & 0x80:
                    running = st
                    k = j + 1
                elif running:
                    k = j
                else:
                    warnings.append("data_without_status")
                    break
                n = _DATA_LEN[running & 0xF0]
                if k + n > end:
                    raise IndexError("channel data past end")
                # Clip like mido(clip=True): a corrupt data byte must not become a status.
                ev = Event(tick + delta, running, bytes(x & 0x7F for x in b[k:k + n]))
                nxt = k + n
        except (IndexError, ValueError):
            warnings.append("truncated_track")
            break
        tick = ev.tick
        i = nxt
        if ev.status == 0xFF and ev.meta == META_END_OF_TRACK:
            break
        events.append(ev)
    return events


def parse(data: bytes) -> RawMidi:
    """Parse ``data`` leniently; raise InvalidMidi when it cannot be used at all."""
    off, warnings = sniff(data)
    if len(data) < off + 14:
        raise InvalidMidi("bad_header")
    hlen, fmt, ntrks, division = struct.unpack(">IHHH", data[off + 4:off + 14])
    if hlen < 6 or fmt > 2:
        raise InvalidMidi("bad_header")
    if division & 0x8000:
        raise InvalidMidi("smpte")
    if division == 0:
        raise InvalidMidi("bad_header")
    if fmt == 2:
        raise InvalidMidi("format2")
    pos = off + 8 + hlen
    tracks: list[list[Event]] = []
    n = len(data)
    while pos + 8 <= n:
        cid = data[pos:pos + 4]
        clen = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        if cid == b"MTrk":
            end = pos + 8 + clen
            if end > n:
                warnings.append("truncated_file")
                end = n
            tracks.append(_parse_track(data, pos + 8, end, warnings))
            pos = end
        elif all(0x20 <= c < 0x7F for c in cid):
            warnings.append("unknown_chunk")  # e.g. Yamaha XF 'XFIH': skip per SMF spec
            pos += 8 + clen
        else:
            # A previous chunk lied about its length: resynchronise on the next MTrk.
            nxt = data.find(b"MTrk", pos + 1)
            if nxt < 0:
                break
            warnings.append("resync")
            pos = nxt
    if not tracks:
        raise InvalidMidi("no_tracks")
    if len(tracks) != ntrks:
        warnings.append("track_count_mismatch")
    raw = RawMidi(fmt, division, tracks, sorted(set(warnings)))
    if raw.n_note_ons == 0:
        raise InvalidMidi("no_notes")
    return raw
