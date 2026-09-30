"""Skyline, clean-up, metric classes, lead/bass lane selection and interval entropy."""

from __future__ import annotations

import pytest

from musichistory.identity import melody as m
from musichistory.identity.meter import Meter

M44 = Meter([(0.0, 4, 4, 100)])


def n(beat, length, pitch, track=1, channel=1, vel=0.8):
    return [beat, length, pitch, track, channel, vel]


def test_skyline_keeps_the_top_note_and_cuts_at_the_next_onset():
    notes = [n(0, 2, 60), n(0, 2, 64), n(0, 2, 67), n(1, 1, 72), n(2, 1, 65)]
    assert m.skyline(notes) == [(0, 1, 67), (1, 1, 72), (2, 1, 65)]
    assert m.skyline(notes, highest=False) == [(0, 1, 60), (1, 1, 72), (2, 1, 65)]


def test_cleanup_removes_grace_notes_quantizes_and_absorbs_rests():
    line = [(0.0, 0.5, 60), (0.9375, 0.0625, 63), (1.0, 0.5, 64),   # grace note a semitone below
            (2.02, 0.4, 67),                                     # slightly late onset
            (3.0, 0.05, 60), (3.06, 0.5, 72),                    # short but a big leap: kept
            (4.3333333, 0.3, 69), (6.0, 1.0, 67)]                # triplet onset, then a rest before 6
    out = m.cleanup(line)
    assert [p for _, _, p in out] == [60, 64, 67, 60, 72, 69, 67]
    onsets = [t for t, _, _ in out]
    assert onsets == [0.0, 1.0, 2.0, 3.0, round(37 / 12, 6), round(52 / 12, 6), 6.0]
    durs = [d for _, d, _ in out]
    assert durs[0] == 1.0                      # absorbs the gap and the removed grace note
    assert durs[5] == pytest.approx(6.0 - 52 / 12, abs=1e-6)   # rest before beat 6 absorbed
    assert durs[-1] == 1.0                     # last note keeps its own (quantized) length


def test_metric_classes_simple_and_compound():
    assert [M44.metric_class(b) for b in (0, 1, 1.5, 1.25, 4, 7.5, 1 / 3)] == [0, 1, 2, 3, 0, 2, 3]
    m68 = Meter([(0.0, 6, 8, 10)])
    assert [m68.metric_class(b) for b in (0, 1.5, 0.5, 1.0, 3.0, 0.25)] == [0, 1, 2, 2, 0, 3]
    mixed = Meter([(0.0, 4, 4, 2), (8.0, 3, 4, 2)])
    assert mixed.is_downbeat(8.0) and mixed.is_downbeat(11.0) and not mixed.is_downbeat(12.0)
    assert mixed.beats_per_bar() == 4.0 and mixed.bar_start(12.5) == 11.0


def test_interval_entropy():
    assert m.interval_entropy([60] * 10) == 0.0
    assert m.interval_entropy([60, 62, 60, 62, 60]) == pytest.approx(1.0)
    assert m.interval_entropy([60]) == 0.0


def _song():
    """Tune on track 3 (monophonic, mid register), chords on track 1, bass on track 2."""
    notes = []
    for b in range(40):
        t = b * 4
        notes += [n(t, 4, p, 1, 1, 0.6) for p in (60, 64, 67)]
        notes += [n(t, 2, 36, 2, 2, 0.9), n(t + 2, 2, 43, 2, 2, 0.9)]
        notes += [n(t + k, 1, 72 + (k * 2 + b) % 7, 3, 3, 1.0) for k in range(4)]
    return notes


def test_classifier_picks_the_tune_not_the_bass_or_the_chords():
    line = m.select_melody(_song(), None, lambda b: 0, M44)
    assert line.method == "classifier" and (line.track, line.channel) == (3, 3)
    assert len(line) == 160 and line.met[:4] == [0, 1, 1, 1]
    bass = m.select_bass(_song(), None, lambda b: 0, M44, exclude=(3, 3))
    assert (bass.track, bass.channel) == (2, 2) and bass.pitches[:2] == [36, 43]


def test_name_and_lyric_hints_come_first():
    feats = {"tracks": [{"index": 1, "role": "vocal", "role_src": "name", "is_drum": False, "n_notes": 120}]}
    line = m.select_melody(_song(), feats, lambda b: 0, M44)
    assert line.method == "name" and line.track == 1 and line.confidence == 0.9
    feats = {"tracks": [], "lyric_melody_track": 2, "lyric_f1": 0.8}
    line = m.select_melody(_song(), feats, lambda b: 0, M44)
    assert line.method == "lyric_timing" and line.track == 2
    # A role guessed from the GM program is not a name.
    feats = {"tracks": [{"index": 1, "role": "vocal", "role_src": "program", "is_drum": False, "n_notes": 120}]}
    assert m.select_melody(_song(), feats, lambda b: 0, M44).method == "classifier"
    assert m.lead_track_hint({"tracks": [{"index": 4, "role": "melody", "role_src": "name", "n_notes": 9}]}) == 4
    assert m.lead_track_hint(None) is None


def test_skyline_fallback_when_no_lane_passes_the_gates():
    notes = [n(b, 1, 60 + (b % 5), 1 + b % 3, 1 + b % 3) for b in range(30)]  # 10 notes per lane
    line = m.select_melody(notes, None, lambda b: 0, M44)
    assert line.method == "skyline" and line.track is None and len(line) == 30


def test_register_tests_use_normalized_pitch():
    # The same tune written a fifth lower still scores as a melody once normalized.
    notes = [[x[0], x[1], x[2] - 7 if x[3] == 3 else x[2], x[3], x[4], x[5]] for x in _song()]
    line = m.select_melody(notes, None, lambda b: 7, M44)
    assert (line.track, line.pitches[0]) == (3, 72)


def test_polyphony_counts_chord_onsets_only():
    doubled = [n(b, 1, 72, 1, 1) for b in range(64)] + [n(b, 1, 76, 1, 1) for b in range(64)]
    st = m.lane_stats((1, 1), doubled, 64.0, lambda b: 0)
    assert st.polyphony == 0.0 and st.occupation == pytest.approx(1.0)
    triads = doubled + [n(b, 1, 79, 1, 1) for b in range(64)]
    assert m.lane_stats((1, 1), triads, 64.0, lambda b: 0).polyphony == 1.0


def test_lines_keep_their_downbeats_when_bar_lines_are_off_the_absolute_grid():
    """Review 'analyze' (bar grid): a first 4/4 at tick 100 of PPQ 384 puts every bar line at
    x.2604. Quantizing onsets to an absolute 1/12 grid (x.25) moved every note off its bar line
    and made every metric class 3; onsets are quantized on their own bar's grid instead."""
    off = 100 / 384
    meter = Meter([(0.0, 4, 4, 1), (round(off, 4), 4, 4, 40)])
    notes = []
    for b in range(40):
        t = off + 4 * b
        notes += [n(t + k, 1, 72 + (k * 2 + b) % 7, 3, 3, 1.0) for k in range(4)]
        notes += [n(t + 0.5, 0.5, 74, 3, 3, 1.0)]
        notes += [n(t, 2, 36, 2, 2, 0.9), n(t + 2, 2, 43, 2, 2, 0.9)]
    mel = m.select_melody(notes, None, lambda b: 0, meter)
    assert mel.met[:5] == [0, 2, 1, 1, 1] and mel.met.count(0) == 40
    assert mel.onsets[0] == pytest.approx(off, abs=1e-3) and mel.durs[:2] == [0.5, 0.5]
    bass = m.select_bass(notes, None, lambda b: 0, meter, exclude=(3, 3))
    assert bass.met[:2] == [0, 1] and bass.met.count(0) == 40
    # Without a meter (or on the absolute grid) quantization is unchanged.
    assert m.quantize(off) == 0.25 and m.quantize(4.51, M44) == 4.5


# --------------------------------------------------------------------------- analyze v2
def test_bass_line_keeps_the_repeated_notes_of_a_riff():
    """Under Pressure: six Ds then an A every bar (beats 0, .5, 1, 1.5, 1.75, 2 and 2.5). The
    stored bass keeps every repeated note with its onset; grace notes (also a same-pitch double
    strike) are still removed and onsets still quantized."""
    riff = [(0.0, 38), (0.5, 38), (1.0, 38), (1.5, 38), (1.75, 38), (2.0, 38), (2.5, 33)]
    notes = []
    for b in range(16):
        notes += [n(4 * b + t + (0.02 if b == 5 and t == 1.0 else 0.0), 0.25, p, 2, 2, 0.9) for t, p in riff]
    notes.append(n(4 * 3 + 0.9375, 0.05, 37, 2, 2, 0.9))      # grace note a semitone below beat 1 of bar 3
    notes.append(n(4 * 7 + 1.9375, 0.05, 38, 2, 2, 0.9))      # double strike just before beat 2 of bar 7
    bass = m.select_bass(notes, {"tracks": [{"index": 2, "role": "bass", "role_src": "program", "n_notes": 114}]},
                         lambda b: 0, M44)
    assert len(bass) == 16 * 7
    assert bass.pitches[:7] == [38, 38, 38, 38, 38, 38, 33]
    assert bass.onsets[:7] == [0.0, 0.5, 1.0, 1.5, 1.75, 2.0, 2.5]
    assert bass.onsets[5 * 7 + 2] == 21.0                        # 21.02 quantized to the grid
    assert bass.durs[:6] == [0.5, 0.5, 0.5, 0.25, 0.25, 0.5]     # each repeat lasts until the next one
    assert all(bass.pitches[7 * b:7 * b + 7] == [38] * 6 + [33] for b in range(16))


def _lane_song():
    """Tune (track 3), bass (track 2), a pad repeating one chord (track 1), a strings figure
    (track 4), a drone (track 5), an octave doubling of the tune (track 6), a sparse lane (track 7)."""
    notes = _song()
    for b in range(40):
        t = b * 4
        notes += [n(t + k, 1, 76 + (2 * k + b) % 5, 4, 4, 0.7) for k in range(0, 4, 2)]   # strings: 80 notes
        notes += [n(t + k, 1, 48, 5, 5, 0.5) for k in range(4)]                           # drone: 160 notes, 1 pc
        notes += [n(t + k, 1, 84 + (k * 2 + b) % 7, 6, 6, 0.6) for k in range(4)]        # tune an octave up
    notes += [n(4 * b, 1, 70 + b % 3, 7, 7, 0.5) for b in range(20)]                       # 20 notes: too few
    return notes


def test_select_lanes_stores_the_other_melodic_lanes():
    notes = _lane_song()
    mel = m.select_melody(notes, None, lambda b: 0, M44)
    bass = m.select_bass(notes, None, lambda b: 0, M44, exclude=(mel.track, mel.channel))
    assert (mel.track, bass.track) == (3, 2)
    used = m.source_lanes(mel, notes, None) | m.source_lanes(bass, notes, None)
    assert used == {(3, 3), (2, 2)}
    lanes = m.select_lanes(notes, lambda b: 0, M44, exclude=used, kept=(mel, bass))
    # Only the strings figure: the pad's top voice never moves (one pitch class), the drone has
    # one pitch class, track 6 doubles the tune, track 7 has 20 notes.
    assert [(ln.track, ln.channel) for ln in lanes] == [(4, 4)]
    assert all(ln.method == "lane" for ln in lanes)
    strings = lanes[0]
    assert len(strings) == 80 and strings.pitches[0] == 76 and strings.onsets[:2] == [0.0, 2.0]
    assert strings.met[:2] == [0, 1] and strings.durs[:2] == [2.0, 2.0]
    assert m.lane_role(4, 4) == "lane:4:4"
    # Lanes are normalized like the lead line: every pitch moves with the region shift.
    shifted = m.select_lanes(notes, lambda b: 3, M44, exclude=used, kept=(mel, bass))
    assert [p - 3 for p in shifted[0].pitches] == strings.pitches
    # Without the kept lines, the larger of the tune and its octave doubling is kept, not both.
    lanes = [(ln.track, ln.channel) for ln in m.select_lanes(notes, lambda b: 0, M44, exclude={(2, 2)})]
    assert lanes == [(3, 3), (4, 4)]


def test_melodic_needs_three_pitch_classes_and_some_changes():
    def line(pitches):
        return m.Line([float(i) for i in range(len(pitches))], [1.0] * len(pitches), pitches, [1] * len(pitches),
                      1, 1, "lane", 0.0)
    assert not m.melodic(line([48, 55] * 20))                       # two-note pump
    assert not m.melodic(line([60, 64, 67] * 5))                    # 15 notes: too short
    assert m.melodic(line([60] * 7 + [67] + [60] * 7 + [64] + [60] * 7 + [67] + [60] * 7 + [64] + [62] * 8))
    assert not m.melodic(line([60] * 30 + [62, 64, 60]))           # three changes only: a drone


def test_source_lanes_by_selection_method():
    notes = [n(b, 1, 70, 1, 1) for b in range(40)] + [n(b, 1, 74, 1, 2) for b in range(40)] + \
            [n(b, 1, 40, 2, 3) for b in range(40)]
    named = m.Line([0.0], [1.0], [70], [0], 1, 1, "name", 0.9)
    assert m.source_lanes(named, notes, None) == {(1, 1), (1, 2)}
    classified = m.Line([0.0], [1.0], [70], [0], 1, 2, "classifier", 0.5)
    assert m.source_lanes(classified, notes, None) == {(1, 2)}
    feats = {"tracks": [{"index": 1, "role": "bass", "role_src": "program", "n_notes": 80}]}
    bass = m.Line([0.0], [1.0], [70], [0], 1, 1, "bass", 1.0)
    assert m.source_lanes(bass, notes, feats) == {(1, 1), (1, 2)}   # a named bass track: all its channels
    assert m.source_lanes(bass, notes, None) == {(1, 1)}            # the lowest-lane rule: that lane
    assert m.source_lanes(m.Line([], [], [], [], None, None, "skyline", 0.2), notes, None) == set()
    assert m.source_lanes(None, notes, None) == set()


def test_register_tests_can_use_their_own_frame():
    """The lane choice uses ``register_at`` (extract passes the ensemble home's one shift), so a
    region cleanup that moves the song down a fifth does not drop a lead line written low."""
    notes = [[x[0], x[1], x[2] - 21 if x[3] == 3 else x[2], x[3], x[4], x[5]] for x in _song()]   # tune at ~53
    assert m.select_melody(notes, None, lambda b: -5, M44).track != 3                 # 48 < 50: gated out
    line = m.select_melody(notes, None, lambda b: -5, M44, register_at=lambda b: 0)
    assert (line.track, line.pitches[0]) == (3, 72 - 21 - 5)                          # built with shift_at
