# MusicHistory

A temporal influence graph of the top ~1000 songs of all time, built from MIDI
transcriptions and walked in Unity with the music playing.

1. **canon** fuses all-time song lists (Billboard year-end 1946–2025 via Wikipedia, tsort.info,
   Rolling Stone 500, Grammy Hall of Fame, Spotify most-streamed) into a ranked list of
   compositions with their **original** release year.
2. **fetch** collects candidate MIDI files for every song from several sources (Lakh MIDI
   Dataset, midicollection.com, freemidi.org, midiworld.com), validates them and strips
   lyrics and other text.
3. **select** compares the candidates (static quality, agreement between transcriptions,
   agreement with Hooktheory annotations) and picks the best source per song.
4. **analyze** runs the [Resonance-2](../Resonance-2) pattern analysis on each song and
   derives chord-progression and melody identities with every song mercilessly transposed
   to one key (C major / A minor) on a tempo-free beat grid.
5. **influence** (C#) scores rare shared material between earlier and later songs against a
   null model and roots each song under its most-referenced influencer.
6. **layout** (C#, a new version of [GPU-FDG](../GPU-FDG)) lays the forest out on the GPU
   with time pinned to the vertical axis.
7. **unity/** (a new version of [Unity-FDG](../Unity-FDG)) renders the graph and plays a
   directed walkthrough: each song starts in the key and BPM of the song before it and
   morphs into its own.

See [docs/DESIGN.md](docs/DESIGN.md) for the contracts between stages.

## Setup

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m musichistory status
```

Requires the .NET 10 SDK, Unity 6000.6.3f1 and a checkout of Resonance-2 next to this
folder (override with `RESONANCE_ROOT`). All downloads and outputs go to `data/`
(gitignored; override with `MUSICHISTORY_DATA`).

## Data sources and credits

* Song lists: Wikipedia (CC BY-SA), [tsort.info](https://tsort.info) top 5000 songs v2.9.0001,
  Rolling Stone 500 mirrors on GitHub, Wikidata (CC0), MusicBrainz (CC0), weekly Hot 100 data
  from utdata/rwd-billboard-data.
* MIDI: [Lakh MIDI Dataset](https://colinraffel.com/projects/lmd/) (CC BY 4.0, Colin Raffel),
  midicollection.com, freemidi.org, midiworld.com. MIDI transcriptions of copyrighted songs
  are fan-made; they are used here for personal analysis only and are never committed.
* Annotations: Hooktheory data via [Sheet Sage](https://github.com/chrisdonahue/sheetsage)
  (CC BY-NC-SA 3.0).
* Synth: [MeltySynth](https://github.com/sinshu/meltysynth) (MIT); MuseScore's MS Basic
  SoundFont (MIT).
