# MusicHistory.Influence

The influence stage (DESIGN.md §8) in C# (.NET 10). It reads the analyze stage's identities from
the pipeline DB, scores rare shared material between earlier and later songs against a
first-order Markov null, builds the "most referenced influencer" forest, and exports the graph
database of DESIGN.md §10 for the layout stage and Unity.

```
dotnet build -c Release influence/MusicHistory.Influence/MusicHistory.Influence.csproj
MusicHistory.Influence.exe run --db data/musichistory.sqlite --graph data/graph/music_graph.db
```

or, through the Python CLI (builds into `data/tools/influence` first):

```
python -m musichistory influence [-- --threads 4 --param name=value ...]
```

## Commands

| command | what it does |
|---|---|
| `run --db <db> --graph <graph.db> [--report <json>] [--root <repo>] [--threads N] [--generated-at <iso>] [--no-export] [--param name=value]...` | steps 1-12: writes `pair_score`, `influence_edge`, `tree_node` (previous rows replaced), the graph DB and `influence_report.json` (next to the graph DB unless `--report`) |
| `score-pairs --db <db> --pairs <json or file> [--debug]` | force-scores `[[a_id, b_id], ...]` and prints per-channel E, mu, sigma, z, Z, S, PMI, segments as JSON; `--debug` lists the counted n-grams and the null's composition |
| `export --db <db> --graph <graph.db> [--root <repo>] [--generated-at <iso>]` | rewrites the graph DB from the pipeline tables only |
| `retree --db <db> --graph <graph.db> [--param name=value]...` | redoes credit, tree and export from the stored `pair_score` rows (no rescoring), for trying tree rules |
| `make-fixture --out <db> --songs N [--seed S]` | synthetic pipeline DB (db.py schema, realistic sizes) with planted influence in `known_influence`, analyze-style settings meta and `key_region` rows (about a third of the songs modulate) |
| `bench --db <db> [--pairs N]` | single-thread kernel throughput, AVX2-vs-scalar equality check, cost per pair |

`--generated-at` fixes `graph_meta.generated_at`, which makes two runs byte-identical.
`--root` is the repository root that stored paths (`data/songs/...`) are relative to; by
default the nearest ancestor of the DB holding `musichistory/config.py`, else the DB folder's
parent. `midi_path` and `normalized_midi_path` in `song_node` are relative to the graph DB's
folder with `/` separators (e.g. `../songs/<work_id>/score.mid`).

## How it works (DESIGN.md §8, with the interpretations this code makes)

1. **Order**: `time_value` per §8.1. A year-precision song whose first Hot 100 week falls in its
   year uses that week as its day, so the chart-week rule can order it without contradicting the
   time axis; any "earlier" verdict must also agree with `time_value` (else contemporaneous).
   A first chart week from **another year** than `work_year` (canon stores the earliest week of any
   recording: a reissue or cover of a pre-1958 or uncharted original) is ignored entirely, so it
   never orders two same-year songs (DESIGN §10).
2. **n-grams**: FNV-1a 64 over `kind|n|tokens` (little-endian int32 tokens). Melody `int` 5/7,
   `deg` 6, `mtype` 4 and bass `int` 5 / `deg` 6 on the pitch-change sequence (repeats
   collapsed); chords `chg` 3-6, `cd` 3-4 (evidence) and `keyfree` 3-4 (candidates only); loop
   identities `(cycle)`, `(cycle, phase)`, `(cycle, phase, rhythm)`, re-rotated after a fifth shift.
3. **Rarity** exactly as §8.3 (time-sliced at the later song). Stop-grams: df > max(2, 5 % of N).
4. **Candidates**: top 60 + any above 20 bits (max 200), plus same-time pairs above 20 bits
   (checked for versions only).
5. **Alignment**: Smith-Waterman-Gotoh with the §8.5 scores (integers x20), Mongeau-Sankoff
   consolidation for lines, the +5/+7 hedge (3-point penalty) when either key is ambiguous by
   a fifth; the shift with the best total top-hit score wins and is reused by the null.
   **Top 3 non-overlapping hits** = Waterman-Eggert by masking: the best cell (each cell carries
   its alignment's start), then the best alignment in the blocks left after closing each hit's
   rows and columns. Surrogates are aligned 8 at a time in AVX2 lanes (bit-identical to the
   scalar kernel; `bench` and the unit tests check this).
6. **Evidence**: bits of n-grams present in B inside a hit's B span whose hash occurs in A inside
   the same hit's A span; greedy non-overlapping cover of B (longest, then heaviest); **each
   distinct n-gram counts once per pair** (a coincidence repeated by B's own chorus is not
   multiplied). **Commonplace n-grams (in more than 2 % of songs) still score, but cheaply: at
   most 8 bits per channel per pair** (the MIR report's 8-bit cap on commonplace schemas). Loops:
   best nested identity, a cross-phase cycle match counts 0.5 w.
7. **Null**: Markov-1 walks over B's own transitions (state = MIDI pitch for lines, L1 token for
   chords; each step carries the chosen token's rhythm), seeded by (A, B, channel), K = 20 screen,
   K = 100 confirm for pairs with any screen z >= 1.5. `z = (E - mu) / max(sigma, 4 bits)`: a
   floor of about half of one rare n-gram (the design's 0.5 bit let one chance-shared n-gram
   against an all-zero null reach z = 10-16). Loop identities are not sequences: their null
   replaces A by random earlier songs ("shared with A more than with any earlier song?").
   Two exact-or-conservative savings: a channel with E < 2 x 4 bits cannot reach z = 2 and is not
   tested (z stored NULL); a pair whose observed bits cannot pass the bits gate (mu >= 0) is never
   significant, so its null is skipped and it enters BH with p = 1 (z_combined, s_bits NULL).
8. **Decision**: a channel counts if z_c >= 2; **Stouffer over the counting channels**, the loop
   channel (weight 0.1) only next to a counting melody/bass/chord channel. BH over all tested
   pairs; significant when Z >= 3, q <= 0.05 and (melody >= 24 or bass >= 24 or chord >= 16 bits
   above mu). S = sum w_c max(0, E_c - mu_c).
9. **Versions**: duration ratio 0.6-1.6 (seconds), then global chord identity >= 0.6, then melody
   PMI >= 0.6 (Needleman-Wunsch identity, gap open 12 / extend 6, pitch classes, at the pair's
   shift) -> relation 'version', never an edge.
10. **Tree**: the shared passages of B are the counted n-grams of each pair and channel,
    clustered along B (segments in `pair_score.segments_json`; a hit itself spans most of both
    songs under the design's cheap gaps). A pair's passages within 32 beats form one passage;
    passages of all significant pairs are merged; a passage is credited only if it carries at least
    12 bits, to the song whose segments cover >= 50 % of it with the highest E (within 10 %: the
    earliest). ref_count, ref_norm, Katz (alpha 0.2) over the credit graph; parent = most
    referenced among S >= 0.5 max S (ties: S, then earlier); up to 8 secondary edges by S.
11. **Excerpts** snapped to bar lines, 8-24 bars (roots: most-credited passage, else the first
    visit of the most-covering loop, else 16 bars).
12. **Report**: counts, degree/ref-count/depth histograms, channel mix, timings, validation
    against `known_influence` (positives: significant / edge / parent / top-3; negatives: edges;
    versions: classified), plus per-plant recall and background false positives on fixtures.

All tunables are fields of `Params` (`--param Name=value`). `--param ZOverCounting=0
--param StopCapBits=1e9 --param CapFraction=1 --param SigmaFloor=0.5 --param LoopNullCorpus=0
--param LoopAuxiliary=0` gives the literal reading of DESIGN §8; `NullDistinct`, `NullKeepRhythm`
and `EvidenceStopGrams` are experiment switches.

## Calibration (why some values differ from DESIGN §8)

Measured on a `make-fixture --songs 1000 --seed 11` corpus (lead ~395 notes, bass ~340, ~136
chord changes; planted melody / bass-riff / rare-loop / fifth-error borrowings, versions, same-year
plants, 300 commonplace negatives). Rates per tested candidate pair at a Z threshold only (no BH),
126 planted pairs expected to give an edge and 3000 random background candidate pairs
(`score-pairs`, scratch script):

| setting | Z >= 3: recall / background FP | Z >= 4 | Z >= 5 |
|---|---|---|---|
| literal DESIGN reading (`--param ZOverCounting=0 --param StopCapBits=1e9 --param CapFraction=1 --param SigmaFloor=0.5 --param LoopNullCorpus=0 --param LoopAuxiliary=0`) | 72/126 / 1.50 % | 53/126 / 0.57 % | 40/126 / 0.13 % |
| this implementation (defaults) | 90/126 / 1.33 % | 78/126 / 0.60 % | 60/126 / 0.23 % |

Bass-riff plants: 15/29 at Z >= 3 with the defaults against 4/29 with the literal reading.
Full run with BH (defaults): 90/126 planted pairs significant (melody 55/59, fifth-error 7/9,
bass 15/29, rare loop 13/29), the planted source is the tree parent for 78 of those 90,
2/300 commonplace negatives got an edge, 15/15 versions classified, 9/9 same-year plants
edge-free, and 966 of 73,718 background pairs (1.3 %) were significant.

What each change fixes: counting-only Stouffer lets a single strong channel (a bass riff) make an
edge; the 8-bit cap on commonplace n-grams (> 2 % of songs) stops bass lines that follow common
loops from adding up to 100 bits; the 4-bit sigma floor stops one chance-shared n-gram against an
all-zero null from reaching z = 10-16; the corpus null for loop identities and the auxiliary loop
channel stop shared ubiquitous loops (Markov walks rarely reproduce an exact cycle and phase) from
driving Z. Fixture melodies are drawn from one shared generator, so unrelated songs share material
more often than any per-song Markov-1 surrogate predicts; on 20 real analyzed songs every
unrelated pair had Z = 0 and only three same-song duplicates scored (Z 6.4-11.0). Check the
report's `validation` on the real corpus and raise `ZMin` if precision is poor.

## Graph DB

Created fresh (temp file, then moved into place), `journal_mode=DELETE`, only tables, a UNIQUE
and a CHECK constraint: readable by SQLite 3.15.0 (verified with Unity-FDG's own sqlite3.dll in
`tests/influence`). Node ids 1..N follow (time_value, work_id); positions are NULL until layout.
`similarity = 1 - 2^(-S/16)`; `weight` = `0.5 + 0.5 similarity` for tree edges, `similarity` for
secondary ones (layout multiplies by its tree/secondary spring factors). `evidence` and
`summary` are channel facts and Resonance form labels only, never text from a MIDI file.

`song_node.entry_tonic_pc/entry_mode` and `exit_tonic_pc/exit_mode` are the `key_region` (analyze)
containing `excerpt_start_beat` and `excerpt_end_beat - 0.001`, looked up as analyze's
`ShiftMap.region_at` does (last region starting at or before the beat); NULL when that region is
the song's home key or the song has no regions. The viewer hands off in these keys, so an excerpt
that sits in a modulation keeps the key continuous (the report counts
`graph.excerpts_outside_home_key`).

`graph_meta.normalization` / `target_key` / `target_bpm` describe how analyze made the normalized
MIDI, read from the pipeline DB, never from the exporting shell: meta `analyze_normalization` /
`analyze_target_bpm`; else the exported songs' `song.normalization` / `song.target_bpm` (majority,
warned when mixed); else the legacy meta key `normalization` (old fixtures); only then
`MUSICHISTORY_NORMALIZATION` / `MUSICHISTORY_TARGET_BPM`, else relative / 120 -- each fallback
logged as `WARNING: graph_meta: ...` and listed in the report's `warnings` (sources in
`graph_meta_settings`).

## Tests

```
dotnet test -c Release influence/MusicHistory.Influence.Tests/MusicHistory.Influence.Tests.csproj
.venv/Scripts/python -m pytest tests/influence -q
```
