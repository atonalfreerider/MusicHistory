"""Loudness (BS.1770), normalization, trimming, speech-band level and the duck."""

import numpy as np

from musichistory.narration import audio

SR = 48000


def sine(freq, amp, seconds, sr=SR):
    return (amp * np.sin(2 * np.pi * freq * np.arange(int(seconds * sr)) / sr)).astype(np.float32)


def test_loudness_of_reference_sine():
    # BS.1770: a 1 kHz sine at 0 dBFS peak on one channel reads -3.01 LKFS; at 0.1 peak, -23.01.
    assert abs(audio.integrated_loudness(sine(1000, 1.0, 5), SR) + 3.01) < 0.1
    assert abs(audio.integrated_loudness(sine(1000, 0.1, 5), SR) + 23.01) < 0.1
    assert audio.integrated_loudness(np.zeros(SR * 2, np.float32), SR) == float("-inf")


def test_normalize_to_minus_16_lufs_under_the_true_peak():
    rng = np.random.default_rng(0)
    y = (rng.standard_normal(SR * 4) * 0.01).astype(np.float32)
    out, info = audio.normalize(y, SR)
    assert abs(info["lufs"] - audio.TARGET_LUFS) < 0.5
    assert info["true_peak"] <= audio.MAX_TRUE_PEAK + 0.05
    spiky = sine(1000, 0.05, 3)
    spiky[SR] = 0.9
    out, info = audio.normalize(spiky, SR)
    assert info["limited"] and info["true_peak"] <= audio.MAX_TRUE_PEAK + 0.05


def test_resample_and_trim():
    y = np.concatenate([np.zeros(24000, np.float32), sine(200, 0.5, 1.0, 24000), np.zeros(24000, np.float32)])
    up = audio.resample(y, 24000)
    assert len(up) == 2 * len(y)
    t = audio.trim(up, SR)
    assert abs(len(t) / SR - (1.0 + audio.LEAD_IN + audio.TAIL)) < 0.02
    assert abs(t[0]) < 1e-6 and abs(t[-1]) < 1e-6


def test_band_level_ignores_out_of_band_energy():
    low = sine(60, 0.5, 2)            # bass, below the speech band
    mid = sine(1000, 0.05, 2)         # in the band: 0.05 peak -> RMS -29 dBFS
    assert audio.band_level(low, SR) < -40
    assert abs(audio.band_level(mid, SR) - 20 * np.log10(0.05 / np.sqrt(2))) < 0.5
    assert abs(audio.band_level(low + mid, SR) - audio.band_level(mid, SR)) < 1.0
    assert audio.band_level(mid, SR, 1.95, 2.0) == float("-inf")


def test_duck_targets_ten_db_and_clamps():
    assert audio.duck_db(-20.0, -18.0) == -12.0          # -20 - 10 - (-18)
    assert audio.duck_db(-20.0, -5.0) == -18.0           # loud music: clamp
    assert audio.duck_db(-20.0, -40.0) == -6.0           # quiet music: still dips by 6
    assert audio.duck_db(-20.0, float("-inf")) == -6.0
