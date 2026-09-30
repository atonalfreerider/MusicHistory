"""Find ffmpeg: ``$FFMPEG``, then ``ffmpeg`` on PATH, then the known Windows install.

The render needs the ``rubberband`` filter (pitch/tempo glides); ``has_filter`` checks it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

ENV = "FFMPEG"
KNOWN = (
    r"C:\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe",
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
)


class FfmpegNotFound(RuntimeError):
    pass


def find_ffmpeg(env: dict[str, str] | None = None, which=shutil.which, known: tuple[str, ...] = KNOWN) -> str:
    """Path of the ffmpeg executable. ``$FFMPEG`` must point to an existing file when set (a
    wrong override is an error, not a silent fallback)."""
    env = os.environ if env is None else env
    override = (env.get(ENV) or "").strip().strip('"')
    if override:
        if Path(override).is_file():
            return str(Path(override))
        raise FfmpegNotFound(f"{ENV}={override!r} does not exist")
    found = which("ffmpeg")
    if found:
        return str(found)
    for candidate in known:
        if Path(candidate).is_file():
            return candidate
    raise FfmpegNotFound(f"ffmpeg not found: set {ENV}, put ffmpeg on PATH, or install it at {known[0]}")


@lru_cache(maxsize=None)
def filters(ffmpeg: str) -> frozenset[str]:
    out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, errors="replace").stdout
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) in (2, 3) and "->" in parts[2]:
            names.add(parts[1])
    return frozenset(names)


def has_filter(ffmpeg: str, name: str) -> bool:
    return name in filters(ffmpeg)
