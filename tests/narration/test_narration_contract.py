"""narration.json contract validator and the script checks the stage applies."""

import copy

import numpy as np
import soundfile

from musichistory.narration import contract, script


def doc():
    return {"version": 1, "voice": "7NoxJCAEPTXbnfIvyaF6", "voice_name": "JohnV4", "model": "eleven_v4",
            "paths": [{"id": "four-chords", "cues": [
                {"id": "intro", "at": 1.5, "seconds": 2.0, "file": "four-chords/01_intro.wav",
                 "text": "Four chords carry it. They return every bar.", "duck_db": -12.0, "image": None,
                 "sources": [{"title": "A book", "url": "https://example.org/book"}],
                 "inflection": {"falls": 2, "of": 2}},
                {"id": "song-two", "at": 6.0, "seconds": 1.0, "file": "four-chords/02_song-two.wav",
                 "text": "The second song borrows the loop.", "duck_db": -6.0, "image": "Q123",
                 "sources": [], "inflection": {"falls": 0, "of": 1}}]}]}


def mashups():
    return {"paths": [{"id": "four-chords", "seconds": 30.0, "file": "four-chords/mix.mp3",
                       "segments": [{"start": 0.0, "end": 10.0}, {"start": 10.0, "end": 30.0}]}]}


def test_valid_document():
    assert contract.validate(doc()) == []
    assert contract.validate(doc(), mashups=mashups()) == []


def test_files_checked_against_root(tmp_path):
    d = doc()
    errs = contract.validate(d, tmp_path)
    assert any("missing" in e for e in errs)
    (tmp_path / "four-chords").mkdir()
    soundfile.write(str(tmp_path / "four-chords/01_intro.wav"), np.zeros(96000, np.float32), 48000, subtype="PCM_16")
    soundfile.write(str(tmp_path / "four-chords/02_song-two.wav"), np.zeros(22050, np.float32), 22050, subtype="PCM_16")
    errs = contract.validate(d, tmp_path)
    assert len(errs) == 1 and "not 48 kHz mono" in errs[0]


def test_violations():
    cases = []
    d = doc(); d["paths"][0]["cues"][0]["text"] = "Is it four chords?"; cases.append((d, "ending in a period"))
    d = doc(); d["paths"][0]["cues"][0]["duck_db"] = -3.0; cases.append((d, "duck_db"))
    d = doc(); d["paths"][0]["cues"][0]["duck_db"] = -24.0; cases.append((d, "duck_db"))
    d = doc(); d["paths"][0]["cues"][1]["file"] = "four-chords/2_song-two.wav"; cases.append((d, "file must be"))
    d = doc(); d["paths"][0]["cues"][1]["at"] = 1.0; cases.append((d, "after the previous cue"))
    d = doc(); d["paths"][0]["cues"][0]["inflection"] = {"falls": 3, "of": 2}; cases.append((d, "inflection"))
    d = doc(); d["paths"][0]["cues"][0]["extra"] = 1; cases.append((d, "keys differ"))
    d = doc(); d["paths"][0]["cues"][0]["sources"] = [{"title": "x", "url": "ftp://x"}]; cases.append((d, "http"))
    d = doc(); d["model"] = "eleven_v3"; cases.append((d, "narrator"))
    d = doc(); d["paths"][0]["cues"][1]["at"] = 31.0; cases.append((d, "past the mix end"))
    for d, frag in cases:
        errs = contract.validate(d, mashups=mashups())
        assert any(frag in e for e in errs), (frag, errs)


def test_unknown_mashup_path():
    d = doc()
    d["paths"][0]["id"] = "other"
    for c in d["paths"][0]["cues"]:
        c["file"] = c["file"].replace("four-chords", "other")
    assert any("no such mashup" in e for e in contract.validate(d, mashups=mashups()))


def _script():
    return {"id": "four-chords", "title": "T", "cues": [
        {"id": "intro", "anchor": {"segment": 0, "offset": 1.5}, "kind": "intro", "text": "One. Two.",
         "image": None, "sources": [{"title": "S", "url": "https://example.org"}]},
        {"id": "hop", "anchor": {"segment": 1, "offset": 2.0}, "kind": "changeover",
         "text": " ".join(["word"] * 60) + ".", "image": None, "sources": []}]}


def test_script_validation_and_times():
    m = mashups()
    s = _script()
    assert script.validate(s, m["paths"][0]) == []
    assert [script.cue_time(c, m["paths"][0]) for c in s["cues"]] == [1.5, 12.0]
    bad = copy.deepcopy(s)
    bad["cues"][1]["anchor"] = {"segment": 5, "offset": 0}
    assert any("outside" in e for e in script.validate(bad, m["paths"][0]))
    bad = copy.deepcopy(s)
    bad["cues"][1]["anchor"] = {"segment": 0, "offset": 0.5}
    assert any("not after the previous cue" in e for e in script.validate(bad, m["paths"][0]))
    bad = copy.deepcopy(s)
    bad["cues"][0]["kind"] = "aside"
    assert any("kind" in e for e in script.validate(bad, m["paths"][0]))


def test_fit_plan_flags_long_lines():
    plan = script.fit_plan(_script(), mashups()["paths"][0])
    assert plan[0]["fits"] and plan[0]["room"] == 10.5
    assert not plan[1]["fits"] and plan[1]["room"] == 18.0 and plan[1]["estimate"] > 18.0


def test_captions_only_cue_is_valid_without_audio():
    from musichistory.narration import contract

    cue = {"id": "intro", "at": 0.3, "seconds": 4.2, "file": None, "text": "Three songs share one loop.",
           "duck_db": None, "image": None, "sources": [{"title": "A", "url": "https://example.org/a"}],
           "inflection": None}
    doc = {"version": contract.VERSION, "voice": contract.VOICE, "voice_name": contract.VOICE_NAME,
           "model": contract.MODEL, "paths": [{"id": "p", "cues": [cue]}]}
    assert contract.validate(doc) == []
    half = dict(cue, duck_db=-10.0)  # audio fields are null together or not at all
    assert contract.validate({**doc, "paths": [{"id": "p", "cues": [half]}]})
