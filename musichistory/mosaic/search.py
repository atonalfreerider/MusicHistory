"""The search over the whole corpus: every viable target's loop, its best cover and harmonies.

* **Corpus** (``load_corpus``): every song with a preview, Demucs stems and the mashup analysis
  (``data/audio/stems/<work_id>/analysis.json``), its measured key (``paths.measured``, the
  same key the featured paths use), title/artist/year from the graph database and its notes
  (``notes.py``). A song is **no source** when it is marked instrumental (``singer`` table of
  the pipeline database), its vocal stem is separation residue (more than
  ``tracks.NO_VOCAL_DB`` under the mix) or its preview is not the song's recording (another
  title, a live take, remix or re-recording, as the paths stage flags them).
* **Meter**: bars as ``mashup.tracks`` counts them - the measured meter when triple, else
  4-beat bars (regrouped at the phase where the chords change most); the notes' downbeat phase
  is the track's.
* **Tempo octave**: a song whose beat_this grid runs faster than ``FAST_BPM`` (or slower than
  ``SLOW_BPM``) is counted on every other beat (or on half beats) - notes, bars and the loop
  alike - so onset tolerances in beats mean about the same time in every song.
* **Targets** must also have a clear vocal (at most ``CLEAR_VOCAL_DB`` under the mix) and a
  loop (``assemble.choose_loop``) of 4-8 bars with enough sung notes.
* **Per target** (``evaluate_target``): its loop, which must be a clear melody (at most
  ``MAX_REPEAT_SHARE`` repeated notes, ``MIN_LOOP_PITCHES`` pitches over ``MIN_LOOP_RANGE``
  semitones: no chanting or rap), every piece of every other song (``match.find_pieces``;
  songs of the same title - covers of the same composition - are excluded) whose span moves
  (``melodic_piece``), the pieces pruned per span and scheduled (``assemble.cover``).
* **Harmonies** (``harmonize``) for the best targets: ``harmony.voices`` over every other
  song.

The work runs in a process pool whose workers each load the corpus once (``_init``).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np

from .. import config
from ..mashup import analysis, tracks
from ..paths import audio as paths_audio
from ..paths import graph as graph_mod
from ..paths import measured as measured_mod
from . import assemble, harmony, match
from . import notes as notes_mod

CLEAR_VOCAL_DB = -14.0
FAST_BPM = 165.0               # a tracker grid faster than this is counted in half time ...
SLOW_BPM = 60.0                # ... and one slower than this in double time
WEIGHTS = dict(length_bonus=assemble.LENGTH_BONUS, miss_cost=assemble.MISS_COST, piece_cost=assemble.PIECE_COST)
MIN_CHANGES = 2                # a piece must move: at least this many pitch changes ...
CHANGE_SHARE = 0.35            # ... and this share of its intervals (a repeated-note chant proves nothing)
MAX_REPEAT_SHARE = 0.45        # a target loop repeating its pitch more often is chanted or rapped, not a melody
MIN_LOOP_PITCHES = 5           # ... and one needs this many distinct pitches
MIN_LOOP_RANGE = 5             # ... over at least this range (semitones)


@dataclass
class Song:
    work_id: str
    title: str
    artist: str
    year: int
    key: str
    tonic: int
    mode: str
    notes: notes_mod.SongNotes
    seq: match.Seq
    norm_title: str
    source_ok: bool
    reason: str = ""                   # why it is no source (empty: it is one)

    def track(self) -> tracks.Track:
        """The song's bars and chords (``mashup.tracks``) on the same grid as its notes."""
        return song_track(self.work_id, self.key, self.title, self.artist, self.year).regrid(self.notes.factor)


def song_track(work_id: str, key: str, title: str = "", artist: str = "", year: int = 0) -> tracks.Track:
    """``mashup.tracks.from_analysis`` in the measured meter when it is triple (3 or 6 beats a
    bar), else regrouped into 4-beat bars (a duple-counted song's bars become whole bars)."""
    doc = json.loads(analysis.analysis_path(work_id).read_text(encoding="utf-8"))
    own = int(doc.get("beats_per_bar") or 4)
    return tracks.from_analysis(work_id, doc, key, bpb=own if own in (3, 6) else 4, title=title, artist=artist,
                                year=year)


def octave_factor(bpm: float) -> float:
    return 0.5 if bpm > FAST_BPM else 2.0 if bpm < SLOW_BPM else 1.0


def instrumental_works() -> set[str]:
    if not config.PIPELINE_DB.is_file():
        return set()
    conn = sqlite3.connect(f"file:{config.PIPELINE_DB.as_posix()}?mode=ro", uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='singer'").fetchone():
            return set()
        return {r[0] for r in conn.execute("SELECT work_id FROM singer WHERE gender = 'instrumental'")}
    finally:
        conn.close()


def load_corpus(log: Callable[[str], None] = print, workers: int | None = None) -> dict[str, Song]:
    adir = config.DATA / "audio"
    g = graph_mod.load(config.GRAPH_DB, adir)
    an = paths_audio.load_cache(adir / "analysis.json")
    meas = {m.work_id: m for m in measured_mod.resolve_all(g, an).values()}
    songs = {s.work_id: s for s in g.songs.values() if s.preview is not None}
    ids = [w for w in songs if w in meas and analysis.analysis_path(w).is_file()]
    sn, failed = notes_mod.load_all(ids, workers=workers, log=log)
    instr = instrumental_works()
    out: dict[str, Song] = {}
    for w in ids:
        if w not in sn:
            continue
        s, m, n = songs[w], meas[w], sn[w]
        n = n.regrid(octave_factor(n.bpm), song_track(w, m.key_name).phase)
        seq, _ = assemble.stable_seq(n)
        reason = ""
        if w in instr:
            reason = "instrumental"
        elif n.vocal_rel_db < tracks.NO_VOCAL_DB:
            reason = f"no sung vocal (vocal stem {n.vocal_rel_db:+.1f} dB re mix)"
        elif s.preview_issue:
            reason = f"preview is not the song's recording ({s.preview_issue})"
        elif len(seq) < match.MIN_PIECE:
            reason = "no notes"
        out[w] = Song(w, s.title, s.artist, int(s.year), m.key_name, m.tonic, m.mode, n, seq,
                      graph_mod._norm(s.title), not reason, reason)
    return out


# --------------------------------------------------------------------------- workers
_STATE: dict = {}


def _init() -> None:
    corpus = load_corpus(log=lambda _m: None, workers=1)
    seqs = {w: s.seq for w, s in corpus.items() if s.source_ok}
    _STATE.update(corpus=corpus, index=match.Index(seqs),
                  rolls={w: (harmony.source_roll(q), corpus[w].notes.ibi) for w, q in seqs.items()})


def excluded(corpus: dict[str, Song], wid: str) -> set[str]:
    t = corpus[wid]
    return {w for w, s in corpus.items() if w == wid or not s.source_ok or (t.norm_title and s.norm_title == t.norm_title)}


def piece_doc(p: match.Piece) -> dict:
    return {"work_id": p.work_id, "a": p.a, "b": p.b, "sa": p.sa, "sb": p.sb, "fold": p.fold, "offset": p.offset,
            "transpose": p.transpose, "shift": p.shift, "matched": p.matched, "n_target": p.n_target,
            "n_source": p.n_source, "rate": round(p.rate, 4), "tempo_ratio": round(p.tempo_ratio, 4),
            "weight": round(p.weight, 3), "pairs": p.pairs}


def piece_from(d: dict) -> match.Piece:
    p = match.Piece(d["work_id"], d["a"], d["b"], d["sa"], d["sb"], d["fold"], d["offset"], d["transpose"],
                    d["matched"], d["n_target"], d["n_source"], d["tempo_ratio"], d["weight"])
    p.pairs = [tuple(x) for x in d["pairs"]]
    return p


def melodic_piece(target: match.Seq, p: match.Piece) -> bool:
    """The target span a piece rebuilds moves enough (``MIN_CHANGES``, ``CHANGE_SHARE``)."""
    seg = target.pitch[p.a:p.b + 1]
    changes = int(np.sum(np.diff(seg) != 0))
    return changes >= max(MIN_CHANGES, int(np.ceil(CHANGE_SHARE * (len(seg) - 1))))


def melody_problem(seq: match.Seq) -> str:
    """Why a loop's notes are no clear melody ('' when they are)."""
    p = seq.pitch
    if len(p) < 2:
        return "no melody"
    rep = float(np.mean(np.diff(p) == 0))
    if rep > MAX_REPEAT_SHARE:
        return f"chanted, not a melody ({rep:.0%} repeated notes)"
    if len(set(p.tolist())) < MIN_LOOP_PITCHES or int(p.max() - p.min()) < MIN_LOOP_RANGE:
        return f"too few pitches ({len(set(p.tolist()))} over {int(p.max() - p.min())} semitones)"
    return ""


def evaluate_target(wid: str) -> dict:
    corpus: dict[str, Song] = _STATE["corpus"]
    s = corpus[wid]
    t0 = time.monotonic()
    out: dict = {"work_id": wid}
    if not s.source_ok:
        return {**out, "rejected": s.reason}
    if s.notes.vocal_rel_db < CLEAR_VOCAL_DB:
        return {**out, "rejected": f"vocal not clear ({s.notes.vocal_rel_db:+.1f} dB re mix)"}
    tr = s.track()
    loop = assemble.choose_loop(tr, s.notes, accept=lambda w: not melody_problem(w))
    if loop is None:
        loose = assemble.choose_loop(tr, s.notes)
        why = melody_problem(loose.notes) if loose is not None else ""
        return {**out, "rejected": why or "no 4-8 bar loop with enough sung notes"}
    target = loop.notes
    pieces = match.find_pieces(target, _STATE["index"], ibi_target=s.notes.ibi, exclude=excluded(corpus, wid),
                               **WEIGHTS)
    pieces = [p for p in pieces if melodic_piece(target, p)]
    chosen = assemble.cover(assemble.prune(pieces), len(target))
    mo = assemble.Mosaic(chosen, len(target))
    out.update({
        "loop": {"start_bar": loop.start_bar, "bars": loop.bars, "bpb": loop.bpb, "beat0": loop.beat0,
                 "seconds": loop.params["seconds"], "notes": len(target), "seam": loop.seam, "score": round(loop.score, 3),
                 "opens_tonic": loop.params["opens_tonic"]},
        "candidates": len(pieces), "pieces": [piece_doc(p) for p in chosen],
        "match": round(mo.match, 4), "coverage": round(mo.coverage, 4), "songs": mo.songs,
        "mean_notes": round(mo.mean_notes, 2), "mean_rate": round(mo.mean_rate, 4),
        "longest": max((p.matched for p in chosen), default=0), "quality": round(mo.quality(), 4),
        "seconds": round(time.monotonic() - t0, 2)})
    return out


def loop_of(s: Song, d: dict) -> tuple[tracks.Track, assemble.Loop]:
    """The target's track and the loop a search result chose."""
    tr = s.track()
    lp = d["loop"]
    b0 = int(lp["beat0"])
    seq, stab = assemble.stable_seq(s.notes)
    sel = (seq.on >= b0) & (seq.on < b0 + lp["bars"] * lp["bpb"])
    loop = assemble.Loop(s.work_id, int(lp["start_bar"]), int(lp["bars"]), int(lp["bpb"]), b0,
                         seq.window(b0, b0 + lp["bars"] * lp["bpb"]), stab[sel], float(lp["score"]), lp["seam"])
    return tr, loop


def slot_chords(tr: tracks.Track, loop: assemble.Loop) -> list[list[int]]:
    return [tr.bar_chords(loop.start_bar + k) for k in range(loop.bars)]


def harmonize(wid: str, d: dict) -> dict:
    corpus: dict[str, Song] = _STATE["corpus"]
    s = corpus[wid]
    t0 = time.monotonic()
    tr, loop = loop_of(s, d)
    ctx = harmony.context(loop.notes, loop.beats, loop.bpb, slot_chords(tr, loop), s.notes.ibi)
    vs = harmony.voices(ctx, _STATE["rolls"], exclude=excluded(corpus, wid))
    return {"work_id": wid, "voices": [{"work_id": v.work_id, "fold": v.fold, "start": v.start, "shift": v.shift,
                                        "tempo_ratio": round(v.tempo_ratio, 4), **v.stats()} for v in vs],
            "seconds": round(time.monotonic() - t0, 2)}


def run_pool(fn: Callable, jobs: list[tuple], workers: int, log: Callable[[str], None], label: str) -> list[dict]:
    out = []
    if workers <= 1:
        if not _STATE:
            _init()
        for k, job in enumerate(jobs, 1):
            out.append(fn(*job))
            if k % 50 == 0:
                log(f"    {label} {k}/{len(jobs)}")
        return out
    with ProcessPoolExecutor(max_workers=workers, initializer=_init) as pool:
        futs = [pool.submit(fn, *job) for job in jobs]
        for k, fut in enumerate(as_completed(futs), 1):
            out.append(fut.result())
            if k % 50 == 0:
                log(f"    {label} {k}/{len(jobs)}")
    return out


# --------------------------------------------------------------------------- ranking
def harmony_quality(h: dict | None) -> float:
    if not h or not h["voices"]:
        return 0.0
    v = h["voices"]
    q = v[0]["score"]
    if len(v) > 1:
        q += 0.25 * v[1]["score"]
    return float(min(1.0, q / 1.1))


def total(r: dict, h: dict | None) -> float:
    return round(0.75 * float(r.get("quality", 0.0)) + 0.25 * harmony_quality(h), 4)


def eligible(r: dict) -> str:
    """Why a searched target cannot be an example ('' when it can)."""
    if "rejected" in r:
        return r["rejected"]
    if r["songs"] < 2:
        return "fewer than two source songs"
    if r["match"] < 0.5:
        return f"match under 0.5 ({r['match']:.2f})"
    return ""


def np_default(o):
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(type(o))
