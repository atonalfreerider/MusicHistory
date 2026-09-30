"""Run Resonance-2 PatternPrep headless and slim its output (DESIGN.md §7).

Why a subprocess and not an in-process port: the stock exe gives exact parity with
Resonance, and one process per song means a crash, a hang or a memory blow-up on a hostile
file only costs that song (the exe is killed on timeout).

Pitfalls handled here (measured in the research phase, see resonance-analysis.md):

* Resonance pitch classes are **A = 0**; everything we write is **C = 0**
  (``pc_C = (pc_A + 9) % 12``).
* The top-level ``Key`` is -1 unless song.json sets one: the detected key lives in
  ``Frames[].Key/Minor`` (seconds-timed), which we turn into beat-timed ``key_runs``.
* ``Tempos[0]`` is always a synthetic 120 BPM entry; it is dropped when the file has its
  own tempo at beat 0.
* Markers and track names flow into section names, lane names and the form grammar. The
  slim JSON never copies a name: section and part roles pass an allowlist, grammars pass a
  character/word check, and ``Lyrics``, ``TrackNames``, ``Title``, ``FormName``,
  ``Summary`` and ``Provenance`` are never read.
* PatternPrep writes lyric text only from a ``lyrics.txt``/``library.json``/song.json
  ``Lyrics`` beside the MIDI; none of those is ever written, and stale ones are removed.
* stdout is drained (a 600-bar file prints ~5 KB and deadlocks an undrained pipe).
* A failed run leaves an old bundle in place, so bundles are deleted before and after.
"""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .. import config

SLIM_VERSION = 1  # bump when the slim conversion changes (stage reuse checks it)

SECTION_ROLES = {"Intro", "Verse", "Pre-Chorus", "Chorus", "Post-Chorus", "Bridge", "Interlude",
                 "Outro", "Refrain", "Coda", "Theme"}
PART_ROLES = {"vocal", "bass", "keys", "guitar", "strings", "pad", "lead", "chords", "line"}
QUALITIES = {"", "m", "7", "maj7", "m7", "dim"}
# Words PatternPrep's form grammar is built from (FormAnalysis.Short and classical letters).
GRAMMAR_WORDS = {"In", "V", "PC", "C", "PoC", "Br", "It", "Out", "R", "Co", "Theme", "Part", "Intro",
                 "Coda", "Outro"}
# Words of PatternPrep's generated variation notes (FormPatterns.Describe), e.g. "transposed +2 -
# 1-bar lead-in" or "repeats Chorus 1"; any other word (a marker label) empties the note.
VARIATION_WORDS = {"transposed", "modulates", "in", "pass", "passes", "bar", "bars", "lead", "of", "usually", "tag",
                   "extended", "fundamental", "new", "ending", "varied", "repeats", "Pre", "Post", *SECTION_ROLES}
_ASCII = str.maketrans({"×": "x", "′": "'", "″": "''", "‴": "'''", "·": "-", "–": "-", "—": "-", "−": "-",
                        "→": ">"})
_GRAMMAR_CHARS = re.compile(r"^[A-Za-z0-9 ()'+\-x]*$")
_VARIATION = re.compile(r"^[A-Za-z0-9 ,'+\-()]{0,80}$")

_WINDOWS_NO_CONSOLE = 0x08000000 if os.name == "nt" else 0
_build_lock = threading.Lock()


class AnalysisError(RuntimeError):
    """PatternPrep failed, timed out, or produced an empty analysis."""


# --------------------------------------------------------------------------- build
def tool_dir() -> Path:
    return config.TOOLS / "PatternPrep"


def _project_dir() -> Path:
    return config.RESONANCE_ROOT / "Tools" / "PatternPrep"


def _exe_command(out: Path) -> list[str]:
    exe = out / ("PatternPrep.exe" if os.name == "nt" else "PatternPrep")
    if exe.exists():
        return [str(exe)]
    return ["dotnet", str(out / "PatternPrep.dll")]


def _source_files() -> list[Path]:
    """Everything the build compiles: the project's own .cs files plus the linked Assets
    sources and referenced DLLs named in the csproj (Resonance-2 is edited live)."""
    proj = _project_dir()
    csproj = proj / "PatternPrep.csproj"
    if not csproj.exists():
        raise AnalysisError(f"PatternPrep project not found at {csproj} (set RESONANCE_ROOT)")
    files = set(proj.glob("*.cs")) | {csproj}
    text = csproj.read_text(encoding="utf-8", errors="replace")
    for m in re.finditer(r'Include\s*=\s*"([^"]+)"|<HintPath>([^<]+)</HintPath>', text):
        for rel in (m.group(1) or m.group(2)).split(";"):
            p = (proj / rel.strip().replace("\\", "/")).resolve()
            if p.is_file():
                files.add(p)
    return sorted(files)


def sources_sha256() -> str:
    h = hashlib.sha256()
    root = config.RESONANCE_ROOT
    for p in _source_files():
        try:
            rel = p.resolve().relative_to(root).as_posix()
        except ValueError:
            rel = p.as_posix()
        h.update(rel.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


_commit_cache: dict[str, tuple[str, float]] = {}
COMMIT_TTL_S = 60.0  # re-ask git at most once a minute per process (called once per song)


def resonance_commit() -> str:
    """``git rev-parse HEAD`` of Resonance-2, suffixed ``+dirty.<hash>`` when any file the
    PatternPrep build compiles differs from HEAD (so results stay traceable)."""
    src = sources_sha256()
    hit = _commit_cache.get(src)
    if hit is not None and time.monotonic() - hit[1] < COMMIT_TTL_S:
        return hit[0]
    root = config.RESONANCE_ROOT
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=30, creationflags=_WINDOWS_NO_CONSOLE).stdout.strip()
        rels = []
        for p in _source_files():
            with contextlib.suppress(ValueError):
                rels.append(p.resolve().relative_to(root).as_posix())
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", *rels], capture_output=True,
                               text=True, timeout=30, creationflags=_WINDOWS_NO_CONSOLE).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head, dirty = "", ""
    if not head:
        commit = f"nogit.{src[:12]}"
    else:
        commit = f"{head}+dirty.{src[:10]}" if dirty else head
    _commit_cache[src] = (commit, time.monotonic())
    return commit


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = 900.0) -> Iterator[None]:
    """Cross-process lock so parallel workers never run two builds into one folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > timeout:  # stale lock from a killed build
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() - t0 > timeout:
                raise AnalysisError(f"timed out waiting for {path}") from None
            time.sleep(0.5)
    try:
        yield
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def _read_stamp(out: Path) -> dict:
    try:
        return json.loads((out / "musichistory-build.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _up_to_date(out: Path, commit: str, src: str) -> bool:
    stamp = _read_stamp(out)
    return (Path(_exe_command(out)[-1]).exists() and stamp.get("sources_sha256") == src
            and stamp.get("commit") == commit)


def ensure_built(force: bool = False) -> Path:
    """Build PatternPrep into ``data/tools/PatternPrep`` once; rebuild when the Resonance-2
    HEAD or the compiled sources change. Returns the executable (or the DLL when no apphost
    was produced)."""
    out = tool_dir()
    with _build_lock:
        src = sources_sha256()
        commit = resonance_commit()
        if not force and _up_to_date(out, commit, src):
            return Path(_exe_command(out)[-1])
        with _file_lock(out.parent / "PatternPrep.build.lock"):
            if not force and _up_to_date(out, commit, src):
                return Path(_exe_command(out)[-1])
            csproj = _project_dir() / "PatternPrep.csproj"
            out.mkdir(parents=True, exist_ok=True)
            t0 = time.monotonic()
            proc = subprocess.run(
                ["dotnet", "build", "-c", "Release", str(csproj), "-o", str(out), "-nologo", "-v", "q"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=_WINDOWS_NO_CONSOLE)
            if proc.returncode != 0:
                tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
                raise AnalysisError(f"PatternPrep build failed (exit {proc.returncode}):\n{tail}")
            stamp = {"commit": commit, "sources_sha256": src,
                     "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "build_seconds": round(time.monotonic() - t0, 1),
                     "project": csproj.as_posix()}
            (out / "musichistory-build.json").write_text(json.dumps(stamp, indent=1), encoding="utf-8")
        exe = Path(_exe_command(out)[-1])
        if not exe.exists():
            raise AnalysisError(f"PatternPrep build produced no executable in {out}")
        return exe


# --------------------------------------------------------------------------- run
def analyze(midi_path: str | os.PathLike, workdir: str | os.PathLike, *, lead_track: int | None = None,
            timeout: float = 180.0) -> dict:
    """Analyze one sanitized MIDI: returns the slim dict (also written to
    ``<workdir>/analysis.json``). Raises AnalysisError on a non-zero exit, a timeout, or an
    empty result (no sections, no pitched notes)."""
    ensure_built()
    cmd = _exe_command(tool_dir())
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    score = work / "score.mid"
    src = Path(midi_path)
    if not src.exists():
        raise AnalysisError(f"MIDI not found: {src}")
    _check_header(src)
    if src.resolve() != score.resolve():
        shutil.copyfile(src, score)
    bundle_path = work / "score.mid.patterns.json"
    # Files PatternPrep would pick up as lyric sources, stale outputs of an earlier run.
    for name in ("score.mid.patterns.json", "score.mid.patterns.json.tmp", "lyrics.txt", "lyrics.timing.json",
                 "library.json", "analysis.json"):
        (work / name).unlink(missing_ok=True)
    settings: dict = {"Style": "pop"}
    if lead_track is not None and int(lead_track) >= 0:
        settings["LeadVocalTrack"] = int(lead_track)
    (work / "song.json").write_text(json.dumps(settings), encoding="utf-8")

    proc = subprocess.Popen([*cmd, str(score)], cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=_WINDOWS_NO_CONSOLE)
    try:
        _, err = proc.communicate(timeout=timeout)  # drains stdout and stderr
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        bundle_path.unlink(missing_ok=True)
        (work / "score.mid.patterns.json.tmp").unlink(missing_ok=True)
        raise AnalysisError(f"PatternPrep timed out after {timeout:.0f} s") from None
    if proc.returncode != 0:
        bundle_path.unlink(missing_ok=True)
        raise AnalysisError(f"PatternPrep exit {proc.returncode}: {_first_error(err)}")
    if not bundle_path.exists():
        raise AnalysisError("PatternPrep wrote no .patterns.json")
    try:
        bundle = json.loads(bundle_path.read_bytes())
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"unreadable .patterns.json: {exc}") from None
    finally:
        bundle_path.unlink(missing_ok=True)  # the full bundle is 1-45 MB and may carry names
    slim = slim_from_bundle(bundle, commit=resonance_commit(), midi_sha256=_sha256(score))
    del bundle
    check_slim(slim)
    tmp = work / "analysis.json.tmp"
    tmp.write_text(json.dumps(slim, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    tmp.replace(work / "analysis.json")
    return slim


def _check_header(path: Path) -> None:
    """Reject what PatternPrep would silently turn into garbage (SMPTE timing) or crash on,
    before paying for a process (sanitized files always pass)."""
    with open(path, "rb") as fh:
        head = fh.read(14)
    if len(head) < 14 or head[:4] != b"MThd":
        raise AnalysisError("not a standard MIDI file (no MThd header)")
    fmt, division = int.from_bytes(head[8:10], "big"), int.from_bytes(head[12:14], "big")
    if fmt not in (0, 1):
        raise AnalysisError(f"unsupported MIDI format {fmt}")
    if division & 0x8000:
        raise AnalysisError("SMPTE time division is not supported")


def check_slim(slim: dict) -> None:
    if not slim.get("sections"):
        raise AnalysisError("empty analysis: 0 sections")
    if not any(n[4] != 10 for n in slim.get("notes", ())):
        raise AnalysisError("empty analysis: no pitched notes")


def _first_error(err: bytes) -> str:
    text = err.decode("utf-8", errors="replace").strip()
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:300]
    return "no error output"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- slim
def pc_c(pc_a: int) -> int:
    """Resonance's A = 0 pitch class to C = 0 (rests stay -1)."""
    return -1 if pc_a is None or pc_a < 0 else (int(pc_a) + 9) % 12


def _r(x: float, nd: int = 4) -> float:
    v = round(float(x), nd)
    return 0.0 if v == 0 else v


def _grammar(text: str, words: set[str] | None) -> str:
    """ASCII form of a Resonance grammar string, or '' when it holds anything but the
    generated vocabulary (a marker or track name must never reach our data)."""
    s = (text or "").translate(_ASCII).strip()
    if not _GRAMMAR_CHARS.match(s):
        return ""
    for w in re.findall(r"[A-Za-z]+", re.sub(r"x\d+", " ", s)):  # "Ax8" / "(V PC C)x2" repeat counts
        if (len(w) == 1 and w.isupper()) or (words is not None and w in words):
            continue
        return ""
    return s


def _letter(text: str) -> str:
    s = (text or "").translate(_ASCII).strip()
    return s if re.fullmatch(r"[A-Z]{1,2}'{0,3}", s) else ""


def _variation(text: str) -> str:
    s = (text or "").translate(_ASCII).strip()
    if not _VARIATION.match(s):
        return ""
    for w in re.findall(r"[A-Za-z]+", s):
        if not ((len(w) == 1 and w.isupper()) or w in VARIATION_WORDS):
            return ""
    return s


@dataclass
class _TempoMap:
    beats: list[float]
    seconds: list[float]
    us: list[float]

    def beat_at(self, t: float) -> float:
        i = max(0, bisect.bisect_right(self.seconds, t + 1e-9) - 1)
        return self.beats[i] + (t - self.seconds[i]) * 1e6 / self.us[i]


def _key_runs(bundle: dict, tempo: _TempoMap, end_beat: float) -> list[list]:
    """Beat-timed runs of the per-frame key (Resonance region keys), boundaries snapped to
    the exact KeyChanges beats when the two agree."""
    runs: list[list] = []
    for f in bundle.get("Frames") or ():
        k = f.get("Key", -1)
        if k is None or k < 0:
            continue
        key = (pc_c(k), bool(f.get("Minor")))
        if runs and (runs[-1][2], runs[-1][3]) == key:
            continue
        runs.append([_r(tempo.beat_at(float(f.get("Time", 0.0)))), None, key[0], key[1]])
    if not runs:
        return []
    runs[0][0] = 0.0
    changes = bundle.get("KeyChanges") or []
    if len(changes) == len(runs) - 1 and all(
            (pc_c(c["Key"]), bool(c["Minor"])) == (r[2], r[3]) for c, r in zip(changes, runs[1:])):
        for c, r in zip(changes, runs[1:]):
            r[0] = _r(c["Beat"])
    for a, b in zip(runs, runs[1:]):
        a[1] = b[0]
    runs[-1][1] = _r(max(end_beat, runs[-1][0]))
    return runs


def _tempos(bundle: dict) -> tuple[list[list], _TempoMap]:
    raw = bundle.get("Tempos") or [{"Beat": 0, "Seconds": 0, "Microseconds": 500000}]
    tmap = _TempoMap([float(t["Beat"]) for t in raw], [float(t["Seconds"]) for t in raw],
                     [float(t["Microseconds"]) for t in raw])
    rows: list[list] = []
    for t in raw:  # the last entry at a beat wins (drops the synthetic 120 BPM header)
        beat, us = _r(t["Beat"]), int(round(float(t["Microseconds"])))
        if rows and rows[-1][0] == beat:
            rows[-1][1] = us
        elif not rows or rows[-1][1] != us:
            rows.append([beat, us])
    merged: list[list] = []
    for row in rows:  # identical consecutive tempos are one segment
        if not merged or merged[-1][1] != row[1]:
            merged.append(row)
    return merged, tmap


def _measures(bundle: dict) -> list[list]:
    runs: list[list] = []
    for m in bundle.get("Measures") or ():
        num, den = int(m.get("Numerator", 4)), int(m.get("Denominator", 4))
        if runs and runs[-1][1] == num and runs[-1][2] == den:
            runs[-1][3] += 1
        else:
            runs.append([_r(m["Start"]), num, den, 1])
    return runs


def _chord_row(c: dict) -> list:
    root = int(c.get("Root", -1))
    q = (c.get("Quality") or "").strip()
    if root < 0:
        return [_r(c["Start"]), _r(c["End"]), -1, ""]
    return [_r(c["Start"]), _r(c["End"]), pc_c(root), q if q in QUALITIES else ""]


def slim_from_bundle(bundle: dict, *, commit: str, midi_sha256: str | None = None) -> dict:
    """Reduce a Version-5 ``.patterns.json`` to the slim schema of DESIGN.md §7 (C = 0,
    beats, no names, no lyric fields)."""
    tempos, tmap = _tempos(bundle)
    end_beat = float(bundle.get("EndBeat") or 0.0)
    sections = []
    for s in bundle.get("Sections") or ():
        role = s.get("Role") or ""
        sections.append({
            "first_bar": int(s.get("FirstBar", 0)), "bar_count": int(s.get("BarCount", 0)),
            "family": int(s.get("Family", -1)), "role": role if role in SECTION_ROLES else "Section",
            "letter": _letter(s.get("Letter", "")),
            "start": _r(s.get("Start", 0.0)), "end": _r(s.get("End", 0.0)),
            "loops": int(s.get("Loops", 0)), "cycle_beats": _r(s.get("CycleBeats", 0.0)),
            "transpose": int(s.get("Transpose", 0)), "variation": _variation(s.get("Variation", "")),
            "group": int(s.get("Group", -1)),
        })
    patterns = []
    for p in bundle.get("Patterns") or ():
        role = p.get("Role") or ""
        patterns.append({
            "family": int(p.get("Family", -1)), "reference": int(p.get("Reference", -1)),
            "loop_bars": int(p.get("LoopBars", 0)), "loop_beats": _r(p.get("LoopBeats", 0.0)),
            "visits": int(p.get("Visits", 0)), "passes": int(p.get("Passes", 0)),
            "role": role if role in SECTION_ROLES else "Section",
            "loop": [_chord_row(c) for c in p.get("Loop") or ()],
        })
    parts = []
    for p in bundle.get("Parts") or ():
        role = p.get("Role") or ""
        parts.append({
            "track": int(p.get("Track", -1)), "channel": int(p.get("Channel", 0)),
            "role": role if role in PART_ROLES else "other", "vocal": bool(p.get("Vocal")),
            "bars": int(p.get("Bars", 0)), "fundamental_bars": int(p.get("FundamentalBars", 0)),
            "grammar": _grammar(p.get("Grammar", ""), None),
        })
    notes = sorted(
        ([_r(n["Beat"]), _r(n["Length"]), int(n["Pitch"]), int(n["Track"]), int(n["Channel"]),
          _r(n.get("Velocity", 0.0), 2)] for n in bundle.get("Notes") or ()),
        key=lambda n: (n[0], n[3], n[4], n[2]))
    slim = {
        "version": int(bundle.get("Version", 0)),
        "slim_version": SLIM_VERSION,
        "resonance_commit": commit,
        "midi_sha256": midi_sha256,
        "style": bundle.get("Style") if bundle.get("Style") in ("pop", "classical") else "",
        "song_bars": int(bundle.get("SongBars", 0)),
        "end_beat": _r(end_beat),
        "duration_s": _r(bundle.get("Duration", 0.0), 3),
        "tempos": tempos,
        "measures": _measures(bundle),
        "chords": [_chord_row(c) for c in bundle.get("Chords") or ()],
        "sections": sections,
        "patterns": patterns,
        "groups": [{"id": int(g.get("Id", -1)), "visits": int(g.get("Visits", 0)),
                    "families": [int(f) for f in g.get("Families") or ()]} for g in bundle.get("Groups") or ()],
        "form_grammar": _grammar(bundle.get("FormGrammar", ""), GRAMMAR_WORDS),
        "key_changes": [{"beat": _r(c["Beat"]), "tonic": pc_c(c["Key"]), "minor": bool(c["Minor"]),
                         "from_tonic": pc_c(c["From"]), "from_minor": bool(c["FromMinor"])}
                        for c in bundle.get("KeyChanges") or ()],
        "key_runs": _key_runs(bundle, tmap, end_beat),
        "notes": notes,
        "parts": parts,
    }
    return slim
