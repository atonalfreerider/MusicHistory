"""Build and run the C# stages (influence/, layout/) from the Python CLI.

``python -m musichistory influence [-- extra args]`` builds
``influence/MusicHistory.Influence`` into ``data/tools/influence`` and runs
``MusicHistory.Influence.exe run --db <pipeline db> --graph <graph db> <extra args>``.

``python -m musichistory layout [-- extra args]`` builds ``layout/MusicHistory.Layout``
into ``data/tools/layout`` and runs ``MusicHistory.Layout.exe <graph db> <extra args>``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from . import config

PROJECTS = {
    "influence": ("influence/MusicHistory.Influence/MusicHistory.Influence.csproj", "MusicHistory.Influence.exe"),
    "layout": ("layout/MusicHistory.Layout/MusicHistory.Layout.csproj", "MusicHistory.Layout.exe"),
}


def add_arguments(p: argparse.ArgumentParser, name: str) -> None:
    p.set_defaults(_dotnet=name)
    p.add_argument("--no-build", action="store_true", help="run the last build without rebuilding")
    p.add_argument("extra", nargs=argparse.REMAINDER, help="arguments passed to the executable (after --)")


def build(name: str) -> str:
    csproj, exe = PROJECTS[name]
    out = config.TOOLS / name
    cmd = ["dotnet", "build", "-c", "Release", str(config.ROOT / csproj), "-o", str(out), "-nologo", "-v", "q"]
    subprocess.run(cmd, check=True)
    return str(out / exe)


def run(args: argparse.Namespace) -> int:
    name = args._dotnet
    exe = str(config.TOOLS / name / PROJECTS[name][1]) if args.no_build else build(name)
    extra = [a for a in (args.extra or []) if a != "--"]
    if name == "influence":
        cmd = [exe, "run", "--db", str(config.PIPELINE_DB), "--graph", str(config.GRAPH_DB), *extra]
    elif extra[:1] == ["themes"]:
        # python -m musichistory layout -- themes [options]: the lyric-themes layout (DESIGN §12)
        cmd = [exe, "themes", str(config.GRAPH / "themes_graph.db"), *extra[1:]]
    else:
        cmd = [exe, str(config.GRAPH_DB), *extra]
    print(" ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(0)
