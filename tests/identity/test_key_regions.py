"""Evidence-based key-region cleanup (analyze v2): collections, evidence, resolve() and rehome().

The scenarios mirror the control songs the diagnosis found (docs: identity/key.py): Shape of
You's F# minor runs that are C# minor, Ice Ice Baby's parallel flips, Rain & Tears' B-flat
section relabelled a fifth off, and the real lifts (My Sweet Lord E -> F#, a final whole-step
change) that must survive."""

from __future__ import annotations

import numpy as np
import pytest

from musichistory.identity import key as k

MAJ, MIN = "major", "minor"
CHORD = {"": (0, 4, 7), "m": (0, 3, 7)}


def chord_notes(start_bar: int, chords: list[tuple[int, str]], bars: int, *, bass: bool = True) -> list[list]:
    """One chord per bar (cycled) for ``bars`` bars from ``start_bar``: a whole-bar triad on
    track 1 and the root in the bass on track 2 (4/4, slim note rows)."""
    out = []
    for i in range(bars):
        root, q = chords[i % len(chords)]
        t = (start_bar + i) * 4.0
        out += [[t, 4.0, 60 + (root + x) % 12, 1, 1, 0.7] for x in CHORD[q]]
        if bass:
            out.append([t, 4.0, 36 + root % 12, 2, 2, 0.9])
    return out


def run(start_bar, end_bar, tonic, minor):
    return [start_bar * 4.0, end_bar * 4.0, tonic, minor]


def keys(regs):
    return [(r.start, r.end, r.tonic, r.mode, r.shift) for r in regs]


# --------------------------------------------------------------------------- building blocks
def test_collections_and_relations():
    assert k.collection((0, MAJ)) == frozenset({0, 2, 4, 5, 7, 9, 11}) == k.collection((9, MIN))
    assert k.relation((0, MAJ), (0, MAJ)) == "same"
    assert k.relation((0, MAJ), (7, MAJ)) == "fifth" and k.relation((6, MIN), (1, MIN)) == "fifth"
    assert k.relation((0, MAJ), (7, MIN)) == "fifth"               # tonics a fifth apart, two accidentals
    assert k.relation((2, MIN), (2, MAJ)) == "parallel"
    assert k.relation((0, MAJ), (9, MIN)) == "relative"
    assert k.relation((2, MIN), (10, MAJ)) == "adjacent"           # D minor vs B-flat: one accidental
    assert k.relation((4, MAJ), (6, MAJ)) is None                  # a whole-step lift is not related
    assert k.relation((10, MAJ), (0, MAJ)) is None
    assert k.near((0, MAJ), (7, MAJ)) and k.near((2, MIN), (10, MAJ)) and not k.near((0, MAJ), (7, MIN))


def test_evidence_counts_only_the_pitch_classes_one_collection_explains():
    h = np.zeros(12)
    for pc, w in {0: 8, 2: 4, 4: 6, 5: 3, 7: 7, 9: 4, 11: 2}.items():   # C major, F natural, no F#
        h[pc] = w
    ea, eb = k.evidence(h, (0, MAJ), (7, MAJ))
    assert ea == pytest.approx(3 / 34) and eb == 0.0
    assert k.decisive(h, (0, MAJ), (7, MAJ)) and not k.decisive(h, (7, MAJ), (0, MAJ))
    # A minor key's leading tone never counts against it: A harmonic minor vs A major.
    h = np.zeros(12)
    for pc, w in {9: 8, 11: 3, 0: 5, 2: 3, 4: 6, 5: 2, 8: 3}.items():    # G# leading tone, C and F
        h[pc] = w
    ea, eb = k.evidence(h, (9, MAJ), (9, MIN))
    assert ea == 0.0 and eb == pytest.approx(7 / 30)
    assert k.decisive(h, (9, MIN), (9, MAJ)) and not k.decisive(h, (9, MAJ), (9, MIN))
    # Collections one accidental apart need the other side (almost) silent: 10 % F vs 3 % F#.
    h = np.zeros(12)
    for pc, w in {0: 25, 2: 10, 4: 15, 5: 10, 6: 3, 7: 20, 9: 10, 11: 7}.items():
        h[pc] = w
    assert not k.decisive(h, (0, MAJ), (7, MAJ))
    h[6] = 1.5                                                            # F# down to 1.5 %
    assert k.decisive(h, (0, MAJ), (7, MAJ))
    assert k.evidence(np.zeros(12), (0, MAJ), (7, MAJ)) == (0.0, 0.0)


def test_note_histogram_counts_capped_durations_by_onset_and_skips_drums():
    notes = [[0.0, 1.0, 60, 1, 1, 0.5], [1.0, 9.0, 64, 1, 1, 0.5], [4.0, 1.0, 67, 1, 1, 0.5],
             [2.0, 1.0, 38, 9, 10, 0.5]]                       # drums on channel 10
    hist = k.note_histogram(notes)
    h = hist(0.0, 4.0)
    assert h[0] == 1.0 and h[4] == 4.0 and h[7] == 0.0 and h[2] == 0.0   # [start, end), capped at 4 beats
    assert hist(4.0, 8.0)[7] == 1.0 and hist(0.0, 100.0).sum() == 6.0 and hist(10.0, 20.0).sum() == 0.0


def test_rehome_rescores_the_votes_and_flags_the_fifth():
    votes = {"resonance": (6, MIN), "tkp": (6, MIN), "kk": (6, MIN), "final_chord": (1, MAJ)}
    est = k.ensemble(votes)
    assert est.key == (6, MIN) and not est.ambiguous_fifth
    new = k.rehome(est, (1, MIN))
    assert new.key == (1, MIN) and new.ambiguous_fifth and new.votes == est.votes and new.review == est.review
    assert new.confidence == pytest.approx((0.8 * 0.5 + 0.1 * 0.2) / 0.9, abs=1e-4)
    assert k.rehome(est, est.key) is est


# --------------------------------------------------------------------------- control-song scenarios
E_MAJOR_LOOP = [(1, "m"), (6, "m"), (9, ""), (11, "")]          # C#m F#m A B: the E-major collection


def shape_of_you_notes() -> list[list]:
    """The loop plus the hook (E F# G# F#, the No Scrubs figure) on track 3, every bar."""
    hook = [[b * 4.0 + i, 1.0, p, 3, 3, 1.0] for b in range(92) for i, p in enumerate((64, 66, 68, 66))]
    return chord_notes(0, E_MAJOR_LOOP, 92) + hook


def test_shape_of_you_runs_that_never_use_d_are_c_sharp_minor_and_the_home_follows():
    """Shape of You: Resonance calls 66 % of the song F# minor, but the notes use D# (the B
    chord) and never D: C# minor, one region, so every hook lands at the same degrees."""
    notes = shape_of_you_notes()
    runs = [run(0, 27, 6, True), run(27, 50, 1, True), run(50, 67, 6, True), run(67, 75, 1, False), run(75, 92, 6, True)]
    home, regs = k.resolve(runs, (6, MIN), (6, MIN), 368.0, hist=k.note_histogram(notes),
                           proposals=[(6, MIN), (6, MIN), (6, MIN), (1, MAJ)])
    assert home == (1, MIN)
    assert keys(regs) == [(0.0, 368.0, 1, MIN, -4)]
    assert regs[0].shift_parallel == -1
    # Without the notes only the length rules apply: the 8-bar C# major run merges, the rest stays.
    home, regs = k.resolve(runs, (6, MIN), (6, MIN), 368.0)
    assert home == (6, MIN) and [(r.tonic, r.mode) for r in regs] == [(6, MIN), (1, MIN), (6, MIN)]


def test_parallel_flips_without_thirds_merge_into_the_home_mode():
    """Ice Ice Baby: D major runs of 12 and 8.5 bars (the riff, D and A only) inside D minor."""
    riff = [[b * 4.0 + x, 0.5, 38, 2, 2, 0.9] for b in range(113) for x in (0, 0.5, 1, 1.5, 2)]
    riff += [[b * 4.0 + 2.5, 0.5, 45, 2, 2, 0.9] for b in range(113)]
    minor_part = chord_notes(12, [(2, "m"), (7, ""), (10, ""), (7, "m")], 82, bass=False)
    minor_part += chord_notes(103, [(2, "m"), (7, ""), (10, ""), (7, "m")], 10, bass=False)
    runs = [[0.0, 48.0, 2, False], [48.0, 377.0, 2, True], [377.0, 411.0, 2, False], [411.0, 449.3, 2, True]]
    home, regs = k.resolve(runs, (2, MIN), (2, MIN), 449.3, hist=k.note_histogram(riff + minor_part),
                           proposals=[(2, MIN), (7, MIN), (2, MAJ)])
    assert home == (2, MIN) and keys(regs) == [(0.0, 449.3, 2, MIN, -5)]


def test_length_and_support_decide_which_related_regions_survive():
    """C major home, a G major region: kept when its notes use F# (from 8 bars), kept without
    evidence from 16 bars (Resonance's label is the support), merged when shorter, and always
    merged when its notes use F and never F#."""
    c_part = chord_notes(0, [(0, ""), (5, ""), (7, ""), (0, "")], 40)
    g_real = chord_notes(40, [(7, ""), (2, ""), (0, ""), (7, "")], 40)               # D major chord: F#
    g_neutral = chord_notes(40, [(7, ""), (0, ""), (7, ""), (0, "")], 40)            # G and C chords only
    g_false = chord_notes(40, [(7, ""), (5, ""), (0, ""), (7, "")], 40)              # F major chord: F
    c_again = chord_notes(80, [(0, ""), (5, ""), (7, ""), (0, "")], 40)

    def regs(g_notes, g_bars):
        runs = [run(0, 40, 0, False), run(40, 40 + g_bars, 7, False), run(40 + g_bars, 120, 0, False)]
        home, rr = k.resolve(runs, (0, MAJ), (0, MAJ), 480.0, hist=k.note_histogram(c_part + g_notes + c_again))
        assert home == (0, MAJ)
        return [(r.tonic, r.mode) for r in rr]

    assert regs(g_real, 8) == [(0, MAJ), (7, MAJ), (0, MAJ)]        # supported: 8 bars are enough
    assert regs(g_neutral, 16) == [(0, MAJ), (7, MAJ), (0, MAJ)]    # neutral: 16 bars are enough
    assert regs(g_neutral, 12) == [(0, MAJ)]                        # neutral and short: merged
    assert regs(g_false, 40) == [(0, MAJ)]                          # contradicted: merged however long


def test_resonance_home_runs_keep_their_key_when_the_notes_say_so_and_the_lift_survives():
    """Rain & Tears: Resonance's home run is B-flat (with E-flat, never E); the ensemble said F
    (the profile of B-flat + C averages to F). The B-flat section keeps its key, the D minor
    intro (E-flat too) joins it, the home becomes B-flat, and the final whole-step lift to C
    major is a real modulation."""
    bb = [(10, ""), (5, ""), (7, "m"), (2, "m"), (3, ""), (10, ""), (3, ""), (5, "")]   # Pachelbel in B-flat
    c = [(0, ""), (7, ""), (9, "m"), (4, "m"), (5, ""), (0, ""), (5, ""), (7, "")]      # ... and in C
    notes = chord_notes(0, bb, 41) + chord_notes(41, c, 21)
    runs = [run(0, 11, 2, True), run(11, 41, 10, False), run(41, 62, 0, False)]
    home, regs = k.resolve(runs, (5, MAJ), (10, MAJ), 248.0, hist=k.note_histogram(notes),
                           proposals=[(10, MAJ), (5, MAJ), (5, MAJ), (0, MAJ)])
    assert home == (10, MAJ)
    assert keys(regs) == [(0.0, 164.0, 10, MAJ, 2), (164.0, 248.0, 0, MAJ, 0)]
    # Without note evidence the old rule stands: Resonance's home run takes the ensemble's key.
    _, regs = k.resolve(runs, (5, MAJ), (10, MAJ), 248.0)
    assert [(r.tonic, r.mode) for r in regs] == [(5, MAJ), (0, MAJ)]


def test_real_lifts_are_never_merged():
    """My Sweet Lord: E major then F# major (a whole step up) both stay, with their own shifts."""
    notes = chord_notes(0, [(4, ""), (11, ""), (9, ""), (1, "m")], 52) + chord_notes(52, [(6, ""), (1, ""), (11, ""), (3, "m")], 78)
    runs = [run(0, 52, 4, False), run(52, 130, 6, False)]
    home, regs = k.resolve(runs, (6, MAJ), (6, MAJ), 520.0, hist=k.note_histogram(notes), proposals=[(6, MAJ), (1, MAJ)])
    assert home == (6, MAJ) and keys(regs) == [(0.0, 208.0, 4, MAJ, -4), (208.0, 520.0, 6, MAJ, 6)]
    # A final whole-step change of only 8 bars survives too.
    notes = chord_notes(0, [(0, ""), (5, ""), (7, ""), (0, "")], 40) + chord_notes(40, [(2, ""), (7, ""), (9, ""), (2, "")], 8)
    _, regs = k.resolve([run(0, 40, 0, False), run(40, 48, 2, False)], (0, MAJ), (0, MAJ), 192.0,
                        hist=k.note_histogram(notes))
    assert [(r.tonic, r.shift) for r in regs] == [(0, 0), (2, -2)]


def test_relative_regions_need_a_clear_tonic_to_stay():
    """An A minor stretch inside C major has the same notes; it stays only when long and its
    profile clearly says A minor (the A minor tonic and dominant weigh most)."""
    c_part = chord_notes(0, [(0, ""), (5, ""), (7, ""), (0, "")], 20)
    a_clear = chord_notes(20, [(9, "m"), (4, "m"), (9, "m"), (2, "m")], 20)
    a_vague = chord_notes(20, [(0, ""), (9, "m"), (5, ""), (7, "")], 20)
    runs = [run(0, 20, 0, False), run(20, 40, 9, True)]
    _, regs = k.resolve(runs, (0, MAJ), (0, MAJ), 160.0, hist=k.note_histogram(c_part + a_clear))
    assert [(r.tonic, r.mode) for r in regs] == [(0, MAJ), (9, MIN)]
    _, regs = k.resolve(runs, (0, MAJ), (0, MAJ), 160.0, hist=k.note_histogram(c_part + a_vague))
    assert [(r.tonic, r.mode) for r in regs] == [(0, MAJ)]


def test_resolve_is_deterministic_and_ignores_the_order_of_proposals():
    notes = shape_of_you_notes()
    runs = [run(0, 27, 6, True), run(27, 50, 1, True), run(50, 67, 6, True), run(67, 75, 1, False), run(75, 92, 6, True)]
    props = [(6, MIN), (1, MAJ), (4, MAJ), (11, MAJ)]
    first = k.resolve(runs, (6, MIN), (6, MIN), 368.0, hist=k.note_histogram(notes), proposals=props)
    for _ in range(3):
        assert k.resolve(runs, (6, MIN), (6, MIN), 368.0, hist=k.note_histogram(notes), proposals=props[::-1]) == first
    assert k.regions(runs, (6, MIN), (6, MIN), 368.0, hist=k.note_histogram(notes), proposals=props) == first[1]
