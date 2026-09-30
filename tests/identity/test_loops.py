"""Booth least rotation, primitive period and loop identities."""

from __future__ import annotations

import random

from id_helpers import slim

from musichistory.identity import loops


def brute_least_rotation(seq):
    n = len(seq)
    rots = [seq[i:] + seq[:i] for i in range(n)]
    best = min(rots)
    return rots.index(best)


def test_booth_matches_brute_force():
    rng = random.Random(7)
    for _ in range(2000):
        n = rng.randint(1, 12)
        seq = [rng.randint(0, 3) for _ in range(n)]
        k = loops.booth(seq)
        assert seq[k:] + seq[:k] == seq[brute_least_rotation(seq):] + seq[:brute_least_rotation(seq)]


def test_primitive_period():
    assert loops.primitive_period([0, 15, 0, 15]) == 2
    assert loops.primitive_period([0, 15, 0, 21]) == 4
    assert loops.primitive_period([7, 7, 7]) == 1
    assert loops.primitive_period([0, 21, 28, 15, 0, 21, 28, 15]) == 4


def test_rotations_share_a_cycle_and_phase_points_to_the_song_start():
    axis = [0, 21, 28, 15]  # C G Am F
    ids = set()
    for r in range(4):
        seq = axis[r:] + axis[:r]
        cycle_id, phase, rhythm = loops.identity(seq, [4.0, 4.0, 4.0, 4.0])
        ids.add(cycle_id)
        cyc = [int(t) for t in cycle_id.split(".")]
        assert cyc[phase:] + cyc[:phase] == seq
        assert rhythm == "3.3.3.3"
    assert ids == {"0.21.28.15"}
    # I-vi-IV-V (doo-wop) is a different cycle, not a rotation.
    assert loops.identity([0, 28, 15, 21], [4] * 4)[0] != "0.21.28.15"


def test_rhythm_signature_rotates_with_the_cycle():
    cycle_id, phase, rhythm = loops.identity([28, 15, 0, 21], [8.0, 4.0, 2.0, 2.0])
    assert cycle_id == "0.21.28.15" and phase == 2
    assert rhythm == "2.2.4.3"


def test_reduce_loop_wraps_collapses_and_bounds():
    # G C C G (wraps to C G), passing chord absorbed.
    steps = [(0, 4, 21), (4, 6, 0), (6, 6.5, 7), (6.5, 8, 0), (8, 12, 21)]
    toks, durs = loops.reduce_loop(steps)
    assert toks == [21, 0] and durs == [8.0, 4.0]
    assert loops.reduce_loop([(0, 4, 0), (4, 8, 15), (8, 12, 0), (12, 16, 15)]) == ([0, 15], [4.0, 4.0])
    assert loops.reduce_loop([(0, 16, 0)]) is None  # one chord
    nine = [(i * 2, i * 2 + 2, t) for i, t in enumerate([0, 3, 6, 9, 12, 15, 18, 21, 24])]
    assert loops.reduce_loop(nine) is None
    assert loops.reduce_loop([(0, 4, -1), (4, 8, 0), (8, 12, 21)]) == ([0, 21], [4.0, 4.0])


def test_roman_numerals_in_both_frames():
    assert loops.roman_seq([28, 15, 0, 21]) == "vi-IV-I-V"
    assert loops.roman_seq([28, 15, 0, 21], 9, True) == "i-VI-III-VII"
    assert loops.roman(30) == "bVII" and loops.roman(35) == "viio"
    assert loops.roman(3) == "bII" and loops.roman(4) == "bii" and loops.roman(19) == "#iv"


def test_loops_from_slim_normalize_by_the_reference_visit_region():
    # A song in A major (shift +3): loop A E F#m D = I V vi IV.
    sections = [{"first_bar": 0, "bar_count": 8, "family": 0, "role": "Chorus", "letter": "A", "start": 0.0,
                 "end": 32.0, "loops": 2, "cycle_beats": 16.0, "transpose": 0, "variation": "", "group": -1},
                {"first_bar": 8, "bar_count": 4, "family": 1, "role": "Bridge", "letter": "B", "start": 32.0,
                 "end": 48.0, "loops": 1, "cycle_beats": 16.0, "transpose": 0, "variation": "", "group": -1},
                {"first_bar": 12, "bar_count": 4, "family": 0, "role": "Chorus", "letter": "A", "start": 48.0,
                 "end": 64.0, "loops": 1, "cycle_beats": 16.0, "transpose": 0, "variation": "", "group": -1}]
    patterns = [{"family": 0, "reference": 0, "loop_bars": 4, "loop_beats": 16.0, "visits": 2, "passes": 2,
                 "role": "Chorus", "loop": [[0, 4, 9, ""], [4, 8, 4, ""], [8, 12, 6, "m"], [12, 16, 2, ""]]},
                {"family": 1, "reference": 1, "loop_bars": 4, "loop_beats": 16.0, "visits": 1, "passes": 1,
                 "role": "Bridge", "loop": [[0, 8, 2, ""], [8, 16, 4, ""]]}]
    s = slim([], [], patterns=patterns, sections=sections)
    out = loops.from_slim(s, lambda b: 3)
    assert len(out) == 1  # the bridge never repeats
    lp = out[0]
    assert lp.cycle_tokens == [0, 21, 28, 15] and lp.cycle_id == "0.21.28.15" and lp.phase == 0
    assert lp.roman == "I-V-vi-IV" and lp.loop_beats == 16.0
    assert lp.coverage_beats == 48.0 and lp.visit_starts == [0.0, 48.0]
    assert loops.main_loop(out, minor=False) == "I-V-vi-IV"
    assert loops.main_loop(out, minor=True) == "I-V-vi-IV (III-VII-i-VI)"


def _section(family, start, end, transpose=0):
    return {"first_bar": int(start // 4), "bar_count": int((end - start) // 4), "family": family, "role": "Chorus",
            "letter": "A", "start": start, "end": end, "loops": 1, "cycle_beats": 16.0, "transpose": transpose,
            "variation": "", "group": -1}


def _modulating_song():
    """C major, then D major from beat 64. The chorus's first visit (4 bars, beat 0) is in C;
    two 8-bar visits in D (transpose +2) make the 8-bar length the reference. Resonance spells
    the pattern loop in the first visit's key: C G Am F."""
    chords = [[0, 4, 0, ""], [4, 8, 7, ""], [8, 12, 9, "m"], [12, 16, 5, ""]]
    chords += [[16 + 4 * i, 20 + 4 * i, (2, 7)[i % 2], ("m", "")[i % 2]] for i in range(12)]
    chords += [[64 + 4 * i, 68 + 4 * i, (2, 9, 11, 7)[i % 4], ("", "", "m", "")[i % 4]] for i in range(16)]
    sections = [_section(0, 0.0, 16.0), _section(1, 16.0, 64.0), _section(0, 64.0, 96.0, 2),
                _section(0, 96.0, 128.0, 2)]
    patterns = [{"family": 0, "reference": 2, "loop_bars": 4, "loop_beats": 16.0, "visits": 3, "passes": 2,
                 "role": "Chorus", "loop": [[0, 4, 0, ""], [4, 8, 7, ""], [8, 12, 9, "m"], [12, 16, 5, ""]]}]
    return slim(chords, [], end_beat=128.0, key_runs=[[0.0, 64.0, 0, False], [64.0, 128.0, 2, False]],
                patterns=patterns, sections=sections)


def test_loop_is_normalized_in_the_key_resonance_spelled_it():
    """Review 'analyze' (loop key frame): the reference visit is transposed (+2) and lies in
    another key region (shift -2), but the loop is spelled in the first visit's key. The loop
    must come out as the reference visit's own chg normalization (I-V-vi-IV), not bVII-IV-v-bIII."""
    s = _modulating_song()
    out = loops.from_slim(s, lambda b: 0 if b < 64 else -2)
    assert len(out) == 1
    assert out[0].cycle_tokens == [0, 21, 28, 15] and out[0].roman == "I-V-vi-IV"
    # A missing reference falls back to the first visit (transpose 0 by construction).
    s["patterns"][0]["reference"] = -1
    assert loops.from_slim(s, lambda b: 0 if b < 64 else -2)[0].cycle_tokens == [0, 21, 28, 15]


def test_loop_tokens_match_the_reference_visits_chord_tokens_end_to_end():
    from musichistory.identity import extract

    ident = extract.extract(_modulating_song(), None, normalization="relative")
    assert [(r.start, r.tonic, r.shift) for r in ident.regions] == [(0.0, 0, 0), (64.0, 2, -2)]
    chg = ident.chords[("chg", "L1")]
    at_ref = [t for t, st in zip(chg.tokens, chg.starts) if 64 <= st < 80]
    assert at_ref == [0, 21, 28, 15]
    assert ident.loops[0].cycle_tokens == at_ref and ident.main_loop == "I-V-vi-IV"
