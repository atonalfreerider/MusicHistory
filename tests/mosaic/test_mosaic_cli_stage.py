"""The ``mosaic`` stage is registered with the command line and exposes the stage interface."""

import argparse
import importlib

from mosaic_helpers import ROOT  # noqa: F401
from musichistory import cli


def test_mosaic_stage_registered_with_defaults():
    module_name, help_text = cli.STAGES["mosaic"]
    assert module_name == "musichistory.mosaic.stage" and help_text
    mod = importlib.import_module(module_name)
    assert callable(mod.run)
    p = argparse.ArgumentParser()
    mod.add_arguments(p)
    args = p.parse_args([])
    assert args.examples == 8 and args.max_seconds == 90.0 and args.target is None
    assert args.min_heard == mod.MIN_HEARD and not args.search_only
    assert mod.params_hash() == mod.params_hash()
