# Narration scripts

One JSON file per featured path, named `<path id>.json` (DESIGN §15). The narration stage reads
these, voices each cue, and places it on the path's mashup (`data/audio/mashups/mashups.json`).
They are committed and hand-editable. After editing one, run
`.venv\Scripts\python.exe -m pytest tests/narration/test_scripts.py`.

## Shape

```json
{"id": "<path id>", "title": "<the mashup's title>",
 "cues": [{"id": "intro",
           "anchor": {"segment": 0, "offset": 0.3},
           "kind": "intro",
           "text": "The spoken line.",
           "image": "artist-Q2525354",
           "sources": [{"title": "No Woman, No Cry (Wikipedia)", "url": "https://en.wikipedia.org/wiki/No_Woman,_No_Cry"}]}]}
```

* `anchor.segment` indexes the path's `segments` in mashups.json. `anchor.offset` is seconds after
  that segment starts and must fall inside it. The cue starts at segment start plus offset, and
  cues are in time order.
* `kind` is `intro` (over the root song), `song` (about the song that is playing),
  `changeover` (anchored in a changeover segment, where song B's voice comes in over song A's
  band) or `outro` (the last cue, which ties the path together).
* `image` is `artist-<Wikidata QID>` of the performer (P175 of the song's item), which is the id
  the photos stage uses. Use null when no artist should be shown.
* `sources` has at least one `{title, url}` for every cue.

## Timing

* A cue gets at most 2.4 words per second of the time until the next cue, minus a 1 s gap.
  The last cue has the time until the mix ends, minus 0.5 s. Hyphenated words count once per
  part.
* The narration stays silent for the first 2 s of every changeover so the new voice can be
  heard. A cue that starts before a changeover must finish, at 2.4 words per second, by the
  time the changeover starts. Changeover cues therefore start about 2.1 s into their segment.
* The narration may overlap the music, because the viewer ducks the music under it.

## Delivery

The voice should fall at the end of each sentence.

* Write declarative sentences that end in periods. Do not use questions, exclamations,
  ellipses or quotation marks.
* Use short to medium sentences.
* Spell numbers out, such as "nineteen fifty-seven" and "sixty-three years".
* Speak chords as words, such as "one, five, six minor, four", "B flat" or "C sharp minor".
  Keep roman numerals and digits out of `text`. The test rejects them.
* Say what the listener hears at a changeover: the incoming song's voice over the previous
  song's band, matched in key and tempo.

## Accuracy rules

* Every factual claim comes from a cited source. That means a Wikipedia song or artist article
  (Background, Writing or Composition section), a reputable interview or book cited there, or
  a chord transcription for the harmony. Hooktheory TheoryTabs are used for chords, and the
  project's local Hooktheory dataset (`data/cache/hooktheory`) gives the same pages' URLs.
* Paraphrase the sources. Never quote anyone at more than a few words, and never invent a
  quote or an anecdote.
* Never include song lyrics or lyric fragments, and never paraphrase lyrics line by line.
* If a claim is disputed, say so (for example, "Though Marley is widely thought to have written
  it, the credit went to Vincent Ford") or leave it out. The same applies to an origin story
  with competing accounts.
* Do not present this project's own analysis as documented history. A link that only the graph
  finds, such as the shared bass line between Only the Lonely and Duke of Earl, is described as
  the graph's finding. Do not imply that one artist influenced another unless a source says so.
* Respect sources' terms. Do not get around paywalls or bot challenges. If a page will not load,
  find another source or drop the claim.

## Claims checked and left out

* Duane Allman adapting the Layla riff from Albert King's "As the Years Go Passing By". This is
  widely repeated, but neither the Layla article nor the article on that song supports it.
* Johnny Cash's account of the "blue suede shoes" phrase. It competes with Perkins's own story
  of the dance, so only Perkins's account is used and it is attributed to him.
* The chords of Lonestar's "Amazed", Paul Anka's "Diana" beyond the '50s-progression listing,
  "Theme from A Summer Place", "Duke of Earl" and "Only the Lonely". No usable transcription was
  available without bot-blocked chord sites, so those cues rely on the path's identity and the
  measured chord match.
* Sting's royalty arrangement for "I'll Be Missing You" and the band's later credit disputes.
  The reports conflict or are still developing, so the script says only that Sting is credited
  as a writer.
* The identity of the girl behind "Diana" (an Ottawa Citizen piece questions the usual story)
  and the crash that inspired "Last Kiss" (the dates don't line up).
