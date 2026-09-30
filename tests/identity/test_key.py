"""Key ensemble, key names, regions and normalization shift math."""

from __future__ import annotations

import numpy as np
import pytest

from musichistory.identity import key as k


def test_key_names_use_the_design_spelling():
    assert k.key_name(1, "major") == "Db major" and k.key_name(1, "minor") == "C# minor"
    assert k.key_name(6, "major") == "Gb major" and k.key_name(6, "minor") == "F# minor"
    assert k.key_name(10, "minor") == "Bb minor" and k.key_name(8, "minor") == "G# minor"
    assert k.key_name(3, "major") == "Eb major" and k.key_name(3, "minor") == "Eb minor"
    assert k.key_name(0, "minor") == "C minor" and k.key_name(9, "major") == "A major"


@pytest.mark.parametrize("mode", ["major", "minor"])
@pytest.mark.parametrize("norm", ["relative", "parallel"])
def test_shift_lands_on_the_target_and_stays_in_range(mode, norm):
    for tonic in range(12):
        s = k.shift_for(tonic, mode, norm)
        assert -5 <= s <= 6
        target = 9 if (mode == "minor" and norm == "relative") else 0
        assert (tonic + s) % 12 == target


def test_shift_examples():
    assert k.shift_for(9, "major") == 3            # A major -> C major
    assert k.shift_for(9, "minor") == 0            # A minor stays (relative)
    assert k.shift_for(9, "minor", "parallel") == 3
    assert k.shift_for(6, "major") == 6            # F# major -> up a tritone (range is [-5, 6])
    assert k.shift_for(5, "major") == -5           # F major -> down a fourth
    assert k.shift_for(4, "minor") == 5            # E minor -> A minor
    assert k.shift_for(6, "minor") == 3            # F# minor -> A minor, same shift as A major
    assert [k.wrap_shift(x) for x in (-6, 6, 7, 12, -12, 17)] == [6, 6, -5, 0, 0, 5]


def test_profiles_find_rotated_profiles():
    for tonic in range(12):
        assert k.profile_key(np.roll(np.array(k.TKP_MAJOR), tonic), k.TKP_MAJOR, k.TKP_MINOR) == (tonic, "major")
        assert k.profile_key(np.roll(np.array(k.KK_MINOR), tonic), k.KK_MAJOR, k.KK_MINOR) == (tonic, "minor")
    # A diatonic D major melody histogram (tonic, fifth and third weighted).
    h = np.zeros(12)
    for pc, w in {2: 6, 4: 2, 6: 4, 7: 2, 9: 5, 11: 2, 1: 1.5}.items():
        h[pc] = w
    assert k.profile_key(h, k.TKP_MAJOR, k.TKP_MINOR) == (2, "major")
    assert k.profile_key(np.zeros(12), k.TKP_MAJOR, k.TKP_MINOR) is None


def test_ensemble_agreement_and_relative_disagreement():
    est = k.ensemble({"resonance": (9, "major"), "tkp": (9, "major"), "kk": (9, "major"), "final_chord": (9, "major")})
    assert est.key == (9, "major") and est.confidence == 1.0 and not est.review and not est.ambiguous_fifth
    est = k.ensemble({"resonance": (9, "major"), "tkp": (6, "minor"), "kk": (6, "minor")})
    assert est.key == (9, "major") and not est.review and not est.ambiguous_fifth  # relative: harmless
    assert est.confidence == pytest.approx((0.45 + 0.35 * 0.3) / 0.8, abs=1e-4)


def test_ensemble_flags_fifths_and_other_disagreements():
    est = k.ensemble({"resonance": (9, "major"), "tkp": (4, "major"), "kk": (9, "major")})
    assert est.key == (9, "major") and est.ambiguous_fifth and not est.review
    est = k.ensemble({"resonance": (0, "major"), "tkp": (6, "major"), "kk": (0, "major")})
    assert est.key == (0, "major") and est.review and not est.ambiguous_fifth
    # A lone final-chord vote (0.1) is not enough to flag anything.
    est = k.ensemble({"resonance": (0, "major"), "tkp": (0, "major"), "final_chord": (3, "major")})
    assert not est.review and not est.ambiguous_fifth


def test_ensemble_ties_go_to_the_key_more_sources_voted_for():
    # Resonance a fifth off (grid error); both profiles and the final chord agree.
    est = k.ensemble({"resonance": (2, "major"), "tkp": (9, "major"), "kk": (9, "major"), "final_chord": (9, "major")})
    assert est.key == (9, "major") and est.ambiguous_fifth


def test_key_signature_vouches_for_the_collection_not_the_mode():
    # Chopin Op. 9/1: Resonance Bb minor, profiles and a (major) signature say Db.
    votes = {"resonance": (10, "minor"), "tkp": (1, "major"), "kk": (1, "major"), "keysig": (1, "major"),
             "final_chord": (10, "major")}
    assert k.ensemble(votes).key == (10, "minor")


def test_keysig_vote_ignores_c_major_and_reads_sharps_flats():
    assert k.keysig_vote({"key_signatures": [[0.0, 0, 0]]}) is None
    assert k.keysig_vote({"key_signatures": [[0.0, -2, 1]]}) == (7, "minor")   # G minor
    assert k.keysig_vote({"key_signatures": [[0.0, 3, 0]]}) == (9, "major")    # A major
    assert k.keysig_vote({"key_signatures": [[0.0, 0, 0], [4.0, 2, 0]], "end_beat": 400}) == (2, "major")
    assert k.keysig_vote(None) is None


def test_resonance_home_is_the_duration_weighted_mode():
    runs = [[0.0, 52.0, 9, False], [52.0, 136.0, 6, True], [136.0, 402.0, 9, False]]
    assert k.resonance_home(runs) == (9, "major")
    assert k.resonance_home([]) is None


def test_regions_relabel_merge_and_shift():
    runs = [[0.0, 52.0, 9, False], [52.0, 60.0, 4, False], [60.0, 136.0, 6, True], [136.0, 402.0, 9, False]]
    # The 8-beat E major blip (< 8 bars) merges into its neighbour.
    regs = k.regions(runs, (9, "major"), (9, "major"), 402.0)
    assert [(r.start, r.tonic, r.mode, r.shift) for r in regs] == [(0.0, 9, "major", 3), (60.0, 6, "minor", 3),
                                                                     (136.0, 9, "major", 3)]
    # Parallel normalization shifts F# minor to C minor.
    regs = k.regions(runs, (9, "major"), (9, "major"), 402.0, normalization="parallel")
    assert [r.shift for r in regs] == [3, 6, 3]
    # Resonance's home run takes the ensemble's home key.
    regs = k.regions([[0.0, 200.0, 2, False], [200.0, 400.0, 9, False]], (9, "major"), (2, "major"), 400.0)
    assert [(r.start, r.end, r.tonic) for r in regs] == [(0.0, 400.0, 9)]
    # No runs: one region in the home key.
    regs = k.regions([], (4, "minor"), None, 100.0)
    assert len(regs) == 1 and regs[0].shift == 5 and regs[0].shift_parallel == -4


def test_shift_map_lookup():
    regs = [k.KeyRegion(0.0, 52.0, 9, "major", 3, 3), k.KeyRegion(52.0, 100.0, 2, "major", -2, -2)]
    smap = k.ShiftMap(regs)
    assert smap.shift_at(0) == 3 and smap.shift_at(51.99) == 3 and smap.shift_at(52.0) == -2 and smap.shift_at(500) == -2
