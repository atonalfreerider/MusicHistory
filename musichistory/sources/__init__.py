"""MIDI sources for the fetch stage (DESIGN.md §5).

``lakh`` (Lakh MIDI Dataset, offline), ``midicollection`` (offline sitemap index + direct
downloads), ``freemidi`` and ``midiworld`` (search-based gap fillers), and ``hooktheory``
(reference annotations used by select, not MIDI). ``stage`` runs them in that order;
``base`` holds the shared matching rules and candidate ingestion.
"""
