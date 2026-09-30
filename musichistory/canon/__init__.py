"""canon: a reproducible, defensible ranking of works (compositions) with original years.

``python -m musichistory canon`` (see ``stage.py``). Modules:

    billboard, tsort, rollingstone, grammy, spotify, acclaimed   one module per list source
    hot100        weekly Hot 100 first-chart weeks (dates only, never ranking)
    wikipedia     page wikitext pinned by revision;  wikitext  the table parser
    wikidata      pageprops / search / claims / labels;  resolve  rows -> works
    musicbrainz   release dates for works with a missing or suspect Wikidata date
    years         the year rules (pure);  fusion  weighted RRF and the pool (pure)
    known_influence + controls.json   ground truth for validating the influence stage
    net           polite, cached network access (data/cache/canon)
"""
