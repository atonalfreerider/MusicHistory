"""Plan math: mirrors Morph.Plan / MorphPlan in unity/Assets/MusicHistory/Contracts/ISongPlayer.cs
with the heard (measured) key and tempo, and the contract's output-time glide."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory.paths.plan import FOLD_OCTAVES, MORPH_BARS, StepPlan, fold_bpm, plan_step, smoothstep, wrap  # noqa: E402


def unity_wrap(s: int) -> int:           # Morph.Wrap, C# % keeps the dividend's sign
    r = int(math.fmod(s, 12))
    return int(math.fmod(r + 12 + 6, 12)) - 6


@pytest.mark.parametrize("s", range(-30, 31))
def test_wrap_matches_unity_and_range(s):
    assert wrap(s) == unity_wrap(s)
    assert -6 <= wrap(s) <= 5
    assert (wrap(s) - s) % 12 == 0


def test_fold_matches_unity_examples():
    # ISongPlayer.cs: "70 -> 140 is not a ramp; 125 -> 87 starts at 125".
    assert fold_bpm(70, 140) == pytest.approx(140)
    assert fold_bpm(125, 87) == pytest.approx(125)
    assert fold_bpm(180, 88) == pytest.approx(90)            # beyond 0.8 octave: halved
    assert fold_bpm(150, 88) == pytest.approx(150)           # 0.77 octave: ramped literally
    assert fold_bpm(0, 100) == pytest.approx(100)            # nothing heard: no change


def test_fold_threshold_is_unitys():
    assert FOLD_OCTAVES == 0.8 and MORPH_BARS == 2.0
    f = 100.0
    just_in = f * 2 ** 0.79
    just_out = f * 2 ** 0.81
    assert fold_bpm(just_in, f) == pytest.approx(just_in)
    assert fold_bpm(just_out, f) == pytest.approx(just_out / 2)


def test_first_step_plays_natively():
    p = plan_step(None, None, 5, 96.0, beats_per_bar=4)
    assert (p.start_semitones, p.start_bpm, p.morph_seconds, p.start_ratio) == (0, 96.0, 0.0, 1.0)
    assert p.commands() == []


def test_step_starts_in_previous_key_and_heard_tempo():
    # Previous clip ends in C (0) at 100 BPM; this one is in D (2) at 120 BPM.
    p = plan_step(0, 100.0, 2, 120.0, beats_per_bar=4)
    assert p.start_semitones == wrap(0 - 2) == -2           # D played 2 down = C
    assert p.start_bpm == pytest.approx(100.0)
    assert p.start_ratio == pytest.approx(100 / 120)        # Unity's StartTempoRatio = best / f
    assert p.morph_seconds == pytest.approx(2 * 4 * 60 / 100)   # morph_bars bars at the start tempo


def test_morph_seconds_uses_the_meter():
    p = plan_step(0, 90.0, 0, 90.0, beats_per_bar=3)
    assert p.morph_seconds == pytest.approx(2 * 3 * 60 / 90)
    assert p.is_identity


def test_glide_endpoints_and_monotonic():
    p = StepPlan(4, 90.0, 120.0, 4.0)
    T = p.morph_seconds
    assert p.semitones_at(0) == pytest.approx(4) and p.semitones_at(T) == pytest.approx(0)
    assert p.bpm_at(0) == pytest.approx(90) and p.bpm_at(T) == pytest.approx(120)
    assert p.bpm_at(T + 5) == pytest.approx(120)
    vals = [p.bpm_at(T * k / 50) for k in range(51)]
    assert all(b >= a - 1e-12 for a, b in zip(vals, vals[1:]))
    assert p.semitones_at(T / 2) == pytest.approx(2)        # smoothstep(0.5) = 0.5
    assert smoothstep(-1) == 0 and smoothstep(2) == 1


def test_input_time_integrates_the_tempo_ratio():
    p = StepPlan(-3, 132.0, 100.0, 4.0)
    T = p.morph_seconds
    n = 20000
    dt = (T + 2) / n
    acc = 0.0
    for k in range(n):
        acc += p.ratio_at((k + 0.5) * dt) * dt
    assert p.input_time(T + 2) == pytest.approx(acc, rel=1e-6)
    # continuous at the glide's end, native speed afterwards
    assert p.input_time(T) == pytest.approx(T * (1 + p.start_ratio) / 2)
    assert p.input_time(T + 1) - p.input_time(T) == pytest.approx(1.0)


def test_commands_follow_the_glide():
    p = plan_step(7, 110.0, 2, 100.0, beats_per_bar=4)      # wrap(5) = 5 semitones up
    cmds = p.commands(step=0.05)
    assert p.start_semitones == 5
    times = [c[0] for c in cmds]
    assert all(b > a for a, b in zip(times, times[1:]))
    assert cmds[-1] == (pytest.approx(p.input_time(p.morph_seconds)), 1.0, 1.0)
    t1, r1, pitch1 = cmds[0]
    assert r1 == pytest.approx(p.ratio_at(0.05))
    assert pitch1 == pytest.approx(2 ** (p.semitones_at(0.05) / 12))
    assert t1 == pytest.approx(p.input_time(0.05))
