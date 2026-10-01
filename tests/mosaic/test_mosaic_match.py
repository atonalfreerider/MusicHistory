"""Note-for-note span matching on synthetic melodies: exact under transposition and tempo folds,
the onset tolerance, wrong notes lowering the rate, the tempo range, and the cover preferring
one long piece over several short ones of equal coverage."""

from __future__ import annotations

import numpy as np

from mosaic_helpers import RHYTHM, RNG, TUNE, melody, transform
from musichistory.mosaic import assemble, match
from musichistory.mosaic.match import Index, Seq, find_pieces, match_notes

W = dict(length_bonus=assemble.LENGTH_BONUS, miss_cost=assemble.MISS_COST, piece_cost=assemble.PIECE_COST)


def noise(n: int, work_id: str, start: float = 0.0, seed: int = 1) -> Seq:
    rng = np.random.default_rng(seed)
    return melody(list(rng.integers(50, 75, n)), list(rng.choice([0.5, 1.0, 1.5], n)), start=start, work_id=work_id)


def concat(*seqs: Seq, work_id: str = "S", ibi: float = 0.5) -> Seq:
    return Seq(work_id, np.concatenate([s.on for s in seqs]), np.concatenate([s.off for s in seqs]),
               np.concatenate([s.pitch for s in seqs]), ibi)


def test_exact_match_under_shift_and_fold():
    target = melody(TUNE, RHYTHM, gap=0.1)
    source = transform(target, fold=2.0, offset=3.0, transpose=5, ibi=1.0)
    partner = match_notes(target, source, 2.0, 3.0, 5)
    assert list(partner) == list(range(len(target)))
    # a different transposition matches nothing
    assert (match_notes(target, source, 2.0, 3.0, 4) < 0).all()


def test_find_pieces_recovers_the_alignment_in_a_longer_song():
    target = melody(TUNE, RHYTHM, gap=0.1, ibi=0.5)
    core = transform(target, fold=0.5, offset=-10.0, transpose=-7)       # source beats = 2 * (t + 10)
    before = noise(12, "S", start=0.0, seed=3)
    after = noise(10, "S", start=float(core.off.max()) + 1.0, seed=4)
    source = concat(before, core, after, ibi=0.25)                      # x0.5 fold, tempo ratio 1.0
    assert core.on.min() > before.off.max()
    pieces = find_pieces(target, Index({"S": source}), ibi_target=0.5, **W)
    best = max(pieces, key=lambda p: p.weight)
    assert (best.a, best.b) == (0, len(target) - 1)
    assert best.rate == 1.0 and best.matched == len(target)
    assert best.fold == 0.5 and best.transpose == -7 and abs(best.offset + 10.0) < 1e-6
    assert best.shift == -7 + 12 and best.octave == -1                   # audio shift folded into -6..6
    assert abs(best.tempo_ratio - 1.0) < 1e-9


def test_onset_tolerance():
    target = melody(TUNE, RHYTHM, gap=0.1)
    for jitter, expect in ((0.1, True), (0.2, False)):
        signs = RNG.choice([-1.0, 1.0], len(target))
        source = Seq("S", target.on + signs * jitter, target.off + signs * jitter, target.pitch, 0.5)
        # re-centre: the offset is estimated from the matched notes, so test against a fixed one
        partner = match_notes(target, source, 1.0, 0.0, 0)
        assert bool((partner >= 0).all()) is expect


def test_durations_must_be_similar():
    target = melody(TUNE, RHYTHM, gap=0.1)
    short = Seq("S", target.on, target.on + 0.05, target.pitch, 0.5)     # staccato blips under long notes
    partner = match_notes(target, short, 1.0, 0.0, 0)
    long_notes = (target.off - target.on) > 0.5
    assert (partner[long_notes] < 0).all()


def test_wrong_and_extra_notes_lower_the_rate():
    target = melody(TUNE, RHYTHM, gap=0.1)
    pitches = target.pitch.copy()
    pitches[5] += 1                                                       # one wrong note
    source = Seq("S", target.on, target.off, pitches, 0.5)
    pieces = find_pieces(target, Index({"S": source}), ibi_target=0.5, **W)
    whole = [p for p in pieces if (p.a, p.b) == (0, len(target) - 1)]
    assert whole and whole[0].matched == len(target) - 1
    assert abs(whole[0].rate - (len(target) - 1) / len(target)) < 1e-9


def test_tempo_ratio_outside_range_is_not_used():
    target = melody(TUNE, RHYTHM, gap=0.1, ibi=0.5)
    # same beat positions, but the source's beats last 1.0 s: fold 1 would play it at x2.0
    source = transform(target, fold=1.0, transpose=2, ibi=1.0)
    assert match.allowed_folds(1.0, 0.5) == [2.0]
    assert find_pieces(target, Index({"S": source}), ibi_target=0.5, **W) == []


def test_longer_piece_preferred_over_short_ones_with_equal_coverage():
    target = melody(TUNE[:12], RHYTHM[:12], gap=0.1)
    long_src = transform(target, transpose=3, work_id="LONG")
    corpus = {"LONG": long_src}
    for k in range(3):                                                    # three exact 4-note pieces
        part = Seq("P", target.on[4 * k:4 * k + 4], target.off[4 * k:4 * k + 4], target.pitch[4 * k:4 * k + 4], 0.5)
        corpus[f"SHORT{k}"] = transform(part, offset=float(target.on[4 * k]) - 2.0, transpose=-2 + k,
                                        work_id=f"SHORT{k}")
    pieces = find_pieces(target, Index(corpus), ibi_target=0.5, **W)
    assert {p.work_id for p in pieces} >= {"LONG", "SHORT0", "SHORT1", "SHORT2"}
    chosen = assemble.cover(assemble.prune(pieces), len(target))
    assert [(p.work_id, p.a, p.b) for p in chosen] == [("LONG", 0, 11)]
    # the weight itself: one 12-note piece beats three 4-note ones, all exact
    w = match.piece_weight
    assert w(12, 12, **W) > 3 * w(4, 4, **W)


def test_seed_keys_are_transposition_and_tempo_invariant():
    target = melody(TUNE, RHYTHM)
    moved = transform(target, fold=2.0, offset=5.0, transpose=-4)
    assert match.ngram_keys(target) == match.ngram_keys(moved)


def test_chanted_loops_and_static_pieces_are_not_melodies():
    from musichistory.mosaic import search

    tune = melody(TUNE, RHYTHM)
    assert search.melody_problem(tune) == ""
    chant = melody([60, 60, 60, 62, 60, 60, 60, 60, 62, 62, 62, 60, 64, 64, 64, 65], RHYTHM)
    assert "chanted" in search.melody_problem(chant)
    narrow = melody([60, 62, 60, 62, 64, 62, 60, 62] * 2, RHYTHM)
    assert "too few pitches" in search.melody_problem(narrow)
    static = melody([60, 60, 60, 60, 60, 62, 60, 60] + TUNE[:8], RHYTHM)
    p = piece_like(0, 7)
    assert not search.melodic_piece(static, p)
    assert search.melodic_piece(static, piece_like(8, 15))


def piece_like(a: int, b: int) -> match.Piece:
    return match.Piece("S", a, b, a, b, 1.0, 0.0, 0, b - a + 1, b - a + 1, b - a + 1, 1.0)


def test_unrounded_pitches_do_not_flip_at_a_rounding_boundary():
    t = Seq("T", [0.0, 1.0, 2.0, 3.0], [0.8, 1.8, 2.8, 3.8], [63, 64, 62, 60], 0.5, fpitch=[63.45, 63.6, 62.0, 60.0])
    s = Seq("S", [0.0, 1.0, 2.0, 3.0], [0.8, 1.8, 2.8, 3.8], [64, 63, 62, 61], 0.5, fpitch=[63.55, 63.4, 62.0, 60.6])
    partner = match_notes(t, s, 1.0, 0.0, 0)
    # 63.45 ~ 63.55 and 63.6 ~ 63.4 match although they round apart; 60.0 vs 60.6 does not
    assert list(partner) == [0, 1, 2, -1]


def test_fold_shift_keeps_the_fewest_octaves():
    assert [match.fold_shift(t) for t in (-18, -7, -6, 0, 6, 7, 18)] == [-6, 5, -6, 0, 6, -5, 6]
    p = piece_like(0, 3)
    p.transpose = 18
    assert (p.shift, p.octave) == (6, 1)


def test_pieces_span_at_least_a_bar():
    target = melody(TUNE, RHYTHM, gap=0.1)
    part = Seq("S", target.on[:4], target.off[:4], target.pitch[:4], 0.5)     # 4 notes over 3 beats
    idx = Index({"S": part})
    assert find_pieces(target, idx, ibi_target=0.5, **W)
    assert find_pieces(target, idx, ibi_target=0.5, min_beats=4.0, **W) == []
    five = Seq("S", target.on[:5], target.off[:5], target.pitch[:5], 0.5)     # ... the 5th holds to beat 4
    assert {(p.a, p.b) for p in find_pieces(target, Index({"S": five}), ibi_target=0.5, min_beats=4.0, **W)} \
        >= {(0, 4)}


def test_long_spans_may_match_a_little_less():
    target = melody(TUNE, RHYTHM, gap=0.1)                    # 16 notes over 12 beats
    pitches = target.pitch.copy()
    pitches[[3, 5, 7, 10, 12]] += 2                           # 5 wrong notes: 11/16 = 0.69 < MIN_RATE
    source = Seq("S", target.on, target.off, pitches, 0.5)
    idx = Index({"S": source})
    whole = lambda ps: [p for p in ps if (p.a, p.b) == (0, 15)]
    assert not whole(find_pieces(target, idx, ibi_target=0.5, **W))
    got = whole(find_pieces(target, idx, ibi_target=0.5, long_beats=8.0, long_rate=0.6, **W))
    assert got and abs(got[0].rate - 11 / 16) < 1e-9


def test_ornaments_do_not_count_against_a_piece():
    target = melody(TUNE[:8], RHYTHM[:8], gap=0.1)
    grace = Seq("G", target.on[[3, 6]] - 0.1, target.on[[3, 6]] - 0.02, target.pitch[[3, 6]] + 1, 0.5)  # grace notes
    split = Seq("R", target.on + 0.5 * (target.off - target.on), target.off, target.pitch, 0.5)  # re-sung halves
    for extra in (grace, split):
        src = Seq("S", np.r_[target.on, extra.on], np.r_[target.on + 0.5 * (target.off - target.on) - 0.01, extra.off]
                  if extra is split else np.r_[target.off, extra.off], np.r_[target.pitch, extra.pitch], 0.5)
        ps = [p for p in find_pieces(target, Index({"S": src}), ibi_target=0.5, **W) if (p.a, p.b) == (0, 7)]
        assert ps and ps[0].rate == 1.0, extra.work_id


def test_spoken_loops_are_not_melodies():
    from musichistory.mosaic import search

    tune = melody(TUNE, RHYTHM)
    spoken = Seq("T", tune.on, tune.off, tune.pitch, 0.5, fpitch=tune.pitch + 0.3)   # between semitones
    assert "spoken" in search.melody_problem(spoken)
    assert "spoken" in search.melody_problem(tune, np.full(len(tune), 0.7))          # unsteady
    assert search.melody_problem(tune, np.full(len(tune), 0.95)) == ""
