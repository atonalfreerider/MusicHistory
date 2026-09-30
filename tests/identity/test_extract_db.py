"""extract() on synthetic slim analyses, and SongIdentity.to_db."""

from __future__ import annotations

import json

from id_helpers import slim

from musichistory import db
from musichistory.identity import extract
from musichistory.identity.chords import l1


def minor_song(tonic=9, bars=32):
    """i-VI-III-VII in the given minor key, a tune on track 3, bass on track 2, drums."""
    prog = [(0, "m"), (8, ""), (3, ""), (10, "")]
    chords, notes = [], []
    for b in range(bars):
        off, q = prog[b % 4]
        root = (tonic + off) % 12
        chords.append([b * 4.0, b * 4.0 + 4, root, q])
        third = 3 if q == "m" else 4
        notes += [[b * 4.0, 4.0, 48 + root + x, 1, 1, 0.6] for x in (0, third, 7)]
        notes += [[b * 4.0, 2.0, 36 + root, 2, 2, 0.9], [b * 4.0 + 2, 2.0, 36 + root, 2, 2, 0.9]]
        scale = [0, 2, 3, 5, 7, 8, 10]
        notes += [[b * 4.0 + k, 1.0, 69 + tonic - 9 + scale[(b + k * 2) % 7], 3, 3, 1.0] for k in range(4)]
        notes += [[b * 4.0 + k, 0.1, 36, 9, 10, 1.0] for k in range(4)]
    patterns = [{"family": 0, "reference": 0, "loop_bars": 4, "loop_beats": 16.0, "visits": 1, "passes": bars // 4,
                 "role": "Verse", "loop": [[i * 4.0, i * 4.0 + 4, (tonic + o) % 12, q] for i, (o, q) in enumerate(prog)]}]
    return slim(chords, notes, end_beat=bars * 4.0, key_runs=[[0.0, bars * 4.0, tonic, True]], patterns=patterns)


def test_extract_minor_song_relative_and_parallel():
    s = minor_song(tonic=4)  # E minor
    rel = extract.extract(s, None, normalization="relative")
    assert (rel.tonic_pc, rel.mode, rel.key_name) == (4, "minor", "E minor")
    assert rel.norm_shift == 5 and rel.shift_parallel == -4
    assert rel.tokens()[:4] == [l1(9, "m"), l1(5, ""), l1(0, ""), l1(7, "")]  # Am F C G
    assert rel.loops[0].cycle_id == "0.21.28.15" and rel.main_loop == "vi-IV-I-V (i-VI-III-VII)"
    assert rel.melody.method == "classifier" and rel.melody.track == 3
    assert rel.bass.track == 2 and rel.bass.pitches[0] == 36 + 4 + 5
    assert rel.native_bpm == 120.0 and rel.beats_per_bar == 4.0 and rel.first_downbeat == 0.0
    par = extract.extract(s, None, normalization="parallel")
    assert par.norm_shift == -4 and par.tokens()[:4] == [l1(0, "m"), l1(8, ""), l1(3, ""), l1(10, "")]
    # Transposition invariance: the same song in G minor gives identical normalized identities.
    g = extract.extract(minor_song(tonic=7), None, normalization="relative")
    assert g.tokens() == rel.tokens() and g.melody.pitches == rel.melody.pitches
    assert [lp.cycle_id for lp in g.loops] == [lp.cycle_id for lp in rel.loops]


def test_native_bpm_is_the_beat_weighted_median_without_absurd_segments():
    s = minor_song()
    s["tempos"] = [[0.0, 2581], [4.0, 500000], [100.0, 400000]]  # 23246 BPM header, 120, 150
    assert extract.native_bpm(s) == 120.0
    s["tempos"] = [[0.0, 500000], [10.0, 400000]]
    assert extract.native_bpm(s) == 150.0


def test_to_db_writes_and_replaces_rows(tmp_path):
    conn = db.connect(tmp_path / "p.sqlite")
    ident = extract.extract(minor_song(), None)
    ident.to_db(conn, "Q1", candidate_id=7, midi_path="data/songs/Q1/score.mid",
                normalized_midi_path="data/normalized/Q1.mid", patterns_path="data/songs/Q1/analysis.json")
    row = conn.execute("SELECT * FROM song WHERE work_id='Q1'").fetchone()
    assert row["analysis_ok"] == 1 and row["candidate_id"] == 7 and row["tonic_pc"] == 9 and row["mode"] == "minor"
    assert row["norm_shift"] == 0 and row["shift_parallel"] == 3 and row["melody_method"] == "classifier"
    assert row["main_loop"].startswith("vi-IV-I-V") and json.loads(row["summary_json"])["key"] == "A minor"
    kinds = {(r["kind"], r["level"]) for r in conn.execute("SELECT kind, level FROM chord_seq WHERE work_id='Q1'")}
    assert kinds == {("chg", "L1"), ("chg", "L2"), ("cd", "L1"), ("cd", "L2"), ("beat", "L1"), ("beat", "L2"),
                     ("keyfree", "L1")}
    chg = conn.execute("SELECT * FROM chord_seq WHERE work_id='Q1' AND kind='chg' AND level='L1'").fetchone()
    assert json.loads(chg["tokens"])[:4] == [28, 15, 0, 21] and len(json.loads(chg["downbeat"])) == len(json.loads(chg["tokens"]))
    assert conn.execute("SELECT COUNT(*) FROM key_region WHERE work_id='Q1'").fetchone()[0] == 1
    assert conn.execute("SELECT cycle_id FROM loop WHERE work_id='Q1'").fetchone()[0] == "0.21.28.15"
    roles = {r[0] for r in conn.execute("SELECT role FROM melody_line WHERE work_id='Q1'")}
    # Analyze v2 also stores every other pitched lane with melodic content: here the chord
    # track's top voice (track 1, channel 1: 32 triads whose fifth follows i-VI-III-VII).
    assert roles == {"melody", "bass", "lane:1:1"}
    mel = conn.execute("SELECT * FROM melody_line WHERE work_id='Q1' AND role='melody'").fetchone()
    assert len(json.loads(mel["onsets"])) == len(json.loads(mel["pitches"])) == len(json.loads(mel["met"]))
    # Re-running replaces instead of duplicating.
    ident.to_db(conn, "Q1", midi_path="data/songs/Q1/score.mid")
    assert conn.execute("SELECT COUNT(*) FROM chord_seq WHERE work_id='Q1'").fetchone()[0] == 7
    assert conn.execute("SELECT COUNT(*) FROM melody_line WHERE work_id='Q1'").fetchone()[0] == 3


def test_no_text_from_the_file_reaches_the_identity():
    s = minor_song()
    ident = extract.extract(s, None)
    blob = json.dumps(ident.summary) + (ident.main_loop or "") + ident.form_grammar
    assert "Lyrics" not in blob and "lyric" not in blob.lower()


def shape_of_you_like():
    """C#m F#m A B, one chord a bar (pad track 1, bass track 2) and the E F# G# F# hook on track 3,
    92 bars; Resonance's runs call two thirds of it F# minor (it never plays a D, but plays D#)."""
    loop = [(1, "m"), (6, "m"), (9, ""), (11, "")]
    chords, notes = [], []
    for b in range(92):
        root, q = loop[b % 4]
        third = 3 if q == "m" else 4
        chords.append([b * 4.0, b * 4.0 + 4, root, q])
        notes += [[b * 4.0, 4.0, 60 + (root + x) % 12, 1, 1, 0.6] for x in (0, third, 7)]
        notes.append([b * 4.0, 4.0, 36 + root, 2, 2, 0.9])
        notes += [[b * 4.0 + i, 1.0, p, 3, 3, 1.0] for i, p in enumerate((64, 66, 68, 66))]
    runs = [[0.0, 108.0, 6, True], [108.0, 200.0, 1, True], [200.0, 268.0, 6, True], [268.0, 300.0, 1, False],
            [300.0, 368.0, 6, True]]
    return slim(chords, notes, end_beat=368.0, key_runs=runs)


def test_extract_cleans_regions_and_rehomes_on_pitch_evidence():
    ident = extract.extract(shape_of_you_like(), None)
    assert ident.key_votes["resonance"] == [6, "minor"]                  # Resonance's home was F# minor
    assert (ident.tonic_pc, ident.mode, ident.key_name) == (1, "minor", "C# minor")
    assert ident.key_ambiguous_fifth and ident.norm_shift == -4 and ident.shift_parallel == -1
    assert [(r.start, r.end, r.shift) for r in ident.regions] == [(0.0, 368.0, -4)]
    assert ident.summary["key"] == "C# minor" and ident.summary["modulations"] == []
    # The hook E F# G# F# is stored at the same degrees (C D E D) in every bar.
    assert {tuple(ident.melody.pitches[i:i + 4]) for i in range(0, len(ident.melody), 4)} == {(60, 62, 64, 62)}
    assert ident.melody.track == 3 and ident.bass.track == 2


def test_extract_stores_lanes_normalized_like_the_lead(tmp_path):
    s = minor_song(tonic=4)                                              # E minor: shift +5
    ident = extract.extract(s, None)
    assert [(ln.track, ln.channel, ln.method) for ln in ident.lanes] == [(1, 1, "lane")]
    pad = ident.lanes[0]
    assert len(pad) == 32 and pad.pitches[:4] == [48 + (4 + o) % 12 + 7 + 5 for o in (0, 8, 3, 10)]
    assert [r for r, _ in ident.lines()] == ["melody", "bass", "lane:1:1"]
    conn = db.connect(tmp_path / "p.sqlite")
    ident.to_db(conn, "Q2", midi_path="data/songs/Q2/score.mid")
    row = conn.execute("SELECT * FROM melody_line WHERE work_id='Q2' AND role='lane:1:1'").fetchone()
    assert json.loads(row["pitches"]) == pad.pitches and json.loads(row["onsets"]) == pad.onsets
    assert json.loads(row["met"])[:2] == [0, 0]
