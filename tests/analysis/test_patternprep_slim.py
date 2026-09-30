"""The .patterns.json -> slim conversion (no PatternPrep run needed)."""

from __future__ import annotations

import json

import pytest

from musichistory.analysis import patternprep as pp


def bundle():
    """A small Version-5 bundle shaped like real PatternPrep output (A = 0 pitch classes),
    with names that must not survive: a marker-derived section role, a track name, lyrics."""
    return {
        "Version": 5, "Key": -1, "Minor": False, "Style": "pop", "SongBars": 8, "EndBeat": 32.5, "Duration": 19.6,
        "Title": "SECRET-TITLE", "TrackNames": ["SECRET-TRACK"], "FormName": "SECRET-FORM", "Summary": "x",
        "Provenance": "MIDI section markers", "Lyrics": {"Syllables": [{"Text": "SECRET-LYRIC"}]},
        "Tempos": [{"Beat": 0, "Seconds": 0, "Microseconds": 500000}, {"Beat": 0, "Seconds": 0, "Microseconds": 600000},
                   {"Beat": 16, "Seconds": 9.6, "Microseconds": 600000}, {"Beat": 24, "Seconds": 14.4, "Microseconds": 400000}],
        "Measures": [{"Start": 4 * i, "End": 4 * i + 4, "Numerator": 4, "Denominator": 4} for i in range(6)]
        + [{"Start": 24, "End": 27, "Numerator": 3, "Denominator": 4}, {"Start": 27, "End": 30, "Numerator": 3, "Denominator": 4}],
        "Chords": [{"Rest": True, "Start": 0, "End": 4, "Root": -1, "Quality": ""},
                   {"Rest": False, "Start": 4, "End": 8, "Root": 0, "Quality": ""},        # A major
                   {"Rest": False, "Start": 8, "End": 12, "Root": 7, "Quality": "7"},      # E7
                   {"Rest": False, "Start": 12, "End": 16, "Root": 9, "Quality": "m"},     # F#m
                   {"Rest": False, "Start": 16, "End": 20, "Root": 3, "Quality": " tone"}],  # old-bundle quality
        "Sections": [{"FirstBar": 0, "BarCount": 4, "Family": 0, "Role": "Chorus", "Letter": "A", "Start": 0, "End": 16,
                      "Loops": 2, "CycleBeats": 8, "Transpose": 0, "Variation": "1-bar tag", "Group": -1,
                      "Name": "SECRET-NAME", "Label": "SECRET-LABEL"},
                     {"FirstBar": 4, "BarCount": 4, "Family": 1, "Role": "SECRET-MARKER", "Letter": "B", "Start": 16,
                      "End": 32, "Loops": 1, "CycleBeats": 16, "Transpose": 2, "Variation": "SECRET→MARKER",
                      "Group": -1}],
        "Patterns": [{"Family": 0, "Reference": 0, "LoopBars": 2, "LoopBeats": 8, "Visits": 1, "Passes": 2,
                      "Role": "Chorus", "Name": "SECRET-NAME", "Word": "x",
                      "Loop": [{"Root": 0, "Quality": "", "Start": 0, "End": 4}, {"Root": 7, "Quality": "7", "Start": 4, "End": 8}]}],
        "Groups": [{"Id": 0, "Visits": 2, "Name": "SECRET", "Short": "S", "Families": [0, 1]}],
        "FormGrammar": "In (V PC C)×2 Br Out",
        "KeyChanges": [{"Beat": 16, "Key": 2, "From": 0, "Minor": False, "FromMinor": False, "Evidence": "SECRET"}],
        "Frames": [{"Time": 0.0, "Key": 0, "Minor": False}, {"Time": 5.0, "Key": 0, "Minor": False},
                   {"Time": 9.7, "Key": 2, "Minor": False}, {"Time": 12.0, "Key": 2, "Minor": False}],
        "Notes": [{"Beat": 4, "Length": 0.99, "Pitch": 57, "Track": 1, "Channel": 1, "Velocity": 0.866},
                  {"Beat": 4, "Length": 0.1, "Pitch": 36, "Track": 2, "Channel": 10, "Velocity": 0.9}],
        "Parts": [{"Track": 1, "Channel": 1, "Name": "SECRET-TRACK", "Role": "keys", "Vocal": False, "Bars": 8,
                   "FundamentalBars": 2, "Grammar": "· A×3 B′"},
                  {"Track": 2, "Channel": 2, "Name": "x", "Role": "SECRET", "Vocal": True, "Bars": 8,
                   "FundamentalBars": 2, "Grammar": "SECRET words"}],
    }


def test_slim_schema_and_conversions():
    s = pp.slim_from_bundle(bundle(), commit="abc", midi_sha256="f00")
    assert s["version"] == 5 and s["resonance_commit"] == "abc" and s["style"] == "pop" and s["song_bars"] == 8
    assert s["tempos"] == [[0.0, 600000], [24.0, 400000]]  # synthetic 120 BPM header dropped, repeats merged
    assert s["measures"] == [[0.0, 4, 4, 6], [24.0, 3, 4, 2]]
    assert s["chords"] == [[0.0, 4.0, -1, ""], [4.0, 8.0, 9, ""], [8.0, 12.0, 4, "7"], [12.0, 16.0, 6, "m"],
                           [16.0, 20.0, 0, ""]]
    assert s["patterns"][0]["loop"] == [[0.0, 4.0, 9, ""], [4.0, 8.0, 4, "7"]]
    assert s["key_changes"] == [{"beat": 16.0, "tonic": 11, "minor": False, "from_tonic": 9, "from_minor": False}]
    # Frames are seconds-timed; the run boundary snaps to the KeyChange beat.
    assert s["key_runs"] == [[0.0, 16.0, 9, False], [16.0, 32.5, 11, False]]
    assert s["notes"] == [[4.0, 0.99, 57, 1, 1, 0.87], [4.0, 0.1, 36, 2, 10, 0.9]]
    assert s["form_grammar"] == "In (V PC C)x2 Br Out"
    assert s["parts"][0]["grammar"] == "- Ax3 B'" and s["parts"][0]["role"] == "keys"
    assert s["groups"] == [{"id": 0, "visits": 2, "families": [0, 1]}]


def test_slim_never_carries_names_or_lyrics():
    s = pp.slim_from_bundle(bundle(), commit="abc")
    blob = json.dumps(s)
    assert "SECRET" not in blob and "Lyrics" not in blob and "lyric" not in blob.lower()
    assert s["sections"][1]["role"] == "Section" and s["sections"][1]["variation"] == ""
    assert s["sections"][0]["variation"] == "1-bar tag"
    assert s["parts"][1]["role"] == "other" and s["parts"][1]["grammar"] == ""
    for key in ("TrackNames", "Title", "FormName", "Provenance", "Summary"):
        assert key not in s and key.lower() not in s


def test_frame_keys_fall_back_to_frame_times_without_key_changes():
    b = bundle()
    b["KeyChanges"] = []
    s = pp.slim_from_bundle(b, commit="abc")
    # Frame at 9.7 s: tempo 600000 us from beat 16 at 9.6 s -> beat 16 + 0.1/0.6 = 16.1667
    assert s["key_runs"] == [[0.0, 16.1667, 9, False], [16.1667, 32.5, 11, False]]


def test_grammar_filter():
    assert pp._grammar("In It (V PC C)×2 PoC Br Out", pp.GRAMMAR_WORDS) == "In It (V PC C)x2 PoC Br Out"
    assert pp._grammar("Verse 1 (Bob)", pp.GRAMMAR_WORDS) == ""
    assert pp._grammar("A B A′ Coda", pp.GRAMMAR_WORDS) == "A B A' Coda"
    assert pp._grammar("· A′ B×3 A B×2 C″", None) == "- A' Bx3 A Bx2 C''"
    assert pp._grammar("Lead Organ", None) == ""


def test_section_letters():
    assert pp._letter("A") == "A" and pp._letter("B′") == "B'" and pp._letter("AB″") == "AB''"
    assert pp._letter("Verse") == "" and pp._letter("") == ""


def test_variation_filter_keeps_generated_notes_only():
    assert pp._variation("transposed −2 · 1-bar lead-in") == "transposed -2 - 1-bar lead-in"
    assert pp._variation("3 passes (usually 2)") == "3 passes (usually 2)"
    assert pp._variation("repeats Chorus 1") == "repeats Chorus 1"
    assert pp._variation("varied bars 2, 3") == "varied bars 2, 3"
    assert pp._variation("repeats Bobs Song") == ""


def test_rebuild_when_commit_or_sources_change(tmp_path):
    (tmp_path / ("PatternPrep.exe" if pp.os.name == "nt" else "PatternPrep")).write_bytes(b"")
    (tmp_path / "musichistory-build.json").write_text(json.dumps({"commit": "c1", "sources_sha256": "s1"}))
    assert pp._up_to_date(tmp_path, "c1", "s1")
    assert not pp._up_to_date(tmp_path, "c2", "s1")
    assert not pp._up_to_date(tmp_path, "c1", "s2")


def test_header_check_rejects_smpte_and_format2(tmp_path):
    good = b"MThd\x00\x00\x00\x06\x00\x01\x00\x02\x01\xe0"
    (tmp_path / "ok.mid").write_bytes(good)
    pp._check_header(tmp_path / "ok.mid")
    for name, data, msg in (("smpte.mid", good[:12] + b"\xe7\x28", "SMPTE"),
                            ("fmt2.mid", good[:8] + b"\x00\x02" + good[10:], "format 2"),
                            ("html.mid", b"<html>", "MThd")):
        (tmp_path / name).write_bytes(data)
        with pytest.raises(pp.AnalysisError, match=msg):
            pp._check_header(tmp_path / name)


def test_check_slim_rejects_empty_results():
    s = pp.slim_from_bundle(bundle(), commit="abc")
    pp.check_slim(s)
    with pytest.raises(pp.AnalysisError, match="0 sections"):
        pp.check_slim({**s, "sections": []})
    with pytest.raises(pp.AnalysisError, match="no pitched notes"):
        pp.check_slim({**s, "notes": [[0.0, 1.0, 36, 1, 10, 1.0]]})


def _bar(start, end, num=4, den=4):
    return {"Start": start, "End": end, "Numerator": num, "Denominator": den}


def test_measures_keep_a_short_bar_as_its_own_run():
    """Review 'analyze' (bar grid): Resonance cuts a bar short when a meter arrives mid-bar;
    the next bars have the same num/den on a shifted grid and must not join the short bar's run
    (real case: the only 4/4 time signature at tick 100 of PPQ 384 -> bars from beat 0.2604)."""
    from musichistory.identity.meter import Meter

    b = bundle()
    b["Measures"] = [_bar(0, 0.2604)] + [_bar(0.2604 + 4 * k, 4.2604 + 4 * k) for k in range(8)]
    s = pp.slim_from_bundle(b, commit="abc")
    assert s["measures"] == [[0.0, 4, 4, 1], [0.2604, 4, 4, 8]]
    m = Meter.from_slim(s)
    assert m.is_downbeat(4.2604) and not m.is_downbeat(4.0) and m.metric_class(8.2604) == 0
    assert m.bar_start(0.2604) == 0.2604 and m.bar_start(5.0) == 4.2604

    # A meter restated mid-bar: [0,4) [4,6) (cut at the new event) [6,10) [10,14).
    b["Measures"] = [_bar(0, 4), _bar(4, 6), _bar(6, 10), _bar(10, 14)]
    assert pp.slim_from_bundle(b, commit="abc")["measures"] == [[0.0, 4, 4, 2], [6.0, 4, 4, 2]]
    # A bar off its run's grid opens a run even without an End; the normal case still merges.
    b["Measures"] = [{"Start": 0, "Numerator": 4, "Denominator": 4}, {"Start": 3, "Numerator": 4, "Denominator": 4},
                     {"Start": 7, "Numerator": 4, "Denominator": 4}]
    assert pp.slim_from_bundle(b, commit="abc")["measures"] == [[0.0, 4, 4, 1], [3.0, 4, 4, 2]]
    b["Measures"] = [_bar(3 * k, 3 * k + 3, 3, 4) for k in range(5)] + [_bar(15, 18, 6, 8), _bar(18, 21, 6, 8)]
    assert pp.slim_from_bundle(b, commit="abc")["measures"] == [[0.0, 3, 4, 5], [15.0, 6, 8, 2]]
    assert pp.SLIM_VERSION >= 2  # slims cached before this fix carry the merged runs
