"""Chord token encodings (db.py) and the chg / cd / beat / keyfree sequences."""

from __future__ import annotations

from musichistory.identity import chords as ch
from musichistory.identity.key import KeyRegion, ShiftMap
from musichistory.identity.meter import Meter

METER = Meter([(0.0, 4, 4, 100)])


def test_l1_l2_encodings_match_db_docs():
    assert ch.l1(0, "") == 0 and ch.l1(0, "7") == 0 and ch.l1(0, "maj7") == 0
    assert ch.l1(9, "m") == 28 and ch.l1(9, "m7") == 28
    assert ch.l1(11, "dim") == 35
    assert ch.l2(0, "") == 0 and ch.l2(0, "m") == 1 and ch.l2(7, "7") == 44
    assert ch.l2(0, "maj7") == 3 and ch.l2(9, "m7") == 58 and ch.l2(11, "dim") == 71
    assert {ch.l1(r, q) for r in range(12) for q in ch.L1_Q} == set(range(36))
    assert {ch.l2(r, q) for r in range(12) for q in ch.L2_Q} == set(range(72))


def test_duration_classes_and_cd_tokens():
    assert [ch.dur_class(b) for b in (0.25, 0.5, 1, 1.5, 2, 3, 4, 8, 16, 64)] == [0, 0, 1, 2, 2, 3, 3, 4, 5, 5]
    assert ch.cd_token(28, 4.0) == 28 * 8 + 3


def test_keyfree_tokens():
    c, am, f = ch.l1(0, ""), ch.l1(9, "m"), ch.l1(5, "")
    assert ch.keyfree_token(c, am) == 9 * 9 + 0 * 3 + 1
    assert ch.keyfree_token(am, f) == 8 * 9 + 1 * 3 + 0
    assert {ch.keyfree_token(a, b) for a in range(36) for b in range(36)} == set(range(108))


def test_changes_drop_rests_absorb_passing_chords_and_collapse():
    chords = [(0, 4, -1, ""), (4, 8, 0, ""), (8, 8.5, 2, "m"), (8.5, 12, 0, "7"), (12, 16, -1, ""),
              (16, 20, 0, ""), (20, 24, 9, "m"), (24, 28, 5, "maj7")]
    l1 = ch.changes(chords, "L1", METER)
    assert l1.tokens == [0, 28, 15]
    assert l1.starts == [4.0, 20.0, 24.0] and l1.durs == [16.0, 4.0, 4.0]
    assert l1.downbeat == [1, 1, 1]
    l2 = ch.changes(chords, "L2", METER)  # C and C7 differ at L2
    assert l2.tokens == [0, 2, 0, 1 + 54, 5 * 6 + 3]
    assert l2.durs[0] == 4.5  # the passing Dm is absorbed by the chord it follows


def test_cd_and_keyfree_sequences():
    chords = [(0, 4, 0, ""), (4, 6, 7, ""), (6, 8, 9, "m"), (8, 16, 5, "")]
    seqs = ch.sequences(chords, 16.0, METER)
    assert seqs[("chg", "L1")].tokens == [0, 21, 28, 15]
    assert seqs[("cd", "L1")].tokens == [0 * 8 + 3, 21 * 8 + 2, 28 * 8 + 2, 15 * 8 + 4]
    assert seqs[("keyfree", "L1")].tokens == [7 * 9 + 0, 2 * 9 + 1, 8 * 9 + 3]
    assert seqs[("keyfree", "L1")].starts == [4.0, 6.0, 8.0]
    assert set(seqs) == set(ch.KINDS)


def test_beat_tokens_take_the_majority_chord():
    chords = [(0, 1.75, 0, ""), (1.75, 3, 7, ""), (3, 4, -1, "")]
    beat = ch.per_beat(chords, "L1", 4.0, METER)
    assert beat.tokens == [0, 0, 21, -1]  # beat 1 is 3/4 C, 1/4 G
    assert beat.downbeat == [1, 0, 0, 0]


def test_normalize_uses_the_region_shift_at_each_chord():
    regions = [KeyRegion(0.0, 8.0, 9, "major", 3, 3), KeyRegion(8.0, 16.0, 11, "major", 1, 1)]
    smap = ShiftMap(regions)
    out = ch.normalize([[0, 4, 9, ""], [4, 8, 4, ""], [8, 12, 11, ""], [12, 16, -1, ""]], smap.shift_at)
    assert [c[2] for c in out] == [0, 7, 0, -1]
