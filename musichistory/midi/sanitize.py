"""Rewrite a parsed MIDI file as a canonical, text-free SMF type 1 (DESIGN.md §5 steps 3-7).

Why each step exists (see the Resonance PatternPrep failure modes in the research notes):

* lyrics/text/markers/copyright/instrument names/sequencer data are removed: lyrics must
  never be stored, and marker text replaces PatternPrep's repetition analysis with the
  transcriber's labels;
* **all** key signatures are removed after recording them: one bogus signature is only a
  hint, but two or more switch PatternPrep's key inference off;
* type-0 files are split into one track per channel, so every part gets its own lane;
* track names become neutral ``T<nn> <role>`` labels (no credits or titles leak through,
  and PatternPrep's name-based lane roles still work);
* a track whose name says drums is moved to channel 10, where NAudio expects drums, when
  its notes look like a drum part (at most 12 distinct keys, all in 27-87); GM pitched
  patch names such as "Percussive Organ", "Steel Drums" or "Melodic Tom" never count as
  drums (or melody) names;
* hanging notes are closed, a note-on at the final tick is dropped and ``end_of_track`` is
  padded one beat past the last note-off: NAudio/PatternPrep throw otherwise.

Pre-strip facts (key signatures, lyric tick positions, name-derived roles) are returned in
``Facts``. Lyric and text payloads are never decoded; track names are decoded in memory
only to look for role keywords and are then dropped.
"""

from __future__ import annotations

import re
import struct
from collections import Counter
from dataclasses import dataclass, field

from .validate import (
    META_INSTRUMENT,
    META_KEY_SIGNATURE,
    META_LYRIC,
    META_MARKER,
    META_TEMPO,
    META_TEXT,
    META_TIME_SIGNATURE,
    META_TRACK_NAME,
    Event,
    RawMidi,
)

ROLES = ("vocal", "melody", "lead", "bass", "drums", "backing", "piano", "guitar", "strings", "other")
DRUM_CHANNEL = 9  # 0-based; channel 10 in the 1..16 convention

# General MIDI names of *pitched* patches that contain a drums or melody keyword: Percussive
# Organ (17), the Chromatic Percussion family (8-15), Steel Drums (114), Melodic Tom (117),
# Reverse Cymbal (119). Transcribers often name a track after its patch ("PercOrgan",
# "STEELDRUM", "Melodic Toms"), so these phrases are cut out of a name before the role
# patterns run: the phrase itself never makes a track 'drums' or 'melody', and a name with
# nothing else in it takes its role from the program.
_GM_MELODIC = re.compile(
    r"perc\w*\.?[\s._/-]*org\w*"            # Percussive Organ, Perc. Org, PercOrgan, Percusive Organ
    r"|chrom\w*\.?[\s._/-]*perc\w*"         # Chromatic Percussion, Chrom. Perc
    r"|st(?:ee)?l[\s._-]*dru?ms?"           # Steel Drum(s), STEELDRUM, StlDrum
    r"|steel[\s._-]*pans?"                  # Steel Pan(s), the same patch
    r"|mel\w*\.?[\s._-]*toms?\b"            # Melodic Tom(s), Mel. Tom
    r"|rev\w*\.?[\s._-]*cym\w*"             # Reverse Cymbal, RevCymbal
)

# Order matters: the first pattern that matches a (lower-cased) name wins.
_NAME_ROLES: list[tuple[str, re.Pattern[str]]] = [
    ("drums", re.compile(r"drum|perc|snare|kick|hi-?hat|cymbal|conga|bongo|tambourine|shaker|cowbell|\bkit\b")),
    ("backing", re.compile(r"back|bkg|bgv|\bbg\b|backup|harmon|choir|chorus|\booh|\baah")),
    ("vocal", re.compile(r"vocal|voice|\bvox|\bvoc\b|\bsing|singer|\blead vo")),
    ("melody", re.compile(r"melod|\btune\b|\btheme\b|\bmel\b")),
    ("bass", re.compile(r"bass(?!oon)|\bbs\b")),
    ("lead", re.compile(r"\blead|\bsolo")),
    ("piano", re.compile(r"piano|\bpno|rhodes|\bkeys\b|keyboard|\bclav|wurli|\be\.? ?p(iano)?\b")),
    ("guitar", re.compile(r"guit|\bgtr|\bgt\b|strat|acoustic|electric|nylon|steel")),
    ("strings", re.compile(r"string|violin|viola|cello|\bstr\b|\bstrs\b|orch|fiddle|harp")),
]

GM_FAMILIES = (
    "piano", "chromatic_percussion", "organ", "guitar", "bass", "strings", "ensemble", "brass",
    "reed", "pipe", "synth_lead", "synth_pad", "synth_effects", "ethnic", "percussive", "sound_effects",
)


def gm_family(program: int) -> str:
    return GM_FAMILIES[max(0, min(127, program)) // 8]


def program_role(program: int) -> str:
    fam = gm_family(program)
    if fam in ("piano", "guitar", "bass", "strings"):
        return fam
    if fam == "ensemble":
        return "backing" if 52 <= program <= 54 else ("strings" if program <= 51 else "other")
    if fam == "synth_lead":
        return "lead"
    return "other"


def name_role(names: list[bytes]) -> str | None:
    """Role keyword found in a track's name/instrument events; the text is then dropped.

    GM pitched-patch names ("Percussive Organ", "Steel Drums", "Melodic Toms") are removed
    first, so they yield no role (the program decides) instead of 'drums' or 'melody'.
    """
    for raw in names:
        s = _GM_MELODIC.sub(" ", raw[:64].decode("latin-1").lower())
        for role, pat in _NAME_ROLES:
            if pat.search(s):
                return role
    return None


@dataclass
class Facts:
    ppq: int
    key_signatures: list[list[float]]           # [beat, sharps_flats, minor] from the original
    lyric_ticks: list[int]                      # tick positions only; text never kept
    lyric_events: int
    text_events: int
    marker_events: int
    karaoke: bool
    track_roles: list[tuple[str, str]]          # per sanitized track: (role, 'name'|'channel'|'program'|'none')
    warnings: list[str] = field(default_factory=list)


@dataclass
class Sanitized:
    data: bytes
    facts: Facts


# ------------------------------------------------------------------ writer helpers
def _vlq(v: int) -> bytes:
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append(0x80 | (v & 0x7F))
        v >>= 7
    return bytes(reversed(out))


def _meta(mtype: int, payload: bytes) -> bytes:
    return bytes((0xFF, mtype)) + _vlq(len(payload)) + payload


def _write_smf(ppq: int, tracks: list[list[tuple[int, bytes]]]) -> bytes:
    out = bytearray(b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), ppq))
    for tr in tracks:
        body = bytearray()
        last = 0
        for tick, ev in tr:
            body += _vlq(tick - last) + ev
            last = tick
        out += b"MTrk" + struct.pack(">I", len(body)) + body
    return bytes(out)


# Priorities for events sharing a tick: offs first, then conductor meta, then
# program/controller changes, then note-ons.
_P_OFF, _P_META, _P_CTRL, _P_ON = 0, 1, 2, 3


@dataclass
class _Track:
    events: list[Event]
    name_role: str | None
    sysex: list[Event] = field(default_factory=list)


def _collect_facts(raw: RawMidi) -> tuple[list[list[float]], list[int], int, int, int, bool, list[str | None]]:
    ppq = raw.ppq
    keys: list[list[float]] = []
    lyric_ticks: list[int] = []
    kar_ticks: list[int] = []
    n_lyric = n_text = n_marker = 0
    kar = False
    roles: list[str | None] = []
    for tr in raw.tracks:
        names: list[bytes] = []
        tagged = False
        text_ticks: list[int] = []
        for e in tr:
            if e.status != 0xFF:
                continue
            m = e.meta
            if m == META_KEY_SIGNATURE and len(e.data) == 2:
                sf = struct.unpack("b", e.data[:1])[0]
                if -7 <= sf <= 7 and e.data[1] in (0, 1):
                    row = [round(e.tick / ppq, 4), sf, e.data[1]]
                    if row not in keys:
                        keys.append(row)
            elif m == META_LYRIC:
                n_lyric += 1
                lyric_ticks.append(e.tick)
            elif m == META_TEXT:
                n_text += 1
                # Soft Karaoke (.kar): '@' tags mark the words track; syllables follow as text.
                if e.data[:1] == b"@":
                    tagged = True
                    kar = kar or e.data[:2].upper() == b"@K"
                else:
                    text_ticks.append(e.tick)
            elif m == META_MARKER:
                n_marker += 1
            elif m in (META_TRACK_NAME, META_INSTRUMENT):
                names.append(e.data)
        if tagged:
            kar_ticks.extend(text_ticks)
        roles.append(name_role(names))
    ticks = sorted(set(lyric_ticks) | (set(kar_ticks) if kar else set()))
    karaoke = kar or n_lyric >= 16
    keys.sort(key=lambda r: r[0])
    return keys, ticks, n_lyric, n_text, n_marker, karaoke, roles


def _repair_notes(events: list[Event], file_last: int, ppq: int, counts: Counter) -> list[tuple[int, int, int, bytes]]:
    """Pair note-ons/offs; returns (tick, priority, seq, bytes) rows for one track.

    Pairing is first-in first-out per (channel, key), like NAudio, so a new note-on written
    just before the previous note's note-off at the same tick keeps both notes intact.
    """
    rows: list[tuple[int, int, int, bytes]] = []
    open_notes: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    notes: list[tuple[int, int, int, int, int, int]] = []  # on, off, ch, pitch, vel, seq
    for seq, e in enumerate(events):
        hi, ch = e.status & 0xF0, e.status & 0x0F
        if hi == 0x90 and e.data[1] > 0:
            q = open_notes.setdefault((ch, e.data[0]), [])
            if q and q[-1][0] == e.tick:
                counts["duplicate_note_on"] += 1  # same key struck twice at one tick
                continue
            q.append((e.tick, e.data[1], seq))
        elif hi in (0x80, 0x90):
            q = open_notes.get((ch, e.data[0]))
            if not q:
                counts["orphan_note_off"] += 1
            else:
                on, vel, s = q.pop(0)
                notes.append((on, e.tick, ch, e.data[0], vel, s))
        else:
            rows.append((e.tick, _P_CTRL, seq, bytes((e.status,)) + e.data))
    # Unterminated notes end at the next strike of the same key, at most 4 beats later.
    strikes: dict[tuple[int, int], list[int]] = {}
    for on, _, ch, pitch, _, _ in notes:
        strikes.setdefault((ch, pitch), []).append(on)
    for key, q in open_notes.items():
        for on, vel, s in q:
            if on >= file_last:
                counts["final_tick_note_on_dropped"] += 1
                continue
            later = [t for t in strikes.get(key, ()) if t > on] + [t for t, _, _ in q if t > on]
            off = min([file_last, on + 4 * ppq, *later])
            notes.append((on, off, key[0], key[1], vel, s))
            counts["hanging_notes_closed"] += 1
    min_len = max(1, ppq // 32)
    for on, off, ch, pitch, vel, seq in notes:
        if off <= on:
            off = on + min_len
            counts["zero_length_notes"] += 1
        rows.append((on, _P_ON, seq, bytes((0x90 | ch, pitch, vel))))
        rows.append((off, _P_OFF, seq, bytes((0x80 | ch, pitch, 0))))
    return rows


def _dominant_channel(events: list[Event]) -> int | None:
    c = Counter(e.status & 0x0F for e in events if e.status & 0xF0 == 0x90 and e.data[1] > 0)
    return c.most_common(1)[0][0] if c else None


# A drum part played on a melodic channel strikes a few kit keys; a pitched part (organ
# chords, a steel-drum tune) spreads over more keys or leaves the GM/GS drum-key range.
DRUM_KEYS = range(27, 88)       # GM2/GS drum map, High Q .. Open Surdo
DRUM_MAX_DISTINCT_KEYS = 12


def looks_like_drums(events: list[Event], channel: int) -> bool:
    """The note-ons on ``channel`` use at most 12 distinct keys, all inside 27-87."""
    keys = {e.data[0] for e in events if e.status == (0x90 | channel) and e.data[1] > 0}
    return 0 < len(keys) <= DRUM_MAX_DISTINCT_KEYS and all(k in DRUM_KEYS for k in keys)


def sanitize(raw: RawMidi) -> Sanitized:
    ppq = raw.ppq
    warnings = list(raw.warnings)
    keys, lyric_ticks, n_lyric, n_text, n_marker, karaoke, name_roles = _collect_facts(raw)

    # Conductor material from every track: tempo and time signatures (last one per tick wins,
    # as when a player merges tracks in order), plus sysex from tracks without channel data.
    tempo: dict[int, bytes] = {}
    timesig: dict[int, bytes] = {}
    conductor_sysex: list[Event] = []
    tracks: list[_Track] = []
    bad_meta = 0
    for ti, tr in enumerate(raw.tracks):
        chan = [e for e in tr if e.status < 0xF0]
        # Only complete, well-formed sysex messages survive (mido and NAudio reject the rest).
        sysex = [e for e in tr if e.status == 0xF0 and e.data[-1:] == b"\xf7" and max(e.data[:-1], default=0) < 0x80]
        if any(e.status in (0xF0, 0xF7) for e in tr) and len(sysex) != sum(e.status in (0xF0, 0xF7) for e in tr):
            bad_meta += 1
        for e in tr:
            if e.status != 0xFF:
                continue
            if e.meta == META_TEMPO:
                if len(e.data) == 3 and int.from_bytes(e.data, "big") > 0:
                    tempo[e.tick] = e.data
                else:
                    bad_meta += 1
            elif e.meta == META_TIME_SIGNATURE:
                if len(e.data) >= 2 and 1 <= e.data[0] <= 32 and e.data[1] <= 6:
                    cc, bb = (e.data[2], e.data[3]) if len(e.data) >= 4 else (24, 8)
                    timesig[e.tick] = bytes((e.data[0], e.data[1], cc or 24, bb or 8))
                else:
                    bad_meta += 1
        if chan:
            tracks.append(_Track(chan, name_roles[ti], sysex))
        else:
            conductor_sysex.extend(sysex)
    if bad_meta:
        warnings.append(f"bad_meta_dropped:{bad_meta}")

    # Split type 0 (or a lone multi-channel track) into one track per channel.
    if len(tracks) == 1 and len({e.status & 0x0F for e in tracks[0].events}) > 1:
        only = tracks[0]
        by_ch: dict[int, list[Event]] = {}
        for e in only.events:
            by_ch.setdefault(e.status & 0x0F, []).append(e)
        tracks = [_Track(evs, None) for _, evs in sorted(by_ch.items())]
        conductor_sysex.extend(only.sysex)
        warnings.append("type0_split")
    elif raw.format == 0 and tracks:
        tracks[0].name_role = None  # a type-0 track name is the song title, not a role

    # Drum tracks named as such but sitting on another channel go to channel 10, but only
    # when their notes look like a drum part: a pitched part under a drum-like name stays on
    # its channel and takes its role from the program.
    for t in tracks:
        if t.name_role == "drums":
            dom = _dominant_channel(t.events)
            if dom is None or dom == DRUM_CHANNEL:
                continue
            if looks_like_drums(t.events, dom):
                t.events = [
                    Event(e.tick, (e.status & 0xF0) | DRUM_CHANNEL, e.data) if e.status & 0x0F == dom else e
                    for e in t.events
                ]
                warnings.append("drums_to_ch10")
            else:
                t.name_role = None
                warnings.append("drum_name_pitched_kept")

    file_last = max((e.tick for t in tracks for e in t.events if e.status & 0xF0 in (0x80, 0x90)), default=0)
    counts: Counter = Counter()
    rows_per_track = [_repair_notes(t.events, file_last, ppq, counts) for t in tracks]
    for name in ("hanging_notes_closed", "final_tick_note_on_dropped", "zero_length_notes",
                 "orphan_note_off", "duplicate_note_on"):
        if counts[name]:
            warnings.append(f"{name}:{counts[name]}")

    last_off = max((r[0] for rows in rows_per_track for r in rows if r[1] == _P_OFF), default=0)
    end_tick = last_off + ppq

    # Roles and program lookup for the neutral track names.
    first_prog: dict[int, int] = {}
    for rows in rows_per_track:
        for tick, prio, _, b in sorted(rows):
            if b[0] & 0xF0 == 0xC0:
                first_prog.setdefault(b[0] & 0x0F, b[1])
    roles: list[tuple[str, str]] = [("other", "none")]  # conductor
    out_tracks: list[list[tuple[int, bytes]]] = []
    for t, rows in zip(tracks, rows_per_track):
        ons = Counter(b[0] & 0x0F for _, p, _, b in rows if p == _P_ON)
        if not ons:
            role, src = "other", "none"
        else:
            ch = ons.most_common(1)[0][0]
            if ch == DRUM_CHANNEL:
                role, src = "drums", ("name" if t.name_role == "drums" else "channel")
            elif t.name_role and t.name_role != "drums":
                role, src = t.name_role, "name"
            else:
                prog = next((b[1] for _, _, _, b in sorted(rows) if b[0] == (0xC0 | ch)), first_prog.get(ch, 0))
                role, src = program_role(prog), "program"
        roles.append((role, src))
        rows = [r for r in rows if r[0] <= last_off]  # trailing controllers after the music
        rows += [(e.tick, _P_CTRL, -1, bytes((0xF0,)) + _vlq(len(e.data)) + e.data)
                 for e in t.sysex if e.tick <= last_off]
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        out_tracks.append([(r[0], r[3]) for r in rows])

    conductor = [(0, _P_META, _meta(META_TRACK_NAME, b"T00 other"))]
    conductor += [(t, _P_META, _meta(META_TIME_SIGNATURE, v)) for t, v in timesig.items() if t <= last_off]
    conductor += [(t, _P_META, _meta(META_TEMPO, v)) for t, v in tempo.items() if t <= last_off]
    conductor += [(e.tick, _P_CTRL, bytes((0xF0,)) + _vlq(len(e.data)) + e.data)
                  for e in conductor_sysex if e.tick <= last_off]
    conductor.sort(key=lambda r: (r[0], r[1]))
    final: list[list[tuple[int, bytes]]] = [[(t, b) for t, _, b in conductor]]
    for i, (evs, (role, _)) in enumerate(zip(out_tracks, roles[1:]), start=1):
        final.append([(0, _meta(META_TRACK_NAME, f"T{i:02d} {role}".encode()))] + evs)
    for tr in final:
        tr.append((end_tick, _meta(0x2F, b"")))

    facts = Facts(ppq, keys, lyric_ticks, n_lyric, n_text, n_marker, karaoke, roles, sorted(set(warnings)))
    return Sanitized(_write_smf(ppq, final), facts)
