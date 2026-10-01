"""The ``mashup`` stage is registered with the command line and exposes the stage interface."""

import argparse
import importlib

from musichistory import cli


def test_mashup_stage_registered():
    assert "mashup" in cli.STAGES
    module_name, help_text = cli.STAGES["mashup"]
    assert module_name == "musichistory.mashup.stage"
    assert help_text


def test_mashup_stage_interface_and_defaults():
    mod = importlib.import_module(cli.STAGES["mashup"][0])
    assert callable(mod.run)
    p = argparse.ArgumentParser()
    mod.add_arguments(p)
    args = p.parse_args([])
    assert args.changeover == 20.0
    assert args.morph_bars == 2
    assert args.path is None
