"""Command line: ``python -m musichistory <stage> [options]``.

Each Python stage lives in a module exposing ``add_arguments(parser)`` and
``run(args) -> int``. The C# stages are built and invoked through ``musichistory.dotnet``.
"""

from __future__ import annotations

import argparse
import importlib
import sys

from . import config

STAGES: dict[str, tuple[str, str]] = {
    "canon": ("musichistory.canon.stage", "Fuse all-time song lists into a ranked pool of works"),
    "fetch": ("musichistory.sources.stage", "Collect candidate MIDI files from every source"),
    "select": ("musichistory.select", "Score candidates and choose the best MIDI per song"),
    "analyze": ("musichistory.identity.stage", "Resonance analysis + normalized chord/melody identities"),
    "influence": ("musichistory.dotnet", "Score influence and build the tree (C#)"),
    "layout": ("musichistory.dotnet", "GPU force-directed layout with time pinned (C#)"),
    "themes": ("musichistory.themes.stage", "Classify lyric themes, resolve singers, export the themes graph"),
    "paths": ("musichistory.paths.stage", "Curate featured paths and prerender their recording previews"),
    "mashup": ("musichistory.mashup.stage", "Separate stems and render each featured path as one key/tempo/chord-matched mashup"),
    "narration": ("musichistory.narration.stage", "Speak each featured path's narration script (ElevenLabs) with inflection, loudness and duck checks"),
    "photos": ("musichistory.photos.stage", "Find freely licensed photos of the featured songs' artists on Wikimedia Commons"),
    "status": ("musichistory.status", "Show progress of every stage"),
}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="musichistory", description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)
    modules = {}
    for name, (module_name, help_text) in STAGES.items():
        p = sub.add_parser(name, help=help_text)
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:  # stage not built yet
            p.set_defaults(_missing=str(exc))
            continue
        modules[name] = module
        if hasattr(module, "add_arguments"):
            module.add_arguments(p, name) if module_name.endswith("dotnet") else module.add_arguments(p)
    args = parser.parse_args(argv)
    if getattr(args, "_missing", None):
        print(f"stage '{args.stage}' is not available: {args._missing}", file=sys.stderr)
        return 2
    config.ensure_dirs()
    module = modules[args.stage]
    return int(module.run(args) or 0)
