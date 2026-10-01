"""``python -m musichistory narration [--path ID ...] [--script FILE ...] [--dry-run]``: speak every
featured path's narration script over its mashup (DESIGN §15).

For every cue of ``musichistory/narration/scripts/<path id>.json`` (or the ``--script`` files):

1. **Shape** the line (``textshape``): declarative sentences ending in periods.
2. **Speak** it with ElevenLabs (``tts``; voice JohnV4, model eleven_v4, steady settings), cached
   by sha256 of (voice, model, settings, text) under ``data/cache/narration/``.
3. **Check the inflection** (``inflection``): split at the sentence pauses, pYIN the last ~0.35 s
   of each sentence, require a falling slope. A cue with a rising (or unmeasurable) end is
   generated again with slightly different settings (``--tries``, default 3 in all); the best
   take (most falling ends) is kept and its count recorded as ``inflection {falls, of}``.
4. **Master** it (``audio``): 48 kHz mono, trimmed, -16 LUFS (true peak <= -1 dBTP), written to
   ``data/audio/narration/<path id>/<nn>_<cue id>.wav``.
5. **Place and duck**: ``at`` = anchor segment start + offset; ``duck_db`` puts the voice's
   speech band (300 Hz - 4 kHz) 10 dB above the mix's speech band under the line, clamped to
   [-18, -6] dB. A line that runs into the next cue (or past the mix) is reported.
6. **Export** ``narration.json`` (``contract``; other paths already in it are kept) and
   ``narration_report.json`` with every measurement and the API usage.

The ElevenLabs key is read from its file only when a request is sent and is never printed,
logged or stored.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path

from .. import config
from . import audio, contract, inflection, script, textshape, tts

DEFAULT_STABILITY = 0.6
DEFAULT_SIMILARITY = 0.75
QUANTIZED_STABILITY = (0.0, 0.5, 1.0)   # models that only accept these (eleven_v3 style)


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--path", action="append", help="path id to narrate (repeatable; default every script)")
    p.add_argument("--script", action="append", type=Path,
                   help="narrate this script file instead of musichistory/narration/scripts/ (repeatable)")
    p.add_argument("--out", type=Path, default=None, help="output folder (default data/audio/narration)")
    p.add_argument("--key-file", type=Path, default=None, help="file holding the ElevenLabs key (default: tts.KEY_FILE)")
    p.add_argument("--stability", type=float, default=DEFAULT_STABILITY, help="ElevenLabs stability (default 0.6)")
    p.add_argument("--similarity", type=float, default=DEFAULT_SIMILARITY, help="ElevenLabs similarity_boost (default 0.75)")
    p.add_argument("--tries", type=int, default=3, help="takes per cue at most, until every sentence falls (default 3)")
    p.add_argument("--max-requests", type=int, default=200, help="stop before sending more API requests than this")
    p.add_argument("--dry-run", action="store_true", help="validate scripts and print the fit plan; no API calls")
    p.add_argument("--captions-only", action="store_true",
                   help="write caption cues without speaking them (no API calls); without --path only for "
                        "paths that have no narration yet")


def _log(msg: str = "") -> None:
    print(msg, flush=True)


def out_dir(args: argparse.Namespace) -> Path:
    return Path(args.out) if args.out else config.DATA / "audio" / "narration"


def mashups_root() -> Path:
    return config.DATA / "audio" / "mashups"


def _write_json(path: Path, doc, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=indent, ensure_ascii=False, separators=None if indent else (",", ":"))
                   + "\n", encoding="utf-8")
    os.replace(tmp, path)


def takes(stability: float, similarity: float, quantized: bool = False) -> list[tuple[dict, str]]:
    """Settings and text suffix of each take: steady first, then a slightly steadier one with a
    trailing period-space, then a slightly looser one with more similarity."""
    def s(x: float) -> float:
        x = min(1.0, max(0.0, x))
        return min(QUANTIZED_STABILITY, key=lambda q: abs(q - x)) if quantized else round(x, 2)

    sim = round(min(1.0, max(0.0, similarity)), 2)
    out = [({"stability": s(stability), "similarity_boost": sim}, ""),
           ({"stability": s(stability + 0.05), "similarity_boost": sim}, " "),
           ({"stability": s(stability - 0.05), "similarity_boost": round(min(1.0, sim + 0.05), 2)}, "")]
    if quantized:   # stability cannot vary: vary similarity instead
        out[1] = ({"stability": s(stability), "similarity_boost": round(max(0.0, sim - 0.05), 2)}, " ")
    return out


class CueSpeaker:
    """Speaks cues with retries on rising endings; holds the run's API usage."""

    def __init__(self, args: argparse.Namespace, post=None, cache: Path | None = None):
        self.args = args
        self.post = post
        self.cache = cache
        self.usage = tts.Usage()
        self.quantized = False

    def _synth(self, text: str, settings: dict):
        return tts.synthesize(text, settings, cache=self.cache, post=self.post, key_file=self.args.key_file,
                              usage=self.usage, max_requests=self.args.max_requests)

    def speak(self, text: str) -> tuple:
        """(best take samples, their rate, its inflection result, report of every take)."""
        n = len(textshape.sentences(text))
        reports, best = [], None
        for k in range(max(1, self.args.tries)):
            plan = takes(self.args.stability, self.args.similarity, self.quantized)
            settings, suffix = plan[min(k, len(plan) - 1)]
            try:
                y, sr, ident, cached = self._synth(text + suffix, settings)
            except tts.TTSError as e:
                if e.status in (400, 422) and "stab" in str(e).lower() and not self.quantized:
                    _log("    the model rejects this stability; using its fixed stability steps")
                    self.quantized = True
                    settings, suffix = takes(self.args.stability, self.args.similarity, True)[min(k, 2)]
                    y, sr, ident, cached = self._synth(text + suffix, settings)
                else:
                    raise
            y = audio.trim(y, sr)
            res = inflection.check(y, sr, n)
            reports.append({"take": k + 1, "settings": settings, "suffix": suffix, "identity": ident[:16],
                            "cached": cached, "inflection": res})
            if best is None or res["falls"] > best[1]["falls"]:
                best = (y, res, sr)
            if res["ok"]:
                break
        return best[0], best[2], best[1], reports


def narrate_path(doc: dict, mashup: dict, speaker: CueSpeaker, root: Path) -> tuple[dict, dict]:
    pid = doc["id"]
    mix_path = mashups_root() / mashup["file"]
    music, msr = audio.read_mono(mix_path)
    cues, rep_cues = [], []
    ats = [script.cue_time(c, mashup) for c in doc["cues"]]
    for i, c in enumerate(doc["cues"]):
        text = textshape.shape(c["text"])
        _log(f"  {i + 1:02d} {c['id']} @ {ats[i]:.2f}s: {textshape.words(text)} words, "
             f"{len(textshape.sentences(text))} sentence(s)")
        y24, sr, inf, takes_rep = speaker.speak(text)
        y = audio.resample(y24, sr, audio.OUT_RATE)
        y = audio.trim(y, audio.OUT_RATE)
        y, loud = audio.normalize(y, audio.OUT_RATE)
        rel = contract.cue_file(pid, i, c["id"])
        audio.write_wav(root / rel, y)
        seconds = round(len(y) / audio.OUT_RATE, 3)
        speech_db = audio.band_level(y, audio.OUT_RATE)
        music_db = audio.band_level(music, msr, ats[i], ats[i] + seconds)
        duck = audio.duck_db(speech_db, music_db)
        nxt = ats[i + 1] if i + 1 < len(ats) else float(mashup["seconds"])
        overrun = round(ats[i] + seconds - nxt, 2)
        fit = {"room": round(nxt - ats[i], 2), "overrun": max(0.0, overrun), "fits": overrun <= 0,
               "limit": "next cue" if i + 1 < len(ats) else "mix end"}
        cues.append({"id": c["id"], "at": ats[i], "seconds": seconds, "file": rel, "text": text, "duck_db": duck,
                     "image": c.get("image"), "sources": [{"title": s["title"], "url": s["url"]} for s in c.get("sources", [])],
                     "inflection": {"falls": inf["falls"], "of": inf["of"]}})
        rep_cues.append({"id": c["id"], "at": ats[i], "seconds": seconds, "takes": takes_rep, "chosen_inflection": inf,
                         "loudness": loud, "speech_band_db": round(speech_db, 2),
                         "music_band_db": None if not math.isfinite(music_db) else round(music_db, 2),
                         "duck_db": duck, "fit": fit})
        slopes = ", ".join("?" if e["slope_st_per_s"] is None else f"{e['slope_st_per_s']:+.1f}" for e in inf["ends"])
        _log(f"     {seconds:.2f}s, inflection {inf['falls']}/{inf['of']} falling (st/s: {slopes}), "
             f"{len(takes_rep)} take(s), {loud['lufs']} LUFS, TP {loud['true_peak']} dBTP, "
             f"music band {rep_cues[-1]['music_band_db']} dB -> duck {duck} dB")
        if not fit["fits"]:
            _log(f"     WARNING: runs {fit['overrun']:.2f}s past the {fit['limit']} (room {fit['room']:.2f}s)")
        if inf["falls"] < inf["of"]:
            _log(f"     WARNING: {inf['of'] - inf['falls']} sentence end(s) not measured falling after {len(takes_rep)} take(s)")
    return {"id": pid, "cues": cues}, {"id": pid, "cues": rep_cues}


def caption_path(doc: dict, mashup: dict) -> dict:
    """A path's cues as captions only: no audio, the reading time estimated at ``script.WORDS_PER_SECOND``."""
    ats = [script.cue_time(c, mashup) for c in doc["cues"]]
    cues = []
    for i, c in enumerate(doc["cues"]):
        text = textshape.shape(c["text"])
        seconds = round(max(1.0, textshape.words(text) / script.WORDS_PER_SECOND), 3)
        cues.append({"id": c["id"], "at": ats[i], "seconds": seconds, "file": None, "text": text, "duck_db": None,
                     "image": c.get("image"), "sources": [{"title": s["title"], "url": s["url"]} for s in c.get("sources", [])],
                     "inflection": None})
    return {"id": doc["id"], "cues": cues}


def _load_scripts(args: argparse.Namespace) -> list[tuple[Path, dict]]:
    files = list(args.script) if args.script else script.script_files(args.path)
    out = []
    for f in files:
        doc = script.load(f)
        if args.script and args.path and doc.get("id") not in args.path:
            continue
        out.append((Path(f), doc))
    return out


def run(args: argparse.Namespace, post=None) -> int:
    t0 = time.monotonic()
    mpath = mashups_root() / "mashups.json"
    if not mpath.is_file():
        _log(f"error: {mpath} missing: run the mashup stage first")
        return 2
    mashups = json.loads(mpath.read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in mashups["paths"]}
    scripts = _load_scripts(args)
    if not scripts:
        _log("no narration scripts found (musichistory/narration/scripts/*.json or --script)")
        return 2
    bad = False
    for f, doc in scripts:
        pid = doc.get("id")
        errs = script.validate(doc, by_id.get(pid))
        if pid not in by_id:
            errs.append(f"no mashup for path {pid!r}")
        if not args.script and f.stem != pid:
            errs.append(f"file name {f.name} does not match id {pid!r}")
        for e in errs:
            _log(f"{f.name}: {e}")
        bad |= bool(errs)
    if bad:
        return 1
    chars = 0
    for f, doc in scripts:
        plan = script.fit_plan(doc, by_id[doc["id"]])
        tight = [p for p in plan if not p["fits"]]
        chars += sum(len(textshape.shape(c["text"])) for c in doc["cues"])
        _log(f"{doc['id']}: {len(plan)} cues, estimated fit {len(plan) - len(tight)}/{len(plan)}")
        for p in tight:
            _log(f"  WARNING: {p['id']} needs ~{p['estimate']:.1f}s at 2.6 words/s, has {p['room']:.1f}s")
    _log(f"{chars} characters to speak (first takes; cached lines cost nothing)")
    if args.dry_run:
        return 0

    root = out_dir(args)
    if getattr(args, "captions_only", False):
        return _run_captions(args, scripts, mashups, by_id, root)
    speaker = CueSpeaker(args, post=post)
    entries, reports, failed = {}, {}, {}
    for f, doc in scripts:
        _log(f"\n{doc['id']}: {doc.get('title', '')}")
        try:
            entry, rep = narrate_path(doc, by_id[doc["id"]], speaker, root)
        except tts.TTSError as e:
            failed[doc["id"]] = str(e)
            _log(f"  FAILED: {e}")
            if e.status in (401, 403) or "budget" in str(e):
                break
            continue
        entries[doc["id"]] = entry
        reports[doc["id"]] = rep

    doc_path = root / "narration.json"
    old = {}
    if doc_path.is_file():
        try:
            prev = json.loads(doc_path.read_text(encoding="utf-8"))
            if prev.get("version") == contract.VERSION:
                old = {p["id"]: p for p in prev.get("paths", []) if p.get("id") in by_id}
        except ValueError:
            old = {}
    merged = {**old, **entries}
    order = [p["id"] for p in mashups["paths"]]
    out = {"version": contract.VERSION, "voice": tts.VOICE_ID, "voice_name": tts.VOICE_NAME, "model": tts.MODEL,
           "paths": [merged[i] for i in order if i in merged]}
    usage = {"requests": speaker.usage.requests, "characters": speaker.usage.characters,
             "cache_hits": speaker.usage.cache_hits}
    _log(f"\nAPI usage: {usage['requests']} request(s), {usage['characters']} characters sent, "
         f"{usage['cache_hits']} cache hit(s)")
    if not entries:
        return 1
    errors = contract.validate(out, root, mashups)
    if errors:
        for e in errors[:40]:
            _log(f"contract error: {e}")
        return 1
    _write_json(doc_path, out)
    rep_path = root / "narration_report.json"
    old_rep = {}
    if rep_path.is_file():
        try:
            old_rep = {r["id"]: r for r in json.loads(rep_path.read_text(encoding="utf-8")).get("paths", [])}
        except ValueError:
            old_rep = {}
    rmerged = {**old_rep, **reports}
    _write_json(rep_path, {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "usage": usage,
                           "settings": {"stability": args.stability, "similarity_boost": args.similarity,
                                        "tries": args.tries, "quantized_stability": speaker.quantized},
                           "paths": [rmerged[i] for i in order if i in rmerged], "failed": failed}, indent=1)
    _log(f"wrote {doc_path} ({len(out['paths'])} path(s)) and {rep_path} in {time.monotonic() - t0:.0f}s")
    return 1 if failed else 0


def _run_captions(args: argparse.Namespace, scripts, mashups: dict, by_id: dict, root: Path) -> int:
    """``--captions-only``: merge caption cues into narration.json, keeping voiced paths as they are."""
    doc_path = root / "narration.json"
    old = {}
    if doc_path.is_file():
        prev = json.loads(doc_path.read_text(encoding="utf-8"))
        if prev.get("version") == contract.VERSION:
            old = {p["id"]: p for p in prev.get("paths", []) if p.get("id") in by_id}
    entries = {}
    for _, doc in scripts:
        if not args.path and doc["id"] in old:
            continue
        entries[doc["id"]] = caption_path(doc, by_id[doc["id"]])
        _log(f"{doc['id']}: {len(entries[doc['id']]['cues'])} caption cue(s)")
    merged = {**old, **entries}
    order = [p["id"] for p in mashups["paths"]]
    out = {"version": contract.VERSION, "voice": tts.VOICE_ID, "voice_name": tts.VOICE_NAME, "model": tts.MODEL,
           "paths": [merged[i] for i in order if i in merged]}
    errors = contract.validate(out, root, mashups)
    if errors:
        for e in errors[:40]:
            _log(f"contract error: {e}")
        return 1
    _write_json(doc_path, out)
    _log(f"wrote {doc_path} ({len(out['paths'])} path(s); {len(entries)} captions-only)")
    return 0
