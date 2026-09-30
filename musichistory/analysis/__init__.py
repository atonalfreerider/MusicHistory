"""Resonance-2 PatternPrep analysis for MusicHistory (DESIGN.md §7).

``patternprep`` builds the stock PatternPrep tool from the Resonance-2 checkout, runs it on
one sanitized MIDI at a time as a subprocess (a crash or runaway file only costs that song),
and reduces the multi-megabyte ``.patterns.json`` bundle to the small *slim* analysis every
later stage reads. The bundle is deleted as soon as the slim JSON is written.
"""

from .patternprep import AnalysisError

__all__ = ["AnalysisError"]
