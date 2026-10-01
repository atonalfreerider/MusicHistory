"""The narration stage end to end with a fake ElevenLabs: registration, retries on a rising
ending, 48 kHz -16 LUFS WAVs, cue times, duck, fit warnings, a valid narration.json, and no key
anywhere in the outputs."""

import argparse
import importlib
import json

import numpy as np
import soundfile

from musichistory import cli, config
from musichistory.narration import audio, contract, stage, textshape

FAKE_KEY = "sk_fake_SECRET_9876543210"
SR = 24000


def tone(f0a, f0b, seconds, sr=SR):
    f = np.linspace(f0a, f0b, int(seconds * sr))
    ph = 2 * np.pi * np.cumsum(f) / sr
    return (sum(np.sin(k * ph) / k for k in range(1, 6)) * 0.2).astype(np.float32)


class FakeEleven:
    """Speaks one voiced tone per sentence. The first take of ``rise_text`` ends rising."""

    def __init__(self, rise_text=None):
        self.calls = []
        self.rise_text = rise_text

    def __call__(self, url, body, headers, timeout):
        doc = json.loads(body)
        self.calls.append({"url": url, "body": doc, "has_key": headers.get("xi-api-key") == FAKE_KEY})
        n = len(textshape.sentences(doc["text"]))
        rising = doc["text"] == self.rise_text
        parts = [np.zeros(int(0.15 * SR), np.float32)]
        for i in range(n):
            last = i == n - 1
            parts.append(tone(115, 145, 0.9) if (rising and last) else tone(135, 108, 0.9))
            parts.append(np.zeros(int(0.4 * SR), np.float32))
        y = np.concatenate(parts)
        return 200, (y * 20000).astype("<i2").tobytes()


def _setup(tmp_path, monkeypatch, cues):
    mroot = tmp_path / "mashups"
    (mroot / "p1").mkdir(parents=True, exist_ok=True)
    sr = 44100
    rng = np.random.default_rng(0)
    mix = (rng.standard_normal(sr * 30) * 0.1).astype(np.float32)
    try:
        soundfile.write(str(mroot / "p1/mix.mp3"), mix, sr, format="MP3")
    except Exception:  # libsndfile without MP3: soundfile reads by content, not extension
        soundfile.write(str(mroot / "p1/mix.mp3"), mix, sr, format="WAV")
    seg = lambda a, b: {"start": a, "end": b, "kind": "full", "instrumental": "Q1", "vocal": "Q1"}  # noqa: E731
    (mroot / "mashups.json").write_text(json.dumps({"version": 1, "paths": [
        {"id": "p1", "title": "P", "file": "p1/mix.mp3", "seconds": 30.0,
         "segments": [seg(0.0, 10.0), seg(10.0, 30.0)]}]}), encoding="utf-8")
    monkeypatch.setattr(stage, "mashups_root", lambda: mroot)
    monkeypatch.setattr(config, "CACHE", tmp_path / "cache")
    key = tmp_path / "key.txt"
    key.write_text(FAKE_KEY, encoding="utf-8")
    sfile = tmp_path / "script.json"
    sfile.write_text(json.dumps({"id": "p1", "title": "T", "cues": cues}), encoding="utf-8")
    p = argparse.ArgumentParser()
    stage.add_arguments(p)
    out = tmp_path / "out"
    args = p.parse_args(["--script", str(sfile), "--out", str(out), "--key-file", str(key)])
    return args, out


def _cue(cid, seg, off, text):
    return {"id": cid, "anchor": {"segment": seg, "offset": off}, "kind": "song", "text": text, "image": None,
            "sources": [{"title": "Src", "url": "https://example.org/a"}]}


def test_stage_registered():
    assert cli.STAGES["narration"][0] == "musichistory.narration.stage"
    mod = importlib.import_module(cli.STAGES["narration"][0])
    p = argparse.ArgumentParser()
    mod.add_arguments(p)
    a = p.parse_args([])
    assert a.stability == 0.6 and a.tries == 3 and not a.dry_run


def test_takes_vary_settings():
    t = stage.takes(0.6, 0.75)
    assert [x[0]["stability"] for x in t] == [0.6, 0.65, 0.55] and t[1][1] == " "
    q = stage.takes(0.6, 0.75, quantized=True)
    assert all(x[0]["stability"] == 0.5 for x in q) and len({x[0]["similarity_boost"] for x in q}) == 3


def test_end_to_end_with_retry(tmp_path, monkeypatch, capsys):
    first = "The loop climbs to the fifth. Then it settles on the sixth"   # shaped: final period added
    cues = [_cue("intro", 0, 1.0, first), _cue("hop", 1, 0.5, "A second song borrows the same changes!"),
            _cue("late", 1, 1.0, "This line starts too soon after the last one.")]
    args, out = _setup(tmp_path, monkeypatch, cues)
    fake = FakeEleven(rise_text=textshape.shape(first))
    assert stage.run(args, post=fake) == 0
    log = capsys.readouterr().out

    # the rising first take was regenerated with different settings and a trailing space
    assert len(fake.calls) == 4 and all(c["has_key"] for c in fake.calls)
    assert fake.calls[0]["body"]["voice_settings"] == {"stability": 0.6, "similarity_boost": 0.75}
    assert fake.calls[1]["body"]["text"] == textshape.shape(first) + " "
    assert fake.calls[1]["body"]["voice_settings"]["stability"] == 0.65

    doc = json.loads((out / "narration.json").read_text(encoding="utf-8"))
    mashups = json.loads((tmp_path / "mashups/mashups.json").read_text(encoding="utf-8"))
    assert contract.validate(doc, out, mashups) == []
    c = doc["paths"][0]["cues"]
    assert [x["at"] for x in c] == [1.0, 10.5, 11.0]
    assert [x["file"] for x in c] == ["p1/01_intro.wav", "p1/02_hop.wav", "p1/03_late.wav"]
    assert c[0]["inflection"] == {"falls": 2, "of": 2} and c[1]["inflection"] == {"falls": 1, "of": 1}
    assert c[1]["text"] == "A second song borrows the same changes."
    for x in c:
        assert -18.0 <= x["duck_db"] <= -6.0
        y, sr = soundfile.read(str(out / x["file"]), dtype="float32")
        assert sr == 48000 and y.ndim == 1
        assert abs(audio.integrated_loudness(y, sr) + 16.0) < 0.6
        assert abs(len(y) / sr - x["seconds"]) < 0.01
    assert "WARNING: runs" in log                       # cue "hop" overruns "late"
    rep = json.loads((out / "narration_report.json").read_text(encoding="utf-8"))
    assert rep["usage"]["requests"] == 4
    assert len(rep["paths"][0]["cues"][0]["takes"]) == 2
    assert not rep["paths"][0]["cues"][1]["fit"]["fits"]

    # nothing written anywhere carries the key
    for f in list(out.rglob("*")) + list((tmp_path / "cache").rglob("*")):
        if f.is_file():
            assert FAKE_KEY.encode() not in f.read_bytes()
    assert FAKE_KEY not in log

    # a second run is served from the cache
    fake2 = FakeEleven()
    assert stage.run(args, post=fake2) == 0
    assert fake2.calls == []


def test_dry_run_and_bad_script(tmp_path, monkeypatch, capsys):
    args, out = _setup(tmp_path, monkeypatch, [_cue("a", 0, 1.0, "One line."), _cue("b", 0, 0.5, "Two.")])
    args.dry_run = True
    assert stage.run(args, post=FakeEleven()) == 1        # b starts before a
    assert "not after the previous cue" in capsys.readouterr().out
    args, out = _setup(tmp_path, monkeypatch, [_cue("a", 0, 1.0, "One line.")])
    args.dry_run = True
    fake = FakeEleven()
    assert stage.run(args, post=fake) == 0 and fake.calls == [] and not out.exists()
