"""Preview measurement: key/tempo resolution with the MIDI prior, chord labels, beat BPM, and
one end-to-end measurement of a synthesized clip."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory.identity.key import TKP_MAJOR  # noqa: E402
from musichistory.paths import audio  # noqa: E402

C, G, AM, F = 0, 14, 19, 10          # chord labels root*2 + minor


def triad(root: int, minor: bool = False) -> np.ndarray:
    v = np.zeros(12)
    for n in (0, 3 if minor else 4, 7):
        v[(root + n) % 12] = 1.0
    return v


def raw_key(hist, chord_hist=None) -> dict:
    return {"chroma_hist": list(hist), "chord_hist": list(chord_hist) if chord_hist is not None else None}


def test_profile_finds_the_rotated_key():
    for tonic in (0, 3, 7, 11):
        hist = np.roll(np.array(TKP_MAJOR), tonic)
        k = audio.resolve_key(raw_key(hist), None, None)
        assert (k.tonic, k.mode, k.source) == (tonic, "major", "audio")


def test_chord_function_breaks_fifth_ties():
    # A C-major collection whose chords centre on G (G, C, D, Em): the key is G major.
    hist = np.ones(12) * 0.2
    for pc in (0, 2, 4, 5, 7, 9, 11):
        hist[pc] = 1.0
    hist[5] = 0.6                     # F less common ...
    hist[6] = 0.5                     # ... F# present: G major collection
    ch = np.zeros(24)
    ch[7 * 2] = 10                    # G
    ch[0] = 4                         # C
    ch[2 * 2] = 4                     # D
    ch[4 * 2 + 1] = 2                 # Em
    k = audio.resolve_key(raw_key(hist, ch), None, None)
    assert (k.tonic, k.mode) == (7, "major")


def test_midi_prior_decides_a_weak_relative_call():
    # Equal chord evidence for C and Am on a C-major collection: the MIDI key decides.
    hist = np.zeros(12)
    for pc in (0, 2, 4, 5, 7, 9, 11):
        hist[pc] = 1.0
    ch = np.zeros(24)
    ch[C] = ch[AM] = 5
    scores = audio.key_scores(hist, ch)
    assert abs(scores[audio.key_index(0, "major")] - scores[audio.key_index(9, "minor")]) < audio.MIDI_PRIOR
    k_major = audio.resolve_key(raw_key(hist, ch), 0, "major")
    k_minor = audio.resolve_key(raw_key(hist, ch), 9, "minor")
    assert (k_major.tonic, k_major.mode) == (0, "major")
    assert (k_minor.tonic, k_minor.mode) == (9, "minor")


def test_midi_prior_decides_a_weak_fifth_call():
    # C-major collection, chords split between C and G: the audio leans one way, the MIDI decides.
    hist = np.zeros(12)
    for pc in (0, 2, 4, 5, 7, 9, 11):
        hist[pc] = 1.0
    ch = np.zeros(24)
    ch[C], ch[G] = 5, 6
    for midi in ((0, "major"), (7, "major")):
        k = audio.resolve_key(raw_key(hist, ch), *midi)
        assert (k.tonic, k.mode) == midi


def test_midi_prior_does_not_override_clear_audio():
    hist = np.roll(np.array(TKP_MAJOR), 2)          # D major, clearly
    ch = np.zeros(24)
    ch[2 * 2] = 10
    k = audio.resolve_key(raw_key(hist, ch), 11, "minor")   # MIDI says B minor (the relative)
    assert (k.tonic, k.mode, k.prior_used, k.source) == (2, "major", False, "audio")


def test_transposed_midi_is_overridden():
    # A fan MIDI a semitone off (E minor) against a recording clearly in Eb minor.
    hist = np.roll(np.array(audio.TKP_MINOR), 3)
    ch = np.zeros(24)
    ch[3 * 2 + 1] = 10
    k = audio.resolve_key(raw_key(hist, ch), 4, "minor")
    assert (k.tonic, k.mode, k.source) == (3, "minor", "audio")
    assert k.midi_gap > audio.MIDI_PRIOR


def test_flat_chroma_falls_back_to_midi():
    k = audio.resolve_key(raw_key(np.ones(12)), 5, "minor")
    assert (k.tonic, k.mode, k.source) == (5, "minor", "midi")


def test_relative_and_parallel_indices():
    assert audio.relative_of(audio.key_index(0, "major")) == audio.key_index(9, "minor")
    assert audio.relative_of(audio.key_index(9, "minor")) == audio.key_index(0, "major")
    assert audio.parallel_of(audio.key_index(4, "minor")) == audio.key_index(4, "major")


def test_chord_labels_follow_a_progression():
    cols = [triad(0)] * 4 + [triad(7)] * 4 + [triad(9, True)] * 4 + [triad(5)] * 4
    X = np.array(cols).T + 0.05
    labels = audio.chord_labels(X)
    assert labels == [C] * 4 + [G] * 4 + [AM] * 4 + [F] * 4


def test_chord_labels_smooth_a_one_beat_blip():
    cols = [triad(0)] * 3 + [triad(0) * 0.6 + triad(4, True) * 0.5] + [triad(0)] * 3
    labels = audio.chord_labels(np.array(cols).T)
    assert labels == [C] * 7


def test_silent_columns_are_marked():
    X = np.array([triad(0), np.zeros(12), triad(0)]).T
    assert audio.chord_labels(X)[1] == -1


def test_beat_bpm_is_robust_to_skipped_and_extra_beats():
    beats = np.arange(0, 20, 0.5)
    assert audio.beat_bpm(beats)[0] == pytest.approx(120)
    skipped = np.delete(beats, 10)
    assert audio.beat_bpm(skipped)[0] == pytest.approx(120)
    extra = np.sort(np.append(beats, 5.2))
    assert audio.beat_bpm(extra)[0] == pytest.approx(120)
    assert audio.beat_bpm([0, 1])[0] is None


def _cands(*rows):
    return {"candidates": [dict(factor=f, bpm=b, strength=s, beat_bpm=b, ibi_cv=0.02, n_beats=40) for f, b, s in rows]}


def test_tempo_octave_follows_the_midi_prior():
    raw = _cands((0.5, 60.0, 0.8), (1.0, 120.0, 0.7), (2.0, 240.0, 0.3))
    assert audio.resolve_tempo(raw, 118).bpm == pytest.approx(120)
    assert audio.resolve_tempo(raw, 64).bpm == pytest.approx(60)
    # 240 is nearest to a 250 MIDI tempo but too weak (< 0.5 x strongest): next nearest wins.
    t = audio.resolve_tempo(raw, 250)
    assert t.bpm == pytest.approx(120) and t.source == "audio"


def test_tempo_uses_beats_when_they_agree():
    raw = {"candidates": [dict(factor=1.0, bpm=117.45, strength=0.8, beat_bpm=118.57, ibi_cv=0.03, n_beats=58)]}
    assert audio.resolve_tempo(raw, 120).bpm == pytest.approx(118.57)
    raw["candidates"][0]["beat_bpm"] = 60.0                  # tracker locked elsewhere: keep the candidate
    assert audio.resolve_tempo(raw, 120).bpm == pytest.approx(117.45)


def test_unclear_pulse_falls_back_to_midi():
    raw = {"candidates": [dict(factor=1.0, bpm=97.0, strength=0.03, beat_bpm=97.0, ibi_cv=0.4, n_beats=8)]}
    t = audio.resolve_tempo(raw, 72)
    assert (t.bpm, t.source) == (72, "midi")


def test_pack_roundtrip():
    X = np.abs(np.random.default_rng(1).normal(size=(12, 9)))
    Y = audio.unpack(audio._pack(X))
    assert Y.shape == X.shape
    assert np.allclose(Y, X / X.max(axis=0, keepdims=True), atol=1 / 255)


def _synth(bpm: float, chords: list[tuple[int, bool]], seconds: float, sr: int = audio.SR) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    y = np.zeros_like(t)
    beat = 60.0 / bpm
    for i in range(int(seconds / beat)):
        root, minor = chords[(i // 4) % len(chords)]
        a, b = int(i * beat * sr), int((i + 1) * beat * sr)
        seg = t[a:b] - t[a]
        env = np.exp(-seg * 3)
        for n in (0, 3 if minor else 4, 7):
            f = 261.63 * 2 ** (((root + n) % 12) / 12)
            y[a:b] += 0.2 * env * np.sin(2 * np.pi * f * seg)
        f_bass = 65.41 * 2 ** (root / 12)
        y[a:b] += 0.3 * env * np.sin(2 * np.pi * f_bass * seg)
        click = min(b - a, int(0.02 * sr))
        y[a:a + click] += 0.5 * np.random.default_rng(i).normal(size=click) * np.exp(-np.arange(click) / 60)
    return (y / np.abs(y).max() * 0.8).astype(np.float32)


def test_measure_synthesized_clip():
    pytest.importorskip("librosa")
    y = _synth(100.0, [(7, False), (2, False), (4, True), (0, False)], 16.0)   # G D Em C in G major
    raw = audio.measure(y)
    t = audio.resolve_tempo(raw["tempo"], 100.0)
    assert t.bpm == pytest.approx(100.0, rel=0.03)
    k = audio.resolve_key(raw, None, None)
    assert (k.tonic, k.mode) == (7, "major")
    chords = [c for c in raw["grid"]["chords"] if c >= 0]
    assert {7 * 2, 2 * 2, 4 * 2 + 1, 0} <= set(chords)


def test_reference_keys_regression():
    """On recordings with documented keys the prior rule must beat both sources alone
    (tuning run: audio 54/76, MIDI 62/76, rule 64/76)."""
    import json
    import sqlite3

    from musichistory import config
    from musichistory.paths import contract

    cache_path = config.DATA / "audio" / "analysis.json"
    if not cache_path.exists() or not config.GRAPH_DB.exists():
        pytest.skip("preview analysis not built")
    cache = audio.load_cache(cache_path)
    ref = json.loads((Path(__file__).with_name("reference_keys.json")).read_text(encoding="utf-8"))["songs"]
    conn = sqlite3.connect(config.GRAPH_DB)
    midi = {w: (t, m) for w, t, m in conn.execute("SELECT work_id, tonic_pc, mode FROM song_node")}
    conn.close()
    rows = [r for r in ref if r["work_id"] in cache and r["work_id"] in midi]
    assert len(rows) >= 70

    def truth(r):
        return contract.key_tonic(r["key"]), r["key"].split()[1]

    ours = sum(1 for r in rows if (lambda k: (k.tonic, k.mode))(audio.resolve_key(cache[r["work_id"]], *midi[r["work_id"]]))
               == truth(r))
    midi_only = sum(1 for r in rows if midi[r["work_id"]] == truth(r))
    audio_only = sum(1 for r in rows if (lambda k: (k.audio_tonic, k.audio_mode))(
        audio.resolve_key(cache[r["work_id"]], None, None)) == truth(r))
    assert ours >= midi_only and ours > audio_only
    assert ours / len(rows) >= 0.8
