"""Sentence splitting at pauses and the falling-F0 check, on synthetic voiced tones."""

import numpy as np

from musichistory.narration import inflection

SR = 24000


def tone(f0a, f0b, seconds, sr=SR):
    f = np.linspace(f0a, f0b, int(seconds * sr))
    ph = 2 * np.pi * np.cumsum(f) / sr
    return (sum(np.sin(k * ph) / k for k in range(1, 6)) * 0.2).astype(np.float32)


def gap(seconds, sr=SR):
    return np.zeros(int(seconds * sr), np.float32)


def test_pauses_and_sentence_ends():
    # a comma pause (0.15 s) inside sentence one, a sentence pause (0.45 s) between the sentences
    y = np.concatenate([gap(0.1), tone(130, 125, 0.6), gap(0.15), tone(125, 110, 0.7), gap(0.45),
                        tone(120, 100, 0.9), gap(0.2)])
    assert len(inflection.pauses(y, SR)) == 2
    ends = inflection.sentence_ends(y, SR, 2)
    assert len(ends) == 2
    assert abs(ends[0] - (0.1 + 0.6 + 0.15 + 0.7)) < 0.05
    assert abs(ends[1] - (len(y) / SR - 0.2)) < 0.05


def test_falling_and_rising_ends():
    y = np.concatenate([gap(0.2), tone(130, 110, 1.0), gap(0.4), tone(115, 145, 1.0), gap(0.2)])
    r = inflection.check(y, SR, 2)
    assert r["of"] == 2 and r["measured"] == 2 and r["falls"] == 1 and not r["ok"]
    first, second = r["ends"]
    assert first["falls"] and first["slope_st_per_s"] < -1.5
    assert not second["falls"] and second["slope_st_per_s"] > 2


def test_all_falling_is_ok():
    y = np.concatenate([gap(0.2), tone(140, 115, 1.0), gap(0.4), tone(130, 105, 1.2), gap(0.2)])
    r = inflection.check(y, SR, 2)
    assert r["ok"] and r["falls"] == 2


def test_unvoiced_end_is_not_counted_as_falling():
    rng = np.random.default_rng(0)
    y = np.concatenate([gap(0.2), (rng.standard_normal(SR) * 0.1).astype(np.float32), gap(0.2)])
    r = inflection.check(y, SR, 1)
    assert r["falls"] == 0 and not r["ok"]


def test_final_creak_counts_as_a_fall():
    # a modal ending, then creak on pYIN's floor: the tail anchors on the modal voicing
    y = np.concatenate([gap(0.2), tone(112, 104, 0.8), tone(47, 47, 0.3), gap(0.2)])
    e = inflection.measure_end(y, SR, len(y) / SR - 0.2)
    assert e.falls and e.slope < 0
