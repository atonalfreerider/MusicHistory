"""Harmony consonance scoring: a voice a third away beats a doubling, a tritone or second away
is dissonant on the strong beats, and the search finds a planted harmony span among decoys."""

from __future__ import annotations

import numpy as np

from mosaic_helpers import RHYTHM, TUNE, melody
from musichistory.mosaic import harmony
from musichistory.mosaic.match import Seq

C, G, AM, F = 0, 14, 19, 10          # chord labels: root*2 + minor


def ctx_for(target: Seq, bars: int = 4) -> harmony.LoopContext:
    return harmony.context(target, bars * 4, 4, [[C, C], [G, G], [AM, AM], [F, F]][:bars], 0.5)


def target_melody() -> Seq:
    return melody(TUNE, RHYTHM, gap=0.05)          # 16 notes over 12 beats


def shifted(seq: Seq, semis) -> Seq:
    s = np.asarray(semis) if np.ndim(semis) else np.full(len(seq), semis)
    return Seq("V", seq.on, seq.off, seq.pitch + s, seq.ibi)


SCALE = [0, 2, 4, 5, 7, 9, 11]


def third_below(p: int) -> int:
    """The diatonic (C major) third under a scale tone."""
    octave, pc = divmod(int(p), 12)
    k = SCALE.index(pc)
    return octave * 12 + SCALE[k - 2] - (12 if k < 2 else 0)


def padded(seq: Seq, until: float = 40.0) -> Seq:
    """The melody plus one far-away note, so the song runs on past any loop window."""
    return Seq(seq.work_id, np.r_[seq.on, until], np.r_[seq.off, until + 0.5], np.r_[seq.pitch, 40], seq.ibi)


def score_of(target: Seq, voice: Seq) -> dict:
    ctx = ctx_for(target)
    W = harmony.roll(voice, ctx.n_cells, harmony.CELL)[None, :]
    ev = harmony.evaluate(ctx, W)
    j = harmony.SHIFTS.index(0)
    return {k: float(v[0, j]) for k, v in ev.items() if k != "H"}


def test_consonance_table():
    cons = harmony.CONSONANCE
    assert cons[3] == cons[4] == cons[8] == cons[9] == 1.0          # thirds and sixths
    assert cons[7] == cons[5] == 0.8                                  # fifth / fourth
    assert cons[0] < 0.5                                              # doubling is not a harmony
    assert cons[1] == cons[6] == cons[11] == 0.0


def test_third_beats_doubling_and_dissonance():
    t = target_melody()
    third = score_of(t, shifted(t, -4))           # constant major third below
    octave = score_of(t, shifted(t, -12))
    unison = score_of(t, shifted(t, 0))
    tritone = score_of(t, shifted(t, 6))
    second = score_of(t, shifted(t, 2))
    assert third["consonance"] == 1.0 and third["doubling"] == 0.0 and third["coverage"] > 0.95
    assert octave["doubling"] == 1.0 and unison["doubling"] == 1.0
    assert third["score"] > octave["score"] and third["score"] > unison["score"]
    assert tritone["consonance"] == 0.0 and tritone["strong"] > 0.5
    assert abs(second["consonance"] - 0.2) < 1e-9 and second["strong"] > 0.5
    assert third["score"] > max(tritone["score"], second["score"]) + 0.5
    assert third["spread"] == 4.0


def test_low_coverage_is_not_a_voice():
    t = target_melody()
    sparse = Seq("V", t.on[:3], t.off[:3], t.pitch[:3] - 4, 0.5)
    assert score_of(t, sparse)["score"] == -np.inf


def test_search_finds_the_planted_voice():
    t = target_melody()
    # a diatonic third below the tune sits 6 source beats into a song, sung 2 semitones low, at
    # half the target's beat rate (fold 2: one source beat spans 2 loop beats)
    voice_pitch = np.array([third_below(p) for p in t.pitch]) - 2
    planted = padded(Seq("H", 6.0 + t.on / 2, 6.0 + t.off / 2, voice_pitch, 1.0))
    rng = np.random.default_rng(3)
    rolls = {"H": (harmony.source_roll(planted), 1.0)}
    for k in range(5):
        n = 40
        dec = melody(list(rng.integers(52, 72, n)), list(rng.choice([0.5, 1.0], n)), work_id=f"D{k}")
        rolls[f"D{k}"] = (harmony.source_roll(padded(dec)), 0.5)
    ctx = harmony.context(t, 16, 4, [[C, C], [G, G], [AM, AM], [F, F]], 0.5)
    best = harmony.search(ctx, rolls)[0]
    assert best.work_id == "H" and best.fold == 2.0 and best.start == 6.0
    assert abs(best.tempo_ratio - 1.0) < 1e-9
    assert best.consonance == 1.0 and best.doubling == 0.0 and best.strong == 0.0
    # sung 2 low, the planted thirds come back at +2 (a fourth lower, -3, makes sixths: as good)
    assert best.shift in (2, -3)
    heard = harmony.heard_notes(planted, best, 16)
    assert harmony.consonance_of(t, heard, 16) == 1.0


def test_second_voice_avoids_doubling_the_first():
    t = target_melody()
    v1 = Seq("A", t.on, t.off, t.pitch - 4, 0.5)
    v1_copy = Seq("B", t.on, t.off, t.pitch - 4, 0.5)        # would double voice 1
    v2 = Seq("C", t.on, t.off, t.pitch + 3, 0.5)              # a third above instead
    rolls = {w: (harmony.source_roll(padded(s)), 0.5) for w, s in (("A", v1), ("B", v1_copy), ("C", v2))}
    ctx = ctx_for(t)
    vs = harmony.voices(ctx, rolls, min_score=0.0)
    assert len(vs) == 2 and vs[0].work_id != vs[1].work_id
    a, b = vs[0].cells, vs[1].cells
    both = (a >= 0) & (b >= 0)
    assert both.any() and np.mean((a[both] - b[both]) % 12 == 0) < 0.2       # no doubling of voice 1
    assert np.mean(harmony.CONSONANCE[(a[both] - b[both]) % 12]) >= 0.8 - 1e-9  # and consonant with it
