"""MusicHistory: a temporal influence graph of the top songs of all time.

Stages (run with ``python -m musichistory <stage>``):

    canon      fuse all-time song lists into a ranked pool of works with original years
    fetch      collect candidate MIDI files for every pooled work from several sources
    select     score candidates, pick the best source per song, fix the final 1000
    analyze    run the Resonance-2 pattern analysis and derive normalized identities
    influence  score pairwise influence and build the tree (C#, influence/)
    layout     lay the graph out on the GPU with time pinned to an axis (C#, layout/)

See docs/DESIGN.md for the contracts between stages.
"""

__version__ = "0.1.0"
