"""Featured paths: 3-6 songs, every consecutive pair joined by a graph edge (tree or
secondary) from the earlier to the later song, every song with a trusted preview.

Scoring (``score_path``; all terms are reported so a curator can see why a path ranks):

* **identity** of each hop (``identity_weight``): exact shared passages (strong matches) and
  recognizable named identities (Pachelbel ground, Andalusian cadence, doo-wop, axis
  progression, 12-bar blues, descending chromatic bass, ...) weigh most; unnamed 3-4 chord
  loops less; two-chord vamps and bare ii-V-I / I-IV-V-I cadences are *generic* (at most one
  per path, penalized);
* **smoothness** of each handoff with the measured keys and tempos (``plan.plan_step``):
  |start_semitones| <= 4 and a start/end tempo ratio within 0.8-1.25 after folding are free
  up to small costs; anything beyond is penalized steeply;
* **audibility**: the shared identity found in the clips' chord/bass readings
  (``identity.py``);
* **story**: fame (canon rank), era span, and coherence (the same identity carried through,
  or a handoff between strong matches), a mild preference for 4-5 songs;
* **measurement doubt**: a song whose measured tempo differs from its MIDI tempo by more
  than 15 % (after the octave), or whose audio key is not the MIDI key, costs a little, since
  one of the two readings is off and the handoff may not sound as planned.

``select`` picks a diverse set greedily (no song in two paths, an identity in at most two).
The committed ``curated.json`` fixes the actual choice (work_id sequences, titles,
descriptions); ``validate_curated`` checks it against the rules.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import audio, identity
from .graph import Edge, Graph, Song
from .measured import Measured
from .plan import plan_step

CURATED_PATH = Path(__file__).with_name("curated.json")
MIN_SONGS, MAX_SONGS = 3, 6
MAX_SEMITONES = 4
TEMPO_RANGE = (0.8, 1.25)
MIN_PREVIEW_ARTIST = 0.6          # the iTunes artist must resemble ours (else: a cover or a wrong match)

NAMED = [  # (label prefix, weight, category)
    ("Pachelbel ground", 1.0, "chord progression"),
    ("Andalusian cadence", 0.95, "chord progression"),
    ("doo-wop progression", 0.9, "chord progression"),
    ("axis progression", 0.85, "chord progression"),
    ("12-bar blues", 0.85, "12-bar blues"),
    ("descending chromatic bass", 0.85, "bass line"),
    ("bVII-bVI shuttle", 0.7, "chord progression"),
    ("Aeolian progression", 0.7, "chord progression"),
    ("Mixolydian vamp", 0.65, "chord progression"),
    ("I-vi-ii-V turnaround", 0.65, "chord progression"),
    ("iio-V-i cadence", 0.35, "cadence"),
    ("ii-V-I cadence loop", 0.3, "cadence"),
    ("ii-V-I cadence", 0.1, "cadence"),
    ("I-IV-V-I cadence", 0.1, "cadence"),
    ("two-chord vamp", 0.1, "vamp"),
]
GENERIC_MAX = 0.15


def identity_weight(edge: Edge, n_songs: int = 1012) -> tuple[float, str]:
    """(weight 0..1, category) of the identity an edge shares."""
    ev = edge.evidence or ""
    if edge.strong:
        m = re.search(r"exact (melody|chord|bass)\b.*?(\d+)", ev)
        what, count = (m.group(1), int(m.group(2))) if m else ("", 0)
        cat = {"melody": "exact melody", "chord": "exact chord passage", "bass": "bass riff"}.get(what, "exact passage")
        return min(1.0, 0.85 + 0.01 * count), cat
    for prefix, w, cat in NAMED:
        if ev.startswith(prefix):
            return w, cat
    # Unnamed loop, labelled by its roman numerals: longer and rarer loops are more telling.
    n_chords = len(ev.split("-")) if re.fullmatch(r"[b#]*[IViv]+o?(-[b#]*[IViv]+o?)+", ev) else 0
    base = 0.5 if n_chords >= 4 else 0.35 if n_chords == 3 else 0.2
    size = edge.family.size if edge.family else n_songs
    spec = math.log2(n_songs / max(size, 1)) / math.log2(n_songs)
    return round(base * (0.7 + 0.3 * spec), 3), "chord progression"


def trusted(song: Song) -> bool:
    """The preview is the song's own recording: the iTunes artist resembles ours and the track
    is not another title, a re-recording, a remix or a live take (``graph.preview_issue``)."""
    return (song.preview is not None and not song.preview_issue
            and (song.preview_artist is None or song.preview_artist >= MIN_PREVIEW_ARTIST))


# --------------------------------------------------------------------------- hops
@dataclass
class Hop:
    edge: Edge
    weight: float
    category: str
    semitones: int
    start_bpm: float
    bpm: float
    src_check: identity.ClipCheck | None = None
    dst_check: identity.ClipCheck | None = None
    pair: identity.PairCheck | None = None

    @property
    def ratio(self) -> float:
        return self.start_bpm / self.bpm if self.bpm > 0 else 1.0

    @property
    def generic(self) -> bool:
        return self.weight <= GENERIC_MAX

    @property
    def smooth(self) -> bool:
        return abs(self.semitones) <= MAX_SEMITONES and TEMPO_RANGE[0] - 1e-9 <= self.ratio <= TEMPO_RANGE[1] + 1e-9

    def smooth_cost(self) -> float:
        s = abs(self.semitones)
        key = {0: 0.0, 1: 0.0, 2: 0.02, 3: 0.08, 4: 0.15}.get(s, 0.5 + 0.2 * (s - 5))
        d = abs(math.log2(self.ratio)) if self.ratio > 0 else 9.0
        lim = math.log2(TEMPO_RANGE[1])
        tempo = 0.0 if d <= 0.12 else 0.12 * (d - 0.12) / (lim - 0.12) if d <= lim else 0.12 + 2.0 * (d - lim)
        return key + tempo

    def audible(self) -> float:
        """-0.1 .. +0.15: the identity heard in both clips (in key), one, or none."""
        if self.pair is not None and self.pair.passed is not None:
            return 0.15 if self.pair.passed else -0.05
        checks = [c for c in (self.src_check, self.dst_check) if c is not None and c.checked]
        if not checks:
            return 0.0
        heard = sum(1 for c in checks if c.in_key)
        return {0: -0.1, 1: 0.05, 2: 0.15}[heard] if len(checks) == 2 else (0.05 if heard else -0.05)


class Checker:
    """Identity checks with caching, and the chroma-similarity null for strong melody hops."""

    def __init__(self, graph: Graph, measured: dict[int, Measured], null_size: int = 40, seed: int = 7) -> None:
        self.g, self.m = graph, measured
        self.null_size, self.seed = null_size, seed
        self._clip: dict[tuple[int, int], identity.ClipCheck] = {}
        self._frame: dict[int, np.ndarray] = {}
        self._null: dict[int, list[float]] = {}

    def clip(self, nid: int, edge: Edge) -> identity.ClipCheck | None:
        fam = edge.family
        m = self.m.get(nid)
        if fam is None or m is None:
            return None
        key = (nid, fam.family_id)
        if key not in self._clip:
            self._clip[key] = identity.check_clip(fam.kind, fam.label, fam.roman, m.chords, m.bass, m.tonic, m.mode)
        return self._clip[key]

    def frame_chroma(self, nid: int) -> np.ndarray:
        if nid not in self._frame:
            m = self.m[nid]
            self._frame[nid] = identity.roll_to_frame(audio.unpack(m.beat_chroma), m.tonic, m.mode)
        return self._frame[nid]

    def null(self, nid: int) -> list[float]:
        if nid not in self._null:
            rng = random.Random(self.seed * 100003 + nid)
            others = sorted(k for k in self.m if k != nid)
            pick = rng.sample(others, min(self.null_size, len(others)))
            a = self.frame_chroma(nid)
            self._null[nid] = sorted(identity.best_window_similarity(a, self.frame_chroma(o)) for o in pick)
        return self._null[nid]

    def pair(self, edge: Edge) -> identity.PairCheck | None:
        fam = edge.family
        if fam is None or fam.kind != "strong":
            return None
        a, b = self.m.get(edge.source), self.m.get(edge.target)
        if a is None or b is None:
            return None
        if "chord" in fam.label:
            ra = identity.relative_chords(a.chords, a.tonic, a.mode)
            rb = identity.relative_chords(b.chords, b.tonic, b.mode)
            run = identity.longest_common_run(ra, rb)
            return identity.PairCheck("chord_run", run >= identity.CHORD_RUN_MIN, float(run))
        sim = identity.best_window_similarity(self.frame_chroma(edge.source), self.frame_chroma(edge.target))
        null = self.null(edge.source)
        pct = sum(1 for v in null if v < sim) / len(null) if null else 0.0
        return identity.PairCheck("chroma_similarity", pct >= 0.9, round(pct, 3), {"similarity": round(sim, 3)})


def make_hop(graph: Graph, measured: dict[int, Measured], edge: Edge, checker: Checker | None = None) -> Hop:
    a, b = measured[edge.source], measured[edge.target]
    p = plan_step(a.tonic, a.bpm, b.tonic, b.bpm, beats_per_bar=graph.songs[edge.target].beats_per_bar)
    w, cat = identity_weight(edge, len(graph.songs))
    hop = Hop(edge, w, cat, p.start_semitones, p.start_bpm, p.bpm)
    if checker is not None:
        hop.src_check = checker.clip(edge.source, edge)
        hop.dst_check = checker.clip(edge.target, edge)
        hop.pair = checker.pair(edge)
    return hop


# --------------------------------------------------------------------------- paths
@dataclass
class Candidate:
    nodes: list[int]
    hops: list[Hop]
    score: float = 0.0
    terms: dict = field(default_factory=dict)

    @property
    def main_identity(self) -> str:
        return Counter(h.edge.evidence for h in self.hops).most_common(1)[0][0]

    def categories(self) -> list[str]:
        return [h.category for h in self.hops]


def fame(graph: Graph) -> dict[int, float]:
    ranks = sorted((s.canon_rank for s in graph.songs.values() if s.canon_rank), key=float)
    n = len(ranks)
    out = {}
    for nid, s in graph.songs.items():
        if not s.canon_rank or not n:
            out[nid] = 0.0
        else:
            pos = int(np.searchsorted(ranks, s.canon_rank))
            out[nid] = 1.0 - pos / n
    return out


TEMPO_DOUBT = 0.15   # |log(audio / MIDI tempo)| beyond this: the tempo reading is doubtful


def doubtful_tempo(m: Measured | None) -> bool:
    r = m.tempo_ratio if m is not None else float("nan")
    return m is not None and (m.bpm_source != "audio" or not r or math.isnan(r) or abs(math.log(r)) > TEMPO_DOUBT)


def doubtful_key(m: Measured | None) -> bool:
    """The key rests on one reading only: the audio's own best key is not the MIDI key."""
    return m is not None and (m.audio_tonic, m.audio_mode) != (m.midi_tonic, m.midi_mode)


def score_path(graph: Graph, nodes: list[int], hops: list[Hop], fame_of: dict[int, float],
               measured: dict[int, Measured] | None = None) -> tuple[float, dict]:
    weights = [h.weight for h in hops]
    labels = Counter(h.edge.evidence for h in hops)
    main, main_n = labels.most_common(1)[0]
    strong_chain = all(h.edge.strong for h in hops)
    coherence = 1.0 if main_n == len(hops) else (0.8 if strong_chain else main_n / len(hops) * 0.6)
    generic = sum(1 for h in hops if h.generic)
    years = [graph.songs[n].year for n in nodes]
    span = max(years) - min(years)
    terms = {
        "identity": round(float(np.mean(weights)), 3),
        "weakest": round(min(weights), 3),
        "fame": round(float(np.mean([fame_of[n] for n in nodes])), 3),
        "span": round(min(1.0, span / 40.0), 3),
        "coherence": round(coherence, 3),
        "audible": round(float(np.mean([h.audible() for h in hops])), 3),
        "smooth_cost": round(float(np.mean([h.smooth_cost() for h in hops])), 3),
        "rough_hops": sum(1 for h in hops if not h.smooth),
        "generic_hops": generic,
        "doubtful_tempos": sum(1 for n in nodes if doubtful_tempo((measured or {}).get(n))),
        "doubtful_keys": sum(1 for n in nodes if doubtful_key((measured or {}).get(n))),
        "length": {3: 0.0, 4: 0.1, 5: 0.15, 6: 0.12}.get(len(nodes), 0.0),
    }
    score = (1.0 * terms["identity"] + 0.3 * terms["weakest"] + 0.6 * terms["fame"] + 0.3 * terms["span"]
             + 0.25 * terms["coherence"] + 1.0 * terms["audible"] - 1.0 * terms["smooth_cost"]
             - 0.3 * generic - 0.1 * terms["doubtful_tempos"] - 0.05 * terms["doubtful_keys"] + terms["length"])
    return round(score, 4), terms


def enumerate_paths(graph: Graph, measured: dict[int, Measured], checker: Checker | None = None,
                    min_songs: int = MIN_SONGS, max_songs: int = MAX_SONGS, max_generic: int = 1,
                    min_weight: float = 0.0) -> list[Candidate]:
    """Every path of min..max songs over edges between trusted, measured songs."""
    ok = {nid for nid, s in graph.songs.items() if trusted(s) and nid in measured}
    hop_of: dict[tuple[int, int], Hop] = {}
    for (a, b), e in graph.edges.items():
        if a in ok and b in ok and graph.songs[a].time_value < graph.songs[b].time_value:
            h = make_hop(graph, measured, e, checker)
            if h.weight >= min_weight:
                hop_of[(a, b)] = h
    nxt: dict[int, list[int]] = {}
    for a, b in hop_of:
        nxt.setdefault(a, []).append(b)
    fame_of = fame(graph)
    out: list[Candidate] = []

    def dfs(path: list[int], hops: list[Hop], generic: int) -> None:
        if len(path) >= min_songs:
            score, terms = score_path(graph, path, hops, fame_of, measured)
            out.append(Candidate(list(path), list(hops), score, terms))
        if len(path) == max_songs:
            return
        for b in nxt.get(path[-1], []):
            h = hop_of[(path[-1], b)]
            g = generic + (1 if h.generic else 0)
            if g > max_generic:
                continue
            path.append(b)
            hops.append(h)
            dfs(path, hops, g)
            path.pop()
            hops.pop()

    for start in sorted(nxt):
        dfs([start], [], 0)
    return out


def select(cands: list[Candidate], k: int, max_per_identity: int = 2, max_overlap: int = 0) -> list[Candidate]:
    """Greedy diverse pick: best score first; a song in at most one path (``max_overlap``
    shared songs allowed), a main identity in at most ``max_per_identity`` paths, and no path
    that is a sub-path of a chosen one."""
    chosen: list[Candidate] = []
    per_identity: Counter = Counter()
    for c in sorted(cands, key=lambda c: -c.score):
        s = set(c.nodes)
        if any(len(s & set(o.nodes)) > max_overlap for o in chosen):
            continue
        if per_identity[c.main_identity] >= max_per_identity:
            continue
        chosen.append(c)
        per_identity[c.main_identity] += 1
        if len(chosen) == k:
            break
    return chosen


# --------------------------------------------------------------------------- curated.json
@dataclass
class CuratedPath:
    id: str
    title: str
    description: str
    works: list[str]
    subtitle: str | None = None


def load_curated(path: Path = CURATED_PATH) -> list[CuratedPath]:
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for p in doc.get("paths") or []:
        out.append(CuratedPath(p["id"], p["title"], p.get("description", ""), list(p["works"]), p.get("subtitle")))
    return out


SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def validate_curated(graph: Graph, measured: dict[int, Measured], cp: CuratedPath) -> list[str]:
    """Rule violations of one curated path (empty = valid)."""
    errors = []
    if not SLUG.match(cp.id):
        errors.append(f"id {cp.id!r} is not a slug")
    if not (MIN_SONGS <= len(cp.works) <= MAX_SONGS):
        errors.append(f"{len(cp.works)} songs (need {MIN_SONGS}-{MAX_SONGS})")
    if len(set(cp.works)) != len(cp.works):
        errors.append("a song repeats")
    nodes = []
    for w in cp.works:
        nid = graph.by_work.get(w)
        if nid is None:
            errors.append(f"{w} is not in the graph")
            continue
        s = graph.songs[nid]
        if s.preview is None:
            errors.append(f"{w} ({s.title}) has no preview")
        elif not trusted(s):
            why = f"is a {s.preview_issue} mismatch" if s.preview_issue else "is by another artist"
            errors.append(f"{w} ({s.title}) preview {why} ({s.preview_label})")
        if nid not in measured:
            errors.append(f"{w} ({s.title}) is not measured")
        nodes.append(nid)
    if len(nodes) == len(cp.works):
        for a, b in zip(nodes, nodes[1:]):
            e = graph.edge(a, b)
            if e is None:
                errors.append(f"no edge {graph.songs[a].title} -> {graph.songs[b].title}")
            elif graph.songs[a].time_value >= graph.songs[b].time_value:
                errors.append(f"edge {graph.songs[a].title} -> {graph.songs[b].title} runs backwards in time")
    if re.search(r"\blyric|\bsings?\b|\bwords\b", cp.description, flags=re.I):
        errors.append("description must describe music only")
    return errors


def curated_candidate(graph: Graph, measured: dict[int, Measured], cp: CuratedPath, checker: Checker | None = None
                      ) -> Candidate:
    nodes = [graph.by_work[w] for w in cp.works]
    hops = [make_hop(graph, measured, graph.edge(a, b), checker) for a, b in zip(nodes, nodes[1:])]
    score, terms = score_path(graph, nodes, hops, fame(graph), measured)
    return Candidate(nodes, hops, score, terms)


def auto_title(graph: Graph, c: Candidate) -> tuple[str, str, str]:
    """(slug, title, description) for an uncurated path, from its identities only."""
    first, last = graph.songs[c.nodes[0]], graph.songs[c.nodes[-1]]
    main = c.main_identity
    slug = re.sub(r"[^a-z0-9]+", "-", f"{main} {last.title}".lower()).strip("-")[:48].strip("-")
    title = f"{first.title} to {last.title}"
    if all(h.edge.evidence == main for h in c.hops):
        desc = f"Every song here shares the {main}."
    else:
        desc = "Each song shares a musical identity with the next: " + "; ".join(
            dict.fromkeys(h.edge.evidence for h in c.hops)) + "."
    return slug, title, desc


def subtitle(graph: Graph, c: Candidate) -> str:
    years = [graph.songs[n].year for n in c.nodes]
    cats = list(dict.fromkeys(h.category for h in c.hops))
    if len(cats) > 2:
        cat = "mixed identities"
    else:
        cat = " + ".join(cats)
    return f"{years[0]} -> {years[-1]} · {len(c.nodes)} songs · {cat}"
