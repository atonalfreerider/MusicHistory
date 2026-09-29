# MusicHistory design

MusicHistory builds a **temporal influence graph of the top ~1000 songs of all time** from
MIDI transcriptions, and lets you walk it in Unity with the music playing.

```
 canon ──► fetch ──► select ──► analyze ──► influence ──► layout ──► Unity
 (lists)   (MIDI)    (best)     (Resonance   (C#: pairs,   (C#: GPU    (walkthrough,
                                 + identity)  tree, graph)  FDG, time)  key/BPM morph)
```

Run a stage with `python -m musichistory <stage>` (Python 3.13 venv in `.venv`).
`python -m musichistory status` shows progress. Everything a stage writes lives under
`data/` (gitignored; override with `MUSICHISTORY_DATA`).

## 1. Principles

1. **Causality is temporal.** An influence edge always points from an earlier song to a
   later one. Songs whose order cannot be established (same year, no finer date) get no edge.
2. **Everything is compared in one key and one tempo.** Every song is transposed so that
   its home key becomes the target key, region by region, so modulations disappear too.
   Default *relative* normalization: major songs to **C major**, minor songs to **A minor**
   (one pitch collection, no sharps or flats, robust to relative major/minor detection
   errors). `MUSICHISTORY_NORMALIZATION=parallel` puts every tonic on C instead. Tempo is
   removed by working on the **quarter-note beat grid** (tempo-invariant); the normalized
   MIDI files are also written at `TARGET_BPM` (120).
3. **Identities, not audio.** Influence is carried by chord-progression identities
   (key-normalized chord sequences and rotation-invariant loops) and melody identities
   (normalized lead line, plus the bass line as a riff channel).
4. **Common patterns are cheap, rare ones are evidence.** Shared material is scored in bits
   of surprise relative to songs released *before* the later song, and tested against a
   first-order Markov null model. I–V–vi–IV alone never makes an edge.
5. **Tree = most referenced influencer.** Each song's tree parent is the most-referenced
   song (most later songs credit it) among its strong significant influencers. Roots are
   songs with no significant earlier influencer.
6. **No lyrics, ever.** Lyric, text, marker and cue events are stripped from every MIDI at
   ingestion. No stage stores or prints lyric text. Only counts and tick positions of lyric
   events are ever used (to find the melody track), and the text is discarded in memory.
7. **Polite acquisition.** robots.txt is honoured, every host is rate limited, bulk datasets
   are preferred over crawling, and the User-Agent carries no personal data
   (`MUSICHISTORY_CONTACT` adds a contact if you choose). bitmidi.com (downloads disallowed
   by robots.txt), cprato, nonstop2k and hooktheory.com (block automated agents) are not used.

## 2. Repository layout and ownership

| Path | Owner stage | Language |
|---|---|---|
| `musichistory/config.py, http.py, textnorm.py, db.py, cli.py, dotnet.py, status.py` | foundation | Python |
| `musichistory/canon/` | canon | Python |
| `musichistory/sources/`, `musichistory/midi/`, `musichistory/select.py` | fetch + select | Python |
| `musichistory/analysis/`, `musichistory/identity/` | analyze | Python |
| `influence/MusicHistory.Influence/` | influence | C# (.NET 10) |
| `layout/MusicHistory.Layout/` | layout | C# (.NET 10, ComputeSharp) — fork of GPU-FDG |
| `unity/` | viewer | Unity 6000.6.3f1 URP — fork of Unity-FDG |
| `tools/` | shared scripts (setup, run-all, sf3→sf2) | |
| `lists/top_songs.csv` | canon (committed output) | |
| `tests/<area>/` | each stage | pytest |

Stages never write another stage's tables or folders. The pipeline database schema is in
`musichistory/db.py` (read its module docstring for ownership and conventions). A stage that
needs extra private tables creates them with `CREATE TABLE IF NOT EXISTS` in its own module.

Python stage modules expose `add_arguments(parser)` and `run(args) -> int`
(see `musichistory/cli.py`).

## 3. Conventions

* `work_id`: Wikidata QID of the composition (`Q2030651`) or `R` + 12 hex of
  SHA-1(`title_core|primary_artist`). Covers of one composition are one work; the node's
  year is the **original** year of the composition (`work.work_year`).
* Pitch class **C = 0** in every table (Resonance-2 bundles use A = 0: `pc_C = (pc_A + 9) % 12`).
* Time in **quarter-note beats** from the start of the sanitized MIDI (`ticks / PPQ`).
* MIDI channels **1..16** (NAudio convention; drums = 10) in tables and JSON.
* Track indices are 0-based indices into the **sanitized** MIDI file (SMF type 1).
* Key names are ASCII: `C`, `C#`, `Db`, ... `Bb`, `B` + ` major` / ` minor`. Flats for
  F, Bb, Eb, Ab, Db, Gb major and D, G, C, F, Bb, Eb minor; sharps otherwise.
* All JSON is compact UTF-8. No lyrics, no free text from MIDI files.

## 4. Stage: canon (song list)

Goal: a reproducible, defensible ranking of works with original release dates.

**List sources** (all raw downloads cached under `data/cache/canon/`, with URL, revision or
commit, SHA-256 and retrieval time in `source_list`):

| list_id | Source | Weight |
|---|---|---|
| `billboard_ye_<year>` (1946–2025) | Wikipedia "Billboard year-end ..." pages via MediaWiki API (`action=parse&prop=wikitext|revid`); page titles differ by era (1946–48 "Billboard year-end top singles of Y", 1949–55 "... top 30 singles of Y", 1956–58 "... top 50 singles of Y", 1959+ "Billboard Year-End Hot 100 singles of Y") | 1.0 each |
| `tsort_5000` | `https://tsort.info/csv/top5000songs-2-9-0001.csv` (credit tsort.info v2.9.0001; ≥12 s between requests) | 6.0 |
| `rs500_2021` | `https://raw.githubusercontent.com/ossings/rolling_stone_top_500_songs_2021/de2e509db48e98a8551d5032ce27d19b276d4c57/top_500_songs.csv` | 4.0 |
| `rs500_2004` | `https://raw.githubusercontent.com/Computational-Cognitive-Musicology-Lab/CoCoPops-RollingStone-legacy/e90def7bee56ce36dc5193fc7f82954e2fe4a0ee/RollingStone_The500GreatestSongsOfAllTime_2004.tsv` | 3.0 |
| `grammy_hof` | Wikipedia "List of Grammy Hall of Fame Award recipients (A–D|E–I|J–P|Q–Z)"; singles/tracks only; pseudo rank 250 | 1.5 |
| `spotify_top` | Wikipedia "List of Spotify streaming records" most-streamed table | 2.0 |
| (dates only) | weekly Hot 100 CSV `https://raw.githubusercontent.com/utdata/rwd-billboard-data/8ad259131a515ffaa55218c6959c03b9e301f0c4/data-out/hot-100-current.csv` → `work.first_chart_week` | – |

billboard.com, rollingstone.com and acclaimedmusic.net are not fetched (robots.txt /
bot challenge). An optional user-exported Acclaimed Music CSV may be imported (`--acclaimed`).

**Resolution to works**: Wikipedia song link → QID (`prop=pageprops&ppprop=wikibase_item`,
50 titles per request, strip `#anchor`); rows without a link → MediaWiki search
`"<title> <artist> song"`, first result typed song/single/musical work/composition/track
(Q7366, Q134556, Q105543609, Q2188189, Q7302866). Follow P2550 (recording of) to the work.
No QID → `R` id from normalized keys. Double A-sides split; B side weight 0.5.

**Years** (`year_evidence`, rules): Wikidata P577 (min over non-deprecated, with precision);
MusicBrainz (1 req/s) for works lacking P577 or failing a check; list years; earliest weekly
Hot 100 week of any recording = upper bound U (drop P577 later than U); drop list years
more than 2 years before an authoritative date; ignore < 1890 (`traditional = 1`, use the
earliest MusicBrainz recording). `work_year` = min of what remains; `year_confidence` high
(≥ 2 sources within 1 year) / medium / review. `effective_year` = the canonical recording's year.

**Fusion**: weighted reciprocal-rank fusion at work level, `score = Σ_l W_l · f / (60 + rank_l)`
summed over every listed recording of the work. `canon_rank` by score. `in_pool = 1` for
the top `POOL_SIZE` (1500) **plus** enough extra works so that every year from 1950 to the
last complete year has at least 8 pooled works (best-scoring first).
`search_artists` = every performer of a listed recording (MIDI files are often named after
the famous cover). `known_influence` gets Wikidata P144 / P2550 links among works and the
validation controls in `musichistory/canon/controls.json` (positive, commonplace-negative
and version pairs, titles/artists/years only), resolved to work ids when present.

**Outputs**: tables above; `lists/top_songs.csv` (committed): `canon_rank, work_id, title,
artist, original_artist, work_year, effective_year, year_confidence, rrf_score, lists`.

## 5. Stage: fetch (candidate MIDI files)

Sources, in order of preference (each an adapter in `musichistory/sources/`):

| source | How | Politeness |
|---|---|---|
| `lakh` / `lakh_clean` | Lakh MIDI Dataset, CC-BY 4.0, **offline**: `http://hog.ee.columbia.edu/craffel/lmd/lmd_full.tar.gz` (1,768,163,879 B, MD5 2536ce3fd2cede53ddaa264f731859ab), `clean_midi.tar.gz` (234,283,029 B), `md5_to_paths.json`, `match_scores.json`; MSD `unique_tracks.txt` from `http://millionsongdataset.com/sites/default/files/AdditionalFiles/unique_tracks.txt` to name lmd_matched MSD ids. Index every original path of every MD5 (570,601 paths) by normalized artist/title; stream only matched members out of the tarballs. | one bulk download each (resume supported) |
| `midicollection` | offline slug/filename index from `https://midicollection.com/sitemap.xml` → `sitemap-songs-1..7.xml` and `sitemap-artists.xml`; artist pages list `data-url="/midi/MIDI/<file>.mid"`; download `/midi/MIDI/<name>`. Never use `?q=` search (robots). | ≥ 2 s per request |
| `freemidi` | `https://freemidi.org/search?q=` → `download3-<id>-...` page (sets session) → `getter-<id>` with Referer. Generic uploads use the `artists-bands` slug with the artist inside the title. Used for works with < 2 valid candidates after the offline sources. | ≥ 3 s per request, ≤ 3 requests per song |
| `midiworld` | `https://www.midiworld.com/search/?q=` → `/download/<id>`; mostly a subset of midicollection; only for works still lacking candidates. | ≥ 3 s |
| Hooktheory (reference, not MIDI) | `https://github.com/chrisdonahue/sheetsage-data/raw/refs/heads/main/hooktheory/Hooktheory.json.gz` (SHA-256 917b7cd5...0e0c, CC BY-NC-SA 3.0): key-relative melody and harmony per section, used only to score candidates. | one download |

**Matching** (`musichistory/textnorm.py`): accept when title ≥ 92 and artist ≥ 85 (against
any of `work.search_artists`), or the title matches and the file is in lmd_matched under an
MSD track whose artist/title match (`probable`). Title-only matches are rejected. A
same-title file under a different artist is a different recording and is rejected unless that
artist is in `search_artists`. At most 12 candidates per work per source (best match first).

**Validation and sanitization** (`musichistory/midi/`), for every downloaded or extracted file:

1. Reject: empty, HTML/XML/JSON bodies, no `MThd` (after unwrapping RIFF `RMID`),
   SMPTE division, format 2, no note-ons, > 800 bars, < 30 s, unparseable.
2. Parse leniently (mido, `clip=True`), keep only `MThd`/`MTrk` chunks.
3. Record pre-strip facts in the features: key signatures, lyric-event **tick positions**
   (text discarded immediately), per-track role keywords found in the original track names.
4. Strip meta events: lyrics, text, marker, cue_marker, copyright, instrument_name,
   sequencer_specific, and **all key signatures** (they mislead the analysis).
5. Split type-0 files into one track per channel; rename every track `T<nn> <role>`
   (role from the name keyword or GM program family); move a drum track found on another
   channel to channel 10 when its name says drums.
6. Close hanging notes, drop a note-on at the final tick, pad `end_of_track` one beat past
   the last note-off.
7. Save as SMF type 1, same PPQ: `data/candidates/<work_id>/<source>__<md5>.mid`.

`candidate.md5` is the MD5 of the original bytes (dedupe across sources: a file already
found in Lakh is not stored twice); `sha256` is of the sanitized file.

**`features_json`** (computed on the sanitized file; `musichistory/midi/features.py`):

```json
{"format": 1, "ppq": 480, "n_tracks": 12, "duration_s": 212.3, "end_beat": 402.0,
 "n_notes": 5200, "n_pitched_notes": 4100, "n_drum_notes": 1100, "has_drums": true,
 "tempo": {"n_events": 3, "median_bpm": 120.0, "min_bpm": 80.0, "max_bpm": 130.0, "stable_fraction": 0.95},
 "time_signatures": [[0.0, 4, 4]],
 "key_signatures": [[0.0, 2, 0]],
 "lyric_events": 0, "text_events": 13, "marker_events": 0,
 "lyric_melody_track": null, "lyric_f1": null, "karaoke": false,
 "tracks": [{"index": 1, "channel": 4, "program": 65, "family": "reed", "is_drum": false,
             "role": "vocal", "n_notes": 520, "mean_pitch": 67.2, "min_pitch": 55, "max_pitch": 79,
             "polyphony": 0.03, "occupation": 0.62, "mean_abs_interval": 2.4, "mean_velocity": 90.0}],
 "warnings": ["type0_split", "hanging_notes_closed:3"]}
```

`key_signatures` rows are `[beat, sharps_flats, minor(0/1)]` from the original file;
`role` is one of `vocal, melody, lead, bass, drums, backing, piano, guitar, strings, other`.

## 6. Stage: select (best source per song)

1. **Static quality** (0..1) from features: pitched tracks ≥ 4, drums on ch 10, an
   identifiable melody (role or lyric timing), sane stable tempo map, duration 90–480 s,
   lmd_matched membership (weighted by match score ≥ 0.7). Penalties: karaoke-only thin
   arrangements, piano-only reductions, ringtone paths, < 60 s, medleys.
2. Analyze the best **4** candidates per work with PatternPrep (via `musichistory.analysis`)
   and extract identities (via `musichistory.identity`).
3. **Consensus**: mean pairwise chord and melody agreement with the work's other analyzed
   candidates (`musichistory.identity.compare`); the medoid of a cluster of agreeing
   transcriptions is most likely right. **Hooktheory agreement** when annotations exist.
4. `total = 0.35·quality + 0.40·consensus + 0.25·hooktheory` (weights re-normalized when a
   term is missing). Choose the best; write `selection` with a one-line reason.
5. **Final set**: walk works in `canon_rank` order, keeping those with a chosen candidate
   whose analysis succeeded, until `TARGET_SONGS`, with a floor of 5 per year from 1950
   to the last complete year minus one (fill from the next-best works of short years,
   dropping the lowest-ranked songs of over-full years). Set `work.selected`.
6. **Source comparison report**: `data/reports/source_comparison.json` and `.md`: per
   source, candidates found, valid, analyzed, chosen, mean quality/consensus/Hooktheory
   agreement, coverage by decade.

## 7. Stage: analyze (Resonance-2 + identities)

**PatternPrep** (`musichistory/analysis/patternprep.py`): build
`<RESONANCE_ROOT>/Tools/PatternPrep/PatternPrep.csproj` into `data/tools/PatternPrep`
(record `git rev-parse HEAD` of Resonance-2), then per song: copy the sanitized MIDI to
`<workdir>/score.mid`, write `<workdir>/song.json` = `{"Style": "pop", "LeadVocalTrack": N}`
(never `Lyrics`, never a `lyrics.txt` or `library.json`), run the exe with a timeout
(drain stdout), read `score.mid.patterns.json`, write the **slim** `analysis.json`, delete
the full bundle. Runs in parallel (processes = CPU count − 1).

**Slim analysis JSON** (`analysis.json`, pitch classes converted to C = 0):

```json
{"version": 5, "resonance_commit": "4d6a8ba...", "style": "pop", "song_bars": 101,
 "end_beat": 402.0, "duration_s": 193.0,
 "tempos": [[0.0, 480000]], "measures": [[0.0, 4, 4, 101]],
 "chords": [[0.0, 4.0, 9, "m"]],
 "sections": [{"first_bar": 37, "bar_count": 9, "family": 5, "role": "Post-Chorus", "letter": "F",
               "start": 148.0, "end": 184.0, "loops": 2, "cycle_beats": 16.0, "transpose": 0,
               "variation": "1-bar tag", "group": -1}],
 "patterns": [{"family": 4, "reference": 4, "loop_bars": 4, "loop_beats": 16.0, "visits": 4,
               "passes": 1, "role": "Chorus", "loop": [[0.0, 4.0, 9, "m"]]}],
 "groups": [{"id": 0, "visits": 4, "families": [2, 3, 4]}],
 "form_grammar": "In (V PC C)x2 Br Out",
 "key_changes": [{"beat": 52.0, "tonic": 9, "minor": true, "from_tonic": 0, "from_minor": false}],
 "key_runs": [[0.0, 52.0, 0, false]],
 "notes": [[4.0, 0.99, 57, 4, 3, 0.87]],
 "parts": [{"track": 7, "channel": 6, "role": "bass", "vocal": false, "bars": 98,
            "fundamental_bars": 18, "grammar": "A x8 B C"}]}
```

`tempos` rows `[beat, microseconds_per_quarter]` (Resonance's synthetic 120 BPM first entry
removed when another tempo exists at beat 0); `measures` rows `[start_beat, num, den, bars]`
(runs); `chords` rows `[start, end, root_pc (−1 rest), quality ∈ {"", m, 7, maj7, m7, dim}]`;
`key_runs` from `Frames[].Key/Minor` converted to beats; `notes` rows
`[beat, length, pitch, track, channel, velocity]` (all notes, drums on channel 10). No
section names from markers (markers are stripped), no lyric fields.

**Python API used by select** (owned by analyze):

```python
from musichistory.analysis import patternprep
patternprep.ensure_built() -> Path            # exe path; builds once
patternprep.resonance_commit() -> str
patternprep.analyze(midi_path, workdir, *, lead_track=None, timeout=180.0) -> dict   # slim dict; raises AnalysisError

from musichistory.identity import extract, compare
ident = extract.extract(slim, features)        # -> SongIdentity
ident.to_db(conn, work_id, **song_columns)     # writes song / key_region / chord_seq / loop / melody_line
compare.chord_agreement(a, b) -> float         # 0..1, key-normalized L1 chord alignment
compare.melody_agreement(a, b) -> float        # 0..1, normalized lead-line alignment (PMI-like)
compare.hooktheory_agreement(ident, sections) -> float | None
```

**Key and normalization** (`musichistory/identity/key.py`): ensemble vote for the home
tonic and mode — Resonance chord-Viterbi home region (duration-weighted mode of `key_runs`)
0.45, Temperley–Kostka–Payne profile on the duration-weighted pitch-class histogram (bass
counted double) 0.25, Krumhansl–Kessler 0.10, original MIDI key signature (only if not
C major) 0.20, root of the final section's last chord +0.1. Relative-major/minor
disagreement is harmless; a fifth apart sets `key_ambiguous_fifth`; anything else sets
`key_review`. Key regions come from Resonance `key_changes` (≥ 8 bars). Region shift
`((target − tonic + 5) mod 12) − 5` with target 0 (major) / 9 (minor, relative) or 0
(parallel), in [−5, 6].

**Identities**: chord sequences (`chord_seq` kinds `chg`, `cd`, `beat`, `keyfree`; token
encodings in `db.py`), loops (`loop`: repeating families with 2–8 chord changes, primitive
period, Booth least rotation → `cycle_id`, phase, rhythm signature), melody and bass lines
(`melody_line`): lead track by name → karaoke lyric timing → per-track classifier →
skyline fallback; skyline of the chosen track; grace notes removed, onsets quantized to
1/12 beat, rests absorbed; pitches shifted per key region; metric class from `measures`.
`interval_entropy` gates the melody channel (< 2.0 bits halves it, < 1.5 drops it).

**Playback facts** in `song`: `native_bpm` (beat-weighted median tempo, ignoring
< 20 or > 400 BPM segments), `beats_per_bar`, `first_downbeat`, `tonic_pc`, `mode`,
`norm_shift`. **Normalized MIDI**: `data/normalized/<work_id>.mid` — every non-drum note
shifted by its region's shift, all tempo events replaced by one `TARGET_BPM` tempo.
The chosen file is copied to `data/songs/<work_id>/score.mid` with its `analysis.json`.

## 8. Stage: influence (C#, `influence/MusicHistory.Influence`)

`MusicHistory.Influence.exe run --db data/musichistory.sqlite --graph data/graph/music_graph.db`

Reads selected, analyzed songs with their identities and dates; writes `pair_score`,
`influence_edge`, `tree_node`, then exports the graph database (§10) and
`data/graph/influence_report.json`.

1. **Order.** `time_value` = fractional year (day precision: year + (doy − 0.5)/365.25;
   month: year + (month − 0.5)/12; year: year + 0.5). A is earlier than B if the years
   differ, or both dates have month-or-better precision and differ, or both have a first
   chart week and they differ by > 4 weeks. Otherwise the pair is contemporaneous: no edge.
2. **Tokens and n-grams** (64-bit FNV-1a over `kind|n|tokens`): melody `int` 5- and 7-grams
   (intervals clipped ±12, repeated notes collapsed), `deg` 6-grams, `mtype` 4-grams
   (interval, IOI-ratio class `clip(round(log2(IOI_{i+1}/IOI_i)), −2, 2)`); bass `int` 5,
   `deg` 6; chord `chg` 3–6, `cd` 3–4, `keyfree` 3–4 (recall only); loop identities
   `(cycle)`, `(cycle, phase)`, `(cycle, phase, rhythm)`.
3. **Rarity.** `w(g) = 0.5·log2((N_<t + 1)/(df_<t(g) + 0.5)) + 0.5·log2((N + 1)/(df(g) + 0.5))`,
   t = time of the later song, df = number of songs containing g. n-grams in more than 5 %
   of songs are stop-grams for candidate generation (they still score, cheaply).
4. **Candidates.** For each B, sum w over shared non-stop n-grams with each earlier A; keep
   the top 60 plus any above 20 bits (max 200).
5. **Alignment.** Smith–Waterman–Gotoh local alignment, top 3 non-overlapping hits:
   melody/bass tokens (pc, metric class, duration class): +2 same pc, 0 for 1–2 semitones,
   −1 otherwise, × 1.0 same metric class / 0.75 otherwise, gaps open 3 extend 0.3, plus a
   Mongeau–Sankoff consolidation move (one note vs 2–3 same-pitch notes, cost 0.2);
   chords (L1 `chg`): `s(a,b) = 3·J(a,b) − 1` (+0.25 same root, different quality), J =
   Jaccard of the triads' pitch-class sets, gaps open 2.5 extend 0.75. If either song has
   `key_ambiguous_fifth`, also align at ±5/±7 with a 3-point penalty.
6. **Evidence.** `E_c` = bits of the shared n-grams whose B occurrence lies inside the
   aligned regions, greedy non-overlapping cover (longest, then heaviest first). Loops:
   `E_loop` = best nested identity weight (a cross-phase cycle match counts 0.5·w).
7. **Null model.** Markov-1 surrogates of B's token sequence per channel (seeded by pair
   and channel, deterministic): K = 20 to screen, K = 100 to confirm pairs with any
   screen z ≥ 1.5. `z_c = (E_c − μ)/max(σ, 0.5)`.
8. **Decision.** A channel counts if `z_c ≥ 2`. `Z = Σ w_c·z_c / sqrt(Σ w_c²)` over the
   channels available in both songs (w: melody 0.6, bass 0.3, chord 0.3, loop 0.1; melody
   weight halved or dropped by the entropy gate). One-sided p from Z, Benjamini–Hochberg
   over all tested pairs → q. Significant when `Z ≥ 3`, `q ≤ 0.05` and (melody ≥ 24 bits
   or bass ≥ 24 bits or chords ≥ 16 bits above μ). `S(A→B) = Σ w_c·max(0, E_c − μ_c)`.
9. **Versions** are not influence: global melody PMI ≥ 0.60, global chord identity ≥ 0.60
   and duration ratio 0.6–1.6 → `relation = 'version'`.
10. **Credit and tree.** Each significant passage of B is credited to the earlier song with
    the highest E for it (within 10 %: the earliest). `ref_count(A)` = number of later songs
    crediting A; `ref_norm` = ref_count / songs after A; Katz with α = 0.2.
    `Cand(B) = {A significant: S(A→B) ≥ 0.5·max S}`; `parent(B) = argmax ref_count`, ties by
    S, then earlier. Roots have no parent. Other significant pairs become `secondary` edges
    (at most 8 per target, by S).
11. **Excerpts** for the walkthrough: B's excerpt is the tree edge's strongest B segment,
    snapped outward to bar lines (`first_downbeat + k·beats_per_bar`), at least 8 and at
    most 24 bars; roots use the passage their children credit most, else the first visit of
    their most-covering loop family, else the first 16 bars.
12. **Validation** in the report: for every `known_influence` pair present, whether it is
    significant, its rank among B's influencers, and whether commonplace negatives stayed
    edge-free; plus the degree distribution.

Subcommands: `run`, `score-pairs --pairs <json [[a_id, b_id], ...]>` (force-score, print
JSON), `export` (graph DB only). Deterministic; parallel over B.

## 9. Stage: layout (C#, `layout/MusicHistory.Layout`, fork of GPU-FDG)

`MusicHistory.Layout.exe data/graph/music_graph.db [--iterations 1500] [--yearScale 2]
[--timeAxis y] [--timeDirection up] [--treeSpring 1.0] [--secondarySpring 0.25] [--restLength 1.5]
[--repulsion 1.0] [--centering 0.02] [--damping 0.8] [--edgeRepulsion 0]`

Temporal kernel (validated prototype): time coordinate pinned exactly
(`(time_value − min_year)·yearScale` on the time axis, oldest at the bottom), x/z free;
springs with rest length on tree (strong) and secondary (weak) edges weighted by
similarity; mass = `1 + log2(1 + descendants)` enters repulsion (`m_i·m_j`) and centering;
softened repulsion (`d² + 0.05`); ping-pong buffers, velocity with damping, quadratic
cooling, per-node step `1/(m + k·Σw + 1)` → deterministic and settled. Deterministic start:
roots on a ring, children at golden-angle offsets near their parent. Writes `nodes`
positions (parameterized SQL, invariant culture), `node_layout_metadata`, `layout_run`.
`demo --out <db> --nodes 1000` writes a synthetic temporal graph in the §10 schema.

## 10. Graph database (`data/graph/music_graph.db`) — influence → layout → Unity

SQLite **3.15-compatible** (Unity's native sqlite3.dll is 3.15.0): no STRICT, no generated
columns, no window functions or UPSERT in views/triggers; `journal_mode=DELETE`.

```sql
CREATE TABLE graph_meta(key TEXT PRIMARY KEY, value TEXT);
-- schema_version=1, generated_at, normalization, target_key ("C major / A minor"),
-- target_bpm, min_time, max_time, song_count, edge_count, root_count,
-- resonance_commit, pipeline_commit, midi_base ("relative to this file's folder")

CREATE TABLE nodes(id INTEGER PRIMARY KEY,          -- 1..N contiguous, ordered by (time_value, work_id)
  position_x REAL, position_y REAL, position_z REAL); -- NULL until layout runs

CREATE TABLE song_node(
  node_id INTEGER PRIMARY KEY,                       -- = nodes.id
  work_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, artist TEXT NOT NULL,
  year INTEGER NOT NULL, release_date TEXT, date_precision INTEGER,
  time_value REAL NOT NULL, canon_rank INTEGER,
  tonic_pc INTEGER NOT NULL, mode TEXT NOT NULL, key_name TEXT NOT NULL,
  norm_shift INTEGER NOT NULL,
  native_bpm REAL NOT NULL, beats_per_bar REAL NOT NULL, first_downbeat REAL NOT NULL,
  midi_path TEXT NOT NULL,                           -- relative to the db folder, '/' separators
  normalized_midi_path TEXT,
  midi_source TEXT,
  excerpt_start_beat REAL NOT NULL, excerpt_end_beat REAL NOT NULL,
  tree_parent_node INTEGER, tree_root_node INTEGER NOT NULL, tree_depth INTEGER NOT NULL,
  ref_count INTEGER NOT NULL, ref_norm REAL, katz REAL, descendants INTEGER NOT NULL,
  in_degree INTEGER NOT NULL, out_degree INTEGER NOT NULL,
  key_confidence REAL, melody_confidence REAL,
  main_loop TEXT,                                    -- e.g. "vi-IV-I-V (i-VI-III-VII)"
  summary TEXT                                       -- short display facts; never lyrics
);

CREATE TABLE influence_edges(
  id INTEGER PRIMARY KEY,
  source_node INTEGER NOT NULL, target_node INTEGER NOT NULL,   -- source is the earlier song
  kind TEXT NOT NULL,                                -- 'tree' | 'secondary'
  channels TEXT NOT NULL,                            -- e.g. 'melody,chord'
  primary_channel TEXT NOT NULL,                     -- 'melody' | 'bass' | 'chord' | 'loop'
  score_bits REAL NOT NULL, z REAL NOT NULL, q REAL,
  similarity REAL NOT NULL,                          -- 0..1 for display and springs
  weight REAL NOT NULL,                              -- spring weight suggestion
  src_start_beat REAL, src_end_beat REAL, dst_start_beat REAL, dst_end_beat REAL,
  evidence TEXT,                                     -- e.g. "loop vi-IV-I-V (same phase); melody 11 notes"
  UNIQUE(source_node, target_node), CHECK(source_node < target_node));

-- written by layout:
CREATE TABLE node_layout_metadata(node_id INTEGER PRIMARY KEY, mass REAL, is_root INTEGER,
  tree_depth INTEGER, display_radius REAL, time_axis_value REAL);
CREATE TABLE layout_run(run_id INTEGER PRIMARY KEY, created_at TEXT, device TEXT,
  iterations INTEGER, loop_ms REAL, final_mean_move REAL, time_axis TEXT, time_direction TEXT,
  year_scale REAL, min_time REAL, params_json TEXT);
```

Invariants: every non-root node has exactly one `kind='tree'` incoming edge, from
`tree_parent_node`; `time_value[source] < time_value[target]`; node ids ordered by time, so
`source_node < target_node`.

## 11. Unity viewer (`unity/`, fork of Unity-FDG, Unity 6000.6.3f1, URP)

* **Loader** reads §10 (default path `<repo>/data/graph/music_graph.db`), creates one node
  per song at its layout position: bubble area ∝ `descendants + 1`, fill = key colour
  (circle-of-fifths hue, minor darker), ring = decade colour; billboard label
  "Title / Artist · Year" (always for the most influential and the active songs, others on
  hover). A **timeline axis** with decade ticks runs along the time axis.
* **Edges**: tree edges always visible (tapered, wide at the influencer, glowing, colour by
  primary channel: chord warm, melody cool, both white, bass green); secondary edges
  hidden until hover. Hover highlights the song, its influencers and influenced songs;
  HUD: title, artist, year, key, BPM, main loop, parent chain.
* **Walkthrough** (`WalkthroughDirector`): modes *lineage* (root → selected song),
  *subtree* (DFS from the selected song or its root, children by date) and *chronological*.
  Per step: camera flies to frame parent and child, the tree edge animates, the child's
  excerpt plays. Controls: click select, Enter start, Space pause, N/→ next, B/← previous,
  Esc exit.
* **Key/BPM morph** (`SongPlayer`): each song **starts in the key and BPM of the song
  played before it** and morphs to its native key and BPM over the first `morphBars`
  (default 4) bars of its excerpt (smoothstep), then plays natively and hands off to the
  next song at a bar line. Transposition offset `o₀ = wrap(prevTonic − tonic)` in [−6, 5]
  glides continuously to 0 (drums untransposed); tempo ratio starts at `P'/N` where P' is
  the previous BPM folded by an octave (×½, ×1, ×2) closest to the native N, and glides to 1.
  Event times come from the integral of the tempo curve, so beats never drift. The first
  song of a tour plays natively. Optional "apples to apples" mode plays the normalized MIDI
  (C major / A minor, 120 BPM) with no morph.
* **Synth**: MeltySynth (MIT, managed) with a General MIDI SoundFont converted from
  MuseScore's MIT-licensed `MS Basic.sf3` by `tools/sf3_to_sf2.py` into
  `data/soundfonts/MS_Basic.sf2`; MIDI parsed with NAudio.Midi (from Resonance-2) into a
  beat-domain sequencer rendered in `OnAudioFilterRead`, 64-frame blocks, two decks for
  the handoff tail. Falls back to a sine synth when no SoundFont is present.
