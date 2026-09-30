"""Lyric themes (DESIGN.md §12): where each song sits among ten lyrical themes.

Modules:

* ``lyrics``     transient lyric reader: lyric/KAR events of a song's own MIDI candidates,
                 decoded into lines **in memory only** (Lakh files re-read from the cached
                 tarballs, web files re-downloaded into memory);
* ``classify``   the ten themes and the classifiers (local zero-shot NLI on the GPU by
                 default; Claude via the Anthropic API with ``--backend claude``);
* ``validation`` hand-labelled validation set (titles/artists/theme ids only) and accuracy;
* ``export``     pipeline tables ``song_text`` / ``song_theme`` and ``data/graph/themes_graph.db``;
* ``stage``      ``python -m musichistory themes``;
* ``singer``     singer gender from Wikidata (separate owner).

The lyrics rule: lyric text exists only in process memory while it is being classified. It
is never written to disk, a database, a log, stdout/stderr, an exception message, a cache or
a report. Only its SHA-256, its number of lines and the source candidate are recorded.
"""
