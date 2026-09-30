"""Normalized identities of a song (DESIGN.md §7): key and key regions, chord sequences,
rotation-invariant loops, and the lead and bass lines, all in one key frame (C major /
A minor by default) on the quarter-note beat grid.

Modules:

    meter           bar lines, metric classes and the dominant meter from slim ``measures``
    key             ensemble key vote, key regions, per-region normalization shifts
    chords          chord token encodings (db.py) and the chg/cd/beat/keyfree sequences
    loops           Booth least rotation, primitive period, loop identities, roman numerals
    melody          lead-line selection, skyline, clean-up, bass line, interval entropy
    extract         ``extract(slim, features) -> SongIdentity`` and ``SongIdentity.to_db``
    compare         chord / melody / Hooktheory agreement scores (0..1)
    normalize_midi  write the song transposed per region to the target key at TARGET_BPM
    stage           ``python -m musichistory analyze``
"""
