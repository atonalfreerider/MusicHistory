"""``extract(slim, features) -> SongIdentity`` and ``SongIdentity.to_db`` (DESIGN.md §7).

Works from the slim analysis alone (``features`` optional): notes, chords, patterns,
measures, tempos and Resonance's key runs are all in the slim JSON. ``features`` (the
candidate's ``features_json``) only adds hints the sanitizer saw before stripping: track
roles from original names, the karaoke lyric-timing track, GM programs, and the original
key signature.

Order of work: key votes -> ensemble home key -> key regions and shifts -> everything else
is transposed region by region (chords, loops, lead and bass lines), so identities do not
depend on the key or tempo a transcriber happened to use.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from .. import config
from . import chords as chordmod
from . import key as keymod
from . import loops as loopmod
from . import melody as melmod
from .chords import ChordSeq
from .key import KeyEstimate, KeyRegion, ShiftMap
from .loops import Loop
from .melody import Line
from .meter import Meter

SHORT = {"Intro": "In", "Verse": "V", "Pre-Chorus": "PC", "Chorus": "C", "Post-Chorus": "PoC", "Bridge": "Br",
         "Interlude": "It", "Outro": "Out", "Refrain": "R", "Coda": "Co", "Theme": "T"}


@dataclass
class SongIdentity:
    tonic_pc: int
    mode: str
    key_name: str
    key_confidence: float
    key_ambiguous_fifth: bool
    key_review: bool
    norm_shift: int
    shift_parallel: int
    normalization: str
    key_votes: dict[str, list]
    regions: list[KeyRegion]
    chords: dict[tuple[str, str], ChordSeq]
    loops: list[Loop]
    melody: Line | None
    bass: Line | None
    interval_entropy: float
    native_bpm: float
    beats_per_bar: float
    first_downbeat: float
    n_bars: int
    n_notes: int
    duration_s: float
    end_beat: float
    style: str
    form_grammar: str
    resonance_commit: str
    main_loop: str | None
    summary: dict = field(default_factory=dict)

    def tokens(self, kind: str = "chg", level: str = "L1") -> list[int]:
        seq = self.chords.get((kind, level))
        return list(seq.tokens) if seq else []

    def song_row(self) -> dict:
        return {
            "resonance_commit": self.resonance_commit, "n_bars": self.n_bars, "n_notes": self.n_notes,
            "duration_s": self.duration_s, "end_beat": self.end_beat, "style": self.style,
            "form_grammar": self.form_grammar, "tonic_pc": self.tonic_pc, "mode": self.mode,
            "key_confidence": self.key_confidence, "key_ambiguous_fifth": int(self.key_ambiguous_fifth),
            "key_review": int(self.key_review), "norm_shift": self.norm_shift, "shift_parallel": self.shift_parallel,
            "native_bpm": self.native_bpm, "beats_per_bar": self.beats_per_bar, "first_downbeat": self.first_downbeat,
            "melody_track": self.melody.track if self.melody else None,
            "melody_channel": self.melody.channel if self.melody else None,
            "melody_method": self.melody.method if self.melody else None,
            "melody_confidence": self.melody.confidence if self.melody else None,
            "interval_entropy": self.interval_entropy, "n_melody_notes": len(self.melody) if self.melody else 0,
            "bass_track": self.bass.track if self.bass else None,
            "bass_channel": self.bass.channel if self.bass else None,
            "main_loop": self.main_loop, "summary_json": _json(self.summary),
            "normalization": self.normalization,   # the frame of key_region shifts and all tokens
        }

    def to_db(self, conn: sqlite3.Connection, work_id: str, *, commit: bool = True, **song_columns) -> None:
        """Replace this work's rows in song / key_region / chord_seq / loop / melody_line.
        ``song_columns`` supplies the stage's columns (candidate_id, midi_path,
        normalized_midi_path, patterns_path, target_bpm, ...) and may override any computed one."""
        row = {"work_id": work_id, "candidate_id": None, "midi_path": None, "normalized_midi_path": None,
               "patterns_path": None, "analysis_ok": 1, "error": None,
               "analyzed_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), **self.song_row(), **song_columns}
        if not row["midi_path"]:
            raise ValueError("to_db needs midi_path (data/songs/<work_id>/score.mid)")
        clear(conn, work_id)
        cols = list(row)
        conn.execute(f"INSERT OR REPLACE INTO song({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [row[c] for c in cols])
        conn.executemany(
            "INSERT OR REPLACE INTO key_region(work_id, start_beat, end_beat, tonic_pc, mode, shift) VALUES (?,?,?,?,?,?)",
            [(work_id, r.start, r.end, r.tonic, r.mode, r.shift) for r in self.regions])
        conn.executemany(
            "INSERT OR REPLACE INTO chord_seq(work_id, kind, level, tokens, starts, durs, downbeat) VALUES (?,?,?,?,?,?,?)",
            [(work_id, kind, level, _json(s.tokens), _json(s.starts), _json(s.durs), _json(s.downbeat))
             for (kind, level), s in self.chords.items()])
        conn.executemany(
            "INSERT OR REPLACE INTO loop(work_id, family, cycle_id, phase, rhythm_sig, cycle_tokens, roman, loop_beats,"
            " passes, visits, coverage_beats, visit_starts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(work_id, lp.family, lp.cycle_id, lp.phase, lp.rhythm_sig, _json(lp.cycle_tokens), lp.roman, lp.loop_beats,
              lp.passes, lp.visits, lp.coverage_beats, _json(lp.visit_starts)) for lp in self.loops])
        conn.executemany(
            "INSERT OR REPLACE INTO melody_line(work_id, role, onsets, durs, pitches, met) VALUES (?,?,?,?,?,?)",
            [(work_id, role, _json(ln.onsets), _json(ln.durs), _json(ln.pitches), _json(ln.met))
             for role, ln in (("melody", self.melody), ("bass", self.bass)) if ln is not None and len(ln)])
        if commit:
            conn.commit()


def clear(conn: sqlite3.Connection, work_id: str) -> None:
    """Remove a work's identity rows (not its song row)."""
    for table in ("key_region", "chord_seq", "loop", "melody_line"):
        conn.execute(f"DELETE FROM {table} WHERE work_id = ?", (work_id,))


def _json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


# --------------------------------------------------------------------------- facts
def native_bpm(slim: dict) -> float:
    """Beat-weighted median quarter-note BPM, ignoring segments below 20 or above 400 BPM."""
    tempos = slim.get("tempos") or [[0.0, 500000]]
    end = float(slim.get("end_beat") or 0.0)
    segs = []
    for i, (beat, us) in enumerate(tempos):
        nxt = tempos[i + 1][0] if i + 1 < len(tempos) else max(end, beat)
        if nxt > beat and us > 0:
            segs.append((6e7 / us, nxt - beat))
    sane = [s for s in segs if 20 <= s[0] <= 400] or segs
    if not sane:
        return round(6e7 / tempos[-1][1], 2) if tempos[-1][1] > 0 else 120.0
    half = sum(w for _, w in sane) / 2
    acc = 0.0
    for bpm, w in sorted(sane):
        acc += w
        if acc >= half - 1e-9:
            return round(bpm, 2)
    return round(sorted(sane)[-1][0], 2)


def pc_histogram(notes: list, bass_lane: tuple[int, int] | None) -> np.ndarray:
    """Duration-weighted pitch-class histogram of pitched notes, the bass lane counted double."""
    h = np.zeros(12)
    for beat, length, pitch, track, channel, _vel in notes:
        if channel == melmod.DRUMS:
            continue
        w = max(float(length), 0.0) * (2.0 if bass_lane is not None and (track, channel) == bass_lane else 1.0)
        h[pitch % 12] += w
    return h


def summary(slim: dict, ident_key: KeyEstimate, regions: list[KeyRegion], chg: ChordSeq, loops: list[Loop],
            minor_frame_tonic: int) -> dict:
    """Small display facts for the viewer HUD (never text from the MIDI file)."""
    beats: dict[int, float] = {}
    for t, d in zip(chg.tokens, chg.durs):
        beats[t] = beats.get(t, 0.0) + d
    top = sorted(beats.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    minor = ident_key.mode == keymod.MINOR
    return {
        "key": keymod.key_name(ident_key.tonic, ident_key.mode),
        "key_votes": {s: keymod.key_name(*v) for s, v in ident_key.votes.items()},
        "form": slim.get("form_grammar") or "",
        "sections": [[s["start"], s["end"], SHORT.get(s["role"], "S")] for s in slim.get("sections") or ()],
        "chord_changes": len(chg),
        "top_chords": [[loopmod.roman(t, minor_frame_tonic if minor else 0, minor), round(b, 2)] for t, b in top],
        "loops": len(loops),
        "modulations": [[r.start, keymod.key_name(r.tonic, r.mode)] for r in regions[1:]],
    }


# --------------------------------------------------------------------------- extract
def extract(slim: dict, features: dict | None = None, *, normalization: str | None = None) -> SongIdentity:
    normalization = normalization or config.NORMALIZATION
    if normalization not in ("relative", "parallel"):
        raise ValueError(f"unknown normalization {normalization!r}")
    meter = Meter.from_slim(slim)
    notes = slim.get("notes") or []
    end_beat = float(slim.get("end_beat") or 0.0)
    bpb = meter.beats_per_bar()

    # Key: votes -> ensemble -> regions.
    bass_guess = melmod.bass_notes(notes, features)
    bass_lane = None
    if bass_guess is not None:
        tn = bass_guess[0]
        bass_lane = (tn[0][3], tn[0][4]) if tn else None
    hist = pc_histogram(notes, bass_lane)
    resonance = keymod.resonance_home(slim.get("key_runs") or [])
    votes = {
        "resonance": resonance,
        "tkp": keymod.profile_key(hist, keymod.TKP_MAJOR, keymod.TKP_MINOR),
        "kk": keymod.profile_key(hist, keymod.KK_MAJOR, keymod.KK_MINOR),
        "keysig": keymod.keysig_vote(features),
        "final_chord": keymod.final_chord_vote(slim),
    }
    est = keymod.ensemble(votes)
    regs = keymod.regions(slim.get("key_runs") or [], est.key, resonance, end_beat, beats_per_bar=bpb,
                          normalization=normalization)
    smap = ShiftMap(regs)
    shift_at = smap.shift_at
    minor_frame_tonic = 9 if normalization == "relative" else 0

    # Chords and loops.
    normalized = chordmod.normalize(slim.get("chords") or [], shift_at)
    seqs = chordmod.sequences(normalized, end_beat, meter)
    loops = loopmod.from_slim(slim, shift_at, minor_frame_tonic=minor_frame_tonic)

    # Lines.
    mel = melmod.select_melody(notes, features, shift_at, meter)
    exclude = (mel.track, mel.channel) if mel is not None and mel.track is not None else None
    bass = melmod.select_bass(notes, features, shift_at, meter, exclude=exclude)

    first_note = min((n[0] for n in notes), default=0.0)
    return SongIdentity(
        tonic_pc=est.tonic, mode=est.mode, key_name=keymod.key_name(est.tonic, est.mode),
        key_confidence=est.confidence, key_ambiguous_fifth=est.ambiguous_fifth, key_review=est.review,
        norm_shift=keymod.shift_for(est.tonic, est.mode, normalization),
        shift_parallel=keymod.shift_for(est.tonic, est.mode, "parallel"), normalization=normalization,
        key_votes={s: [v[0], v[1]] for s, v in est.votes.items()},
        regions=regs, chords=seqs, loops=loops, melody=mel, bass=bass,
        interval_entropy=melmod.interval_entropy(mel.pitches) if mel else 0.0,
        native_bpm=native_bpm(slim), beats_per_bar=round(bpb, 4), first_downbeat=meter.first_downbeat(first_note),
        n_bars=int(slim.get("song_bars") or 0), n_notes=len(notes), duration_s=float(slim.get("duration_s") or 0.0),
        end_beat=end_beat, style=slim.get("style") or "", form_grammar=slim.get("form_grammar") or "",
        resonance_commit=slim.get("resonance_commit") or "",
        main_loop=loopmod.main_loop(loops, est.mode == keymod.MINOR),
        summary=summary(slim, est, regs, seqs[("chg", "L1")], loops, minor_frame_tonic))
