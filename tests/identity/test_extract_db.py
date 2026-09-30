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
    assert roles == {"melody", "bass"}
    mel = conn.execute("SELECT * FROM melody_line WHERE work_id='Q1' AND role='melody'").fetchone()
    assert len(json.loads(mel["onsets"])) == len(json.loads(mel["pitches"])) == len(json.loads(mel["met"]))
    # Re-running replaces instead of duplicating.
    ident.to_db(conn, "Q1", midi_path="data/songs/Q1/score.mid")
    assert conn.execute("SELECT COUNT(*) FROM chord_seq WHERE work_id='Q1'").fetchone()[0] == 7
    assert conn.execute("SELECT COUNT(*) FROM melody_line WHERE work_id='Q1'").fetchone()[0] == 2


def test_no_text_from_the_file_reaches_the_identity():
    s = minor_song()
    ident = extract.extract(s, None)
    blob = json.dumps(ident.summary) + (ident.main_loop or "") + ident.form_grammar
    assert "Lyrics" not in blob and "lyric" not in blob.lower()
