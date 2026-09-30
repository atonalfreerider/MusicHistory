"""Paths and settings shared by every stage.

Everything the pipeline writes lives under ``DATA`` (gitignored). Override it with the
``MUSICHISTORY_DATA`` environment variable; override the Resonance-2 checkout with
``RESONANCE_ROOT``.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MUSICHISTORY_DATA", ROOT / "data")).resolve()
RESONANCE_ROOT = Path(os.environ.get("RESONANCE_ROOT", ROOT.parent / "Resonance-2")).resolve()

# Raw downloads (list sources, Lakh archives, sitemaps, Hooktheory). Separately overridable
# so a scratch DATA folder for tests can share the multi-gigabyte cache.
CACHE = Path(os.environ.get("MUSICHISTORY_CACHE", DATA / "cache")).resolve()
CANDIDATES = DATA / "candidates"  # sanitized candidate MIDIs: candidates/<work_id>/<source>__<md5>.mid
SONGS = DATA / "songs"            # chosen song per work: songs/<work_id>/score.mid (+ analysis files)
NORMALIZED = DATA / "normalized"  # every song transposed to C major / A minor at TARGET_BPM
ANALYSIS_WORK = DATA / "analysis-work"  # scratch folders for candidate analysis runs
GRAPH = DATA / "graph"            # graph database handed to layout and Unity
TOOLS = DATA / "tools"            # built executables (PatternPrep)
SOUNDFONTS = DATA / "soundfonts"  # converted SF2 for the Unity player
REPORTS = DATA / "reports"        # source comparison and other human-readable reports
PIPELINE_DB = DATA / "musichistory.sqlite"
GRAPH_DB = GRAPH / "music_graph.db"

# Size of the final graph and of the candidate pool the song list hands to acquisition.
TARGET_SONGS = int(os.environ.get("MUSICHISTORY_TARGET_SONGS", "1000"))
POOL_SIZE = int(os.environ.get("MUSICHISTORY_POOL_SIZE", "1500"))

# "Mercilessly transpose to the same key": relative normalization puts major songs in
# C major and minor songs in A minor, so every song shares one pitch collection and a
# relative major/minor mistake in key detection cannot break a match. "parallel" puts
# every tonic on C instead (minor songs in C minor).
NORMALIZATION = os.environ.get("MUSICHISTORY_NORMALIZATION", "relative")
TARGET_BPM = float(os.environ.get("MUSICHISTORY_TARGET_BPM", "120"))

# HTTP identity. No personal data by default; set MUSICHISTORY_CONTACT to add a contact
# (e.g. a URL) where a service's etiquette asks for one (Wikimedia, MusicBrainz).
USER_AGENT = "MusicHistory/0.1 (personal music research tool)"
CONTACT = os.environ.get("MUSICHISTORY_CONTACT", "").strip()


def user_agent() -> str:
    return f"{USER_AGENT[:-1]}; {CONTACT})" if CONTACT else USER_AGENT


def ensure_dirs() -> None:
    for p in (DATA, CACHE, CANDIDATES, SONGS, NORMALIZED, ANALYSIS_WORK, GRAPH, TOOLS, SOUNDFONTS, REPORTS):
        p.mkdir(parents=True, exist_ok=True)
