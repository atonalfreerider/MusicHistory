# MusicHistory.Influence

The influence stage (DESIGN.md §8) in C# (.NET 10). It reads the analyze stage's identities from the
pipeline DB, scores the rare material every later song shares with every earlier song (**V2: the "V8"
analytic corpus null of the calibration benchmark, window max**), decides with an **empirical threshold**,
builds the "most referenced strong influencer" forest, and exports the graph database of DESIGN.md §10
for the layout stage and Unity. The defaults are the **V2.1 calibration on the real data** (below): read
"What the graph can and cannot claim" before using its edges.

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
| `run --db <db> --graph <graph.db> [--report <json>] [--root <repo>] [--threads N] [--generated-at <iso>] [--no-export] [--param name=value]...` | scores every time-ordered pair, decides, writes `pair_score`, `influence_edge`, `tree_node` (previous rows replaced) and the `influence_*` keys of the pipeline `meta` table, the graph DB and `influence_report.json` (next to the graph DB unless `--report`) |
| `score-pairs --db <db> --pairs <json or file> [--debug]` | scores `[[a_id, b_id], ...]` (any order; scored earlier -> later) and prints JSON: fused z, winning window and shift, per-channel S / mu / sigma / z and window maximum, the lane match (`lane_role`), the five best windows, how many of B's windows reach half the pair's z (`windows_above_half` / `windows`), the run's stored threshold; `--debug` lists the winning window's shared rare n-grams (channel, family, df, pair df, bits, `b_beat` / `b_end_beat`, the line each song matched in, and up to four occurrences in A as `a_beats`, so the material can be printed side by side) and ranks A among all earlier songs on that window (`window_rank`, `window_others_half`, `window_runner_up`) |
| `evaluate --benchmark <dir> [--db <pipeline.sqlite>] [--tune] [--threshold-z Z] [--param name=value]...` | scores the calibration benchmark (`pairs.json`, `identities.json.gz`, `df_corpus.npz`, optional `target_lanes.json.gz`) and prints TPR at a window FPR of 1e-3 / 1e-2 per channel and fused: part 1 = the Python V8 port at df cap 5 and uncapped (whatever `DfCap` is), side by side with the Python score files (`scores_v8*.jsonl`, `scores_lanes.jsonl`) and the per-pair max abs dz; part 2 = the production configuration, with the df and documents of the pipeline DB's songs built by the production code when `--db` is given (required for `KeyFreeDf` > 0, else part 2 is skipped); `--tune` = the weight / penalty grid; `--threshold-z` = benchmark recall at a run's pair threshold |
| `export --db <db> --graph <graph.db> [--root <repo>] [--generated-at <iso>]` | rewrites the graph DB from the pipeline tables only |
| `retree --db <db> --graph <graph.db> [--param name=value]...` | redoes credit, tree and export from the stored significant `pair_score` rows (no rescoring), for trying tree rules |
| `make-fixture --out <db> --songs N [--seed S]` | synthetic pipeline DB (db.py schema, realistic sizes) with planted influence in `known_influence`, analyze-style settings meta and `key_region` rows |
| `bench --db <db> [--pairs N]` | single-thread cost of the V8 engine (index build, N later songs against all their earlier songs) |

`--generated-at` fixes `graph_meta.generated_at`, which makes two runs byte-identical (any thread count).
`--root` is the repository root that stored paths (`data/songs/...`) are relative to; by
default the nearest ancestor of the DB holding `musichistory/config.py`, else the DB folder's
parent. `midi_path` and `normalized_midi_path` in `song_node` are relative to the graph DB's
folder with `/` separators (e.g. `../songs/<work_id>/score.mid`).

## How it works (V2; DESIGN.md §8 steps 1 and 9-12 kept, 2-8 replaced)

The diagnosis of the V1 graph (0 of 138 audited edges were borrowings; 1 of 25 known pairs found) and the
calibration benchmark (1,360 positive and 40,800 negative 16-bar windows of real transcriptions) chose the
benchmark's "V8" scorer; V2 ports it exactly and adds the decision.

1. **Order** (unchanged): `time_value` per §8.1; A is earlier than B by `DateOrder.Earlier`, else the pair
   is contemporaneous (same-year rule): it can be a version, never an edge.
2. **n-grams** (`Grams.cs`, a port of the benchmark's `bench_lib.py`): keys are CPython's
   `hash((kind, *tokens))` with the benchmark's kind codes, so a C# key can be looked up in the benchmark's
   `df_corpus.npz` (an identical key is an identical n-gram). Every occurrence also carries its **canonical
   key** (the n-gram transposed so its first pitch class / chord root is 0; key-free families are their own) and
   its **pitch-only projection** (rhythm-coded families: `mtype4` -> its 4 intervals, `mtype5` / `mtype7` -> the
   `int5` / `int7` of the same notes, `dp3` / `dp4` -> the degrees relative to the first, `cd3` / `cdp3` -> the
   canonical `chg3`, `cd4` / `kfd3` -> the canonical `chg4`; riffs have none). Families (the `mix` families of
   `analytic.py`; V2.1 changes in bold):
   * **melody** (lead line, pitch-change sequence): interval 5- and 7-grams, scale-degree 6-grams,
     interval x IOI-ratio 4-grams (`mtype4`; **`ivr4` is the same token sequence under another kind code and is
     left out**, `MelDropFams` 16), **interval x IOI-ratio 5- and 7-grams (`mtype5`, `mtype7`, `MelRhythmFams`)**,
     scale degree x eighth-note slot 3-grams (`dp3`). **Figuration filter** (`LineMinPc` 3, `FigPeriod` 3): a
     melody n-gram spanning fewer than 3 pitch classes (two-note alternations, octaves) or whose pitch classes
     repeat with a period of 2 or 3 notes (broken-chord cycles) is accompaniment, not melody, and is left out of
     the evidence (it stays a document key);
   * **bass**: interval 5-grams, degree 6-grams, `ivr4`, degree x slot 4-grams, n-grams spanning <= 2 pitch
     classes dropped (`bass_mix_npc2`), **plus the riff family** `rs8` (8 notes, repeated notes kept, pitch
     class x sixteenth-note slot) behind a **schema filter**: an `rs8` n-gram counts only if it has >= 2 pitch
     classes and its 7 inter-onset intervals do not repeat with period 1 or 2 (within 0.1 beat), i.e. it is
     not a walking, straight or swung boogie or pumping line (those are left to the pitch families);
   * **chords** (L1 `chg`): change 3-6-grams, change x duration class 3-4-grams, key-free change x
     duration 3-grams (`kfd3`), token x duration x beat-in-bar 3-grams (`cdp3`);
   * **loop** identities (cycle, cycle+phase, cycle+phase+rhythm; auxiliary);
   * **lanes** (**off by default**, `WLanes` 0: no lane is indexed): the lead line of each song against every
     `lane:*` `melody_line` row of the other (melody families): the later song's lead window against each
     earlier lane, and each later lane's window against the earlier lead; the best counts. A lane whose melody
     n-grams overlap the song's own lead with Jaccard >= 0.8 is the lead's own lane and is skipped. Without
     `lane:*` rows the channel is unavailable.
3. **Evidence = V8.** For each 16-bar window W of the later song B (hop 4 bars, bar lines from
   `first_downbeat` / `beats_per_bar`, as `pairlevel.windows_of`) and each channel:
   `S = sum w(g)` over the distinct n-grams g of W that A contains; `mu = sum w(g) p(g)`;
   `sigma^2 = sum w(g)^2 p(g) (1 - p(g))`; `p(g) = df(g) / (N - 2)` with both songs of the pair removed;
   `w(g) = log2((N + 1) / (d + 0.5))` with d = that df + 2; **rare-only**: n-grams with d > `DfCap` (**8**) weigh 0;
   `z = (S - mu) / sqrt(sigma^2 + 4)`. **`KeyFreeDf` 2**: the weight's d is the larger of the pair df of the
   n-gram's canonical key and (rhythm-coded n-grams of the `ProjScope` channels, default chords) of its pitch-only
   projection, each with the true leave-two-out; p(g) keeps the exact df. So a stock progression played in a
   non-home key, or a common progression in an unusual harmonic rhythm, is as common as it is in any key and
   rhythm, and weighs 0 above the cap (`RunCanonical`: a target that holds only the canonical key or the
   projection gets its mu / var correction through their postings). **`NullSizeAdjust` 1**: mu and var of an
   earlier song scale by its key-set size over the channel mean (P(A holds g) ~ p(g) |A| / mean |A|). A window of a line needs >= 8 notes, of chords >= 3 changes, exactly
   as the benchmark (`chans_of`, boundary chords clipped). No alignment gating: alignment only places the
   display passage in A.
4. **Scale: no candidate generation.** `V8Engine` walks each window's rare n-grams through the inverted
   index (df <= cap, so a handful of songs each); every earlier song gets its exact S, and its mu / sigma
   differ from the window's own only through the leave-two-out df of the n-grams it holds, which the same
   walk corrects. Every time-ordered pair is scored exactly (same-time pairs too, for versions). Songs no
   query touched share one value per availability mask.
5. **Transposition hedge.** B's window is also scored at +3, -3, +5, -5 semitones (key-dependent families
   re-extracted, key-free ones reused); a shifted window's fused z pays `ShiftPenalty` (12).
6. **Fusion and window max.** Per window: weighted Stouffer `sum w_c z_c / sqrt(sum w_c^2)` over the
   channels available in both songs (melody 1, chord 1, bass 0.7, lanes 0 = off; the loop channel, weight 0.2,
   only beside a main channel at z >= 5). The pair's statistic is the maximum over B's windows and shifts.
7. **Decision.** The threshold is the empirical `1 - TargetFpr` quantile (default **3e-5**, about 15 expected
   chance pairs of 507,740) of the pair statistic
   over a deterministic sample of `NullSample` (30,000) time-ordered pairs (the smallest FNV hashes of
   "a|b"), recomputed every run: the (floor(fpr n) + 1)-th largest sample value, or an exponential tail fitted
   over the sample's 99th percentile when fewer than 10 values would lie above it. A pair above it is
   **significant** unless:
   * **bass-only**: bass counts at the winning window and melody, chord and lanes do not (a channel counts
     at z >= max(its own 99th sample percentile of window maxima, 3)), and the riff family alone does not
     clear the riff threshold (`RiffFactor` x the fused threshold); the pair then keeps its best other window
     above the threshold that is not bass-only, if any;
   * **hubness** (optional, `HubZ` > 0): min(zA, zB) < HubZ, where zB = (Z - mean) / max(sd, 2) over B's
     earlier songs and zA the same over A's later songs;
   * **version** (§8.9, unchanged): duration ratio 0.6-1.6, chord identity >= 0.6, melody PMI >= 0.6.
   The decision's settings go to the pipeline meta (`influence_*`: threshold, method, sample, target FPR,
   counting and riff thresholds, weights, shifts, df cap, key-free df, line filter, lanes) and `export` copies
   them into `graph_meta`. The report adds the **operating curve** (for FPR 1e-2 ... 1e-5: threshold,
   time-ordered pairs above it, expected chance pairs = FPR x pairs, and 1 - expected / above) and the top 30
   pairs of the null sample (`decision.null_sample_top`), to show what chance matches look like.
8. **S** (bits; the parent rule and `similarity`): `sum_c (w_c / max w) max(0, S_c - mu_c)` at the winning
   window.
9. **Credit, tree, excerpts** (unchanged rules): a decided pair's passages are its counting channels at the
   winning window: B's span = the window (16 bars, so the tree edge's strongest passage is the winning
   window), A's span = the top local-alignment hit of the window against A at the window's shift (the hull of
   A's occurrences of the shared rare n-grams if no hit). Credit, `ref_count`, `ref_norm`, Katz, **parent =
   most referenced among the strong influencers (S >= `ParentFraction` 0.5 x max S)**, ties by S then earlier,
   up to 8 secondary edges, excerpts snapped to bars.
10. **Storage.** `pair_score` holds the null sample, every pair above the sample's 99th percentile, and every
    decided and same-time-checked pair (about 35k rows for 1,012 songs): `e_*` = S_c and `z_*` = z_c at the
    winning window, `z_combined` = the fused z, `q` = the pair's empirical tail probability among the null
    sample, `s_bits` = S; lanes (no column) and each passage's channel z are in `segments_json`.

All tunables are fields of `Params` (`--param Name=value`), each with its justification in `Params.cs`. The V2
evidence is `--param DfCap=5 --param KeyFreeDf=0 --param LineMinPc=0 --param FigPeriod=0 --param MelDropFams=0
--param MelRhythmFams=0 --param NullSizeAdjust=0 --param WLanes=0.7 --param TargetFpr=1e-3`.

## V2 calibration (the V2 port on scratch copies; its defaults are superseded by V2.1 below)

**Port fidelity** (`evaluate --benchmark diag/benchmark`): every benchmark pair's z equals the Python
`analytic.v8` (max abs dz 0.0001, the rounding of the Python files; 0 pairs differ by more than 1e-3), so the
TPR at a window FPR of 1e-3 is identical:

| | melody | bass | chords | fused |
|---|---|---|---|---|
| df cap 5, C# = Python | 0.544 | 0.495 | 0.480 | 0.757 |
| uncapped, C# = Python | 0.544 | 0.483 | 0.537 | 0.761 |
| lead vs every lane incl. lead (uncapped), C# = Python | 0.577 | | | |
| production (cap 5, riff in bass, lanes 0.7, hedge penalty 12) | 0.544 | 0.505 | 0.480 | 0.788 |

**Pair level** on a copy of the 1,012-song pipeline DB (507,740 time-ordered pairs, all scored), defaults:
threshold 22.34 at 1e-3 (null sample p50 -0.02, p99 10.55, p99.9 22.34), 678 significant pairs (508 expected
false at 1e-3), 7 of 25 positive controls and 1 of 42 negative controls with an edge (V1: 1 and 0). With
derived `lane:*` rows: threshold 25.85, 712 significant, 7 of 25 and 1 of 42.

**Fixture** (`make-fixture --songs 1000 --seed 11`): planted melody 56/59, fifth-error 9/9, loop 27/29,
bass riff 5/29 (19 bass-only plants have riff z 27-45, under that fixture's threshold of 46.3), versions
15/15 classified, same-year plants 9/9 edge-free, 0/300 commonplace negatives with an edge, 19 of 291,027
background pairs significant.

**Runtime** (4 threads): all 507,740 pairs in 3.8 s (15 s with 7,559 lanes), whole run 11 s (30 s with lanes); single thread
44 us per pair.

## V2.1 calibration on the real data (analyze v2 DB, 2026-09-30)

Measured on the real pipeline DB after the analyze v2 rerun (1,012 songs, 1,717 `key_region` rows, 4,599 `lane:*`
rows in 980 songs; variants on a byte copy of it, the final run on the DB itself). The benchmark identities were
rebuilt with the analyze v2 code (same seed, windows and pairs; 905 of 3,159 identities changed; identical to the
analyze team's rebuild) and its df recomputed from the analyze v2 DB; `evaluate --benchmark <dir> --db <db>`
reproduces the production numbers below. Precision was measured by labelling edges SPECIFIC / GENERIC from
their matched material printed side by side (scale degrees with bar.beat positions, roman numerals), as in the
diagnosis audit: SPECIFIC = a distinctive idea present contiguously in both songs (a hook, a riff, an identifiable
borrowed progression such as the Pachelbel ground); GENERIC = stock material (scales, arpeggios, pedal and
alternation figures, blues licks, boogie / walking / root bass, I-IV-V, axis, ii-V-I, lament and vamp
progressions, short pentatonic cells) or matches whose rhythm or placement differ.

**V2 as shipped** (defaults of the V2 port, target FPR 1e-3): threshold 26.01; 673 significant pairs (508 expected
by chance), 389 tree edges, 623 roots; the lanes channel counted in 322 of 673 pairs and made hubs of dense lines
(Ode to Billie Joe 93 tree children, Star Dust a subtree of 178); 5 of 25 positive and 1 of 42 negative controls
with an edge; **2 of 40 random tree edges SPECIFIC**.

**Variants** (threshold = empirical fused threshold at the target FPR; benchmark = production fused TPR at a
window FPR of 1e-3 / recall at the pair threshold; controls = known pairs with an edge):

| variant | FPR | threshold | significant | tree edges | biggest parent (children) | controls +/- | benchmark |
|---|---|---|---|---|---|---|---|
| V2 | 1e-3 | 26.01 | 673 | 389 | Ode to Billie Joe (93) | 5 / 1 | 0.787 / 0.707 |
| V2 | 3e-5 | 47.79 | 66 | 58 | Ode to Billie Joe (14) | 2 / 0 | 0.787 / 0.594 |
| + `NullSizeAdjust` 1 | 1e-3 | 24.02 | 606 | 367 | Lips of an Angel (11) | 5 / 1 | |
| + lanes off | 1e-3 | 21.94 | 695 | 411 | Lips of an Angel (13) | 5 / 1 | |
| + `KeyFreeDf` 1, figuration filter | 3e-5 | 30.59 | 110 | 98 | 2 | 3 / 0 | 0.764 / 0.629 |
| + `KeyFreeDf` 2 (chord harmony) | 3e-5 | 26.92 | 72 | 69 | 2 | 3 / 1 | 0.724 / 0.591 |
| + `mtype5`/`mtype7`, `ivr4` out (cap 5) | 3e-5 | 26.75 | 84 | 79 | 3 | 3 / 1 | 0.720 / 0.596 |
| same, `DfCap` 3 | 3e-5 | 25.77 | 52 | 48 | 3 | 3 / 0 | 0.712 / 0.572 |
| **same, `DfCap` 8 (final)** | 3e-5 | 30.00 | 68 | 64 | 2 | 4 / 1 | 0.730 / 0.601 |
| same, `DfCap` 10 | 3e-5 | 31.53 | 65 | 61 | 2 | 4 / 1 | 0.732 / 0.595 |
| same, `DfCap` 12 | 3e-5 | 33.51 | 55 | 52 | 2 | 4 / 0 | 0.740 / 0.589 |
| cap 5, `ShiftPenalty` 6 / 20 | 3e-5 | 27.13 / 26.49 | 82 / 89 | 77 / 83 | 3 | 3 / 1 | 0.721 / 0.594, 0.718 / 0.594 |
| cap 5, `WChord` 0.5 | 3e-5 | 28.99 | 74 | 68 | 3 | 2 / 1 | 0.713 / 0.590 |
| cap 5, `WBass` 1 | 3e-5 | 26.12 | 77 | 71 | 3 | 3 / 1 | 0.721 / 0.606 |
| cap 8, `WBass` 1 | 3e-5 | 29.24 | 69 | 64 | 2 | 4 / 1 | 0.732 / 0.610 |

What moved precision and what did not:
* The **chance share** follows the FPR: at 1e-3 about 508 of the ~680 pairs above the threshold are expected by
  chance, at 3e-5 about 15 of 71 (the report's operating curve). The threshold at 3e-5 comes from an exponential
  tail fitted over the null sample's 99th percentile, which may underestimate the tail: with a 150,000-pair sample
  (cap-5 variant) the empirical 1e-4 quantile was 30.5 against the fit's 22.7, although that sample also holds
  about 30 % of the real relations (Tainted Love -> SOS is its maximum).
* `NullSizeAdjust`, lanes off, `KeyFreeDf` and the figuration filter removed the artefacts (dense-line hubs,
  accompaniment figures, stock progressions in a non-home key frame or an unusual harmonic rhythm): the null
  sample's maximum fell from 62.4 to 35.4 and no parent has more than 2 children.
* **None of it separates a stock pattern from a borrowing.** Among the pairs above threshold, most share an
  exact, long passage (identical scale degrees and rhythm, 10-15 notes or 6-12 chord changes) of stock material:
  that is not chance (it is rarer than random pairs allow), it is style. Measured on labelled edges, these did not
  separate SPECIFIC from GENERIC either: the corpus LM surprisal of the matched notes (23-37 bits SPECIFIC, 14-50
  GENERIC), the rank of A among all earlier songs on the winning window (rank 1 for 24 of 25), how many of B's
  windows repeat the match (2/21-34/34 vs 1/27-33/34), note identity of the chain, or requiring rhythm-coded
  melody n-grams (which loses He's So Fine -> My Sweet Lord). No threshold, cap, weight, penalty or bass rule
  reached the 80 % target: the 25 edges of highest z of a strict variant held 4 SPECIFIC, the final graph 7 of 67.

**Final defaults** (the calibration table; `Params.cs`, checked by `CalibrationTests.DefaultsAreTheCalibratedOnes`):

| parameter | V2 | V2.1 | why |
|---|---|---|---|
| `TargetFpr` | 1e-3 | 3e-5 | about 15 expected chance pairs instead of 508 |
| `DfCap` | 5 | 8 | a shared idea of a cluster of up to 8 songs (the Pachelbel group) still counts: C U When U Get There -> Memories becomes an edge; 10 and 12 add nothing |
| `KeyFreeDf`, `ProjScope` | 0 | 2, 1 | rarity is transposition-invariant; a chord n-gram with durations is as rare as its progression |
| `NullSizeAdjust` | 0 | 1 | dense lines were hubs |
| `LineMinPc`, `FigPeriod` | 0, 0 | 3, 3 | two-note alternations and broken-chord cycles are accompaniment |
| `MelDropFams` | 0 | 16 | `ivr4` duplicated `mtype4` token for token |
| `MelRhythmFams` | 0 | 1 | pitch-and-rhythm evidence over 6 and 8 notes |
| `WLanes` | 0.7 | 0 (off) | lanes matched accompaniment figures; no known pair needs them |
| `WMelody`, `WChord`, `WBass`, `WLoop` | 1, 1, 0.7, 0.2 | unchanged | chord 0.5 lost a control, bass 1 changed nothing measurable |
| `Hedge`, `ShiftPenalty` | 1, 12 | unchanged | 6 and 20 within noise |
| `RiffFactor` (bass rule) | 1 | unchanged | 4 bass-only pairs rejected, 2 cleared by their riff |
| `ParentFraction` | 0.5 | unchanged | the user's rule; with at most 2 children per parent it decides nothing here |
| `NullSample`, `CountFpr` | 30,000, 0.01 | unchanged | |

**Final run on the real DB** (`python -m musichistory influence`, 16 s; layout 7 s): threshold 29.97 (exponential
tail fit, 30,000 of 507,740 time-ordered pairs, 3e-5); 71 pairs above it, 67 significant (4 bass-only rejected),
63 tree edges, 4 secondary edges, 949 roots, depth 0 / 1 / 2 = 949 / 57 / 6; tree edges by primary channel:
chord 28, melody 26, bass 9. Known pairs: 4 of 25 positives with an edge, each the parent (Tainted Love -> SOS,
I Won't Back Down -> Stay with Me, Hook -> C U When U Get There, C U When U Get There -> Memories); 1 of 42
negatives (Rock Around the Clock -> Blue Suede Shoes, a shared blues vocal formula). Benchmark: production fused
TPR 0.730 at a window FPR of 1e-3, recall 0.602 at the pair threshold. Of the 138 edges the diagnosis labelled
GENERIC, 2 are still edges (All Shook Up -> Great Balls of Fire, No Scrubs -> Single Ladies). **Precision: 4 of
the 44-edge audit sample SPECIFIC (40 random tree edges: 4; the 10 edges of the 5 biggest parents: 0), 7 of all 67
edges** (Tainted Love -> SOS, Every Breath You Take -> I'll Be Missing You, Angel of the Morning -> Angel, I Won't
Back Down -> Stay with Me, the Pachelbel pairs Hook -> C U When U Get There and C U When U Get There -> Memories,
and (They Long to Be) Close to You -> We've Only Just Begun, the same 14-note phrase in both transcriptions).

### What the graph can and cannot claim

* It can claim that each edge's two songs share an unusually long, contiguous passage of rare material (the same
  scale degrees and rhythm, or the same chord sequence) at a rate that random song pairs of this corpus reach
  about 3 times in 100,000, and it shows where (the winning window of the later song and the passage in the
  earlier one). It is deterministic and strictly earlier -> later.
* It cannot claim that an edge is a borrowing: about 1 edge in 10 is (7 of 67 by the audit above); most are the
  same stock pattern of a shared style (boogie and walking bass, blues riffs, ii-V-I and lament progressions,
  scale runs, pedal figures). Lineages are at most two edges deep and mostly style kinship.
* It cannot claim absence: 949 songs have no parent, and 21 of the 25 known borrowings are not edges. Among them
  the diagnosis marks as not recoverable from these files The Last Time -> Bitter Sweet Symphony (the orchestral
  figure is not in the Stones file), More Than a Feeling -> Smells Like Teen Spirit, Basket Case -> Hook, Go West
  -> Hook and Hook -> Memories (Hook's MIDI is 12 bars); No Scrubs -> Shape of You and Mary Jane's Last Dance ->
  Dani California share nothing any n-gram variant finds. He's So Fine -> My Sweet Lord (17.0) and The Air That I
  Breathe -> Creep (23.8) score high but below the threshold.
* Telling a borrowing from a stock pattern would need knowledge the statistics do not have (a curated list of
  stock schemata, or a musicologist's review of each edge).

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

`graph_meta` also carries the run's decision (`influence_threshold_z`, `influence_threshold_method`,
`influence_target_fpr`, `influence_null_sample`, `influence_time_ordered_pairs`, `influence_riff_threshold_z`,
`influence_count_z_<channel>`, `influence_channel_weights`, `influence_shifts`, ...), copied from the pipeline
meta keys `influence_*` that `run` writes, so `export` alone reproduces them. `influence_edges.channels` /
`primary_channel` name the counting channels in the graph's vocabulary (lanes evidence is `melody`).

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
MUSICHISTORY_BENCHMARK=<diag/benchmark> .venv/Scripts/python -m pytest tests/influence -q -k benchmark   # port check
```

`V8Tests` pin the port to golden vectors from the benchmark's Python (tuple hashes, n-gram keys per family,
analytic S / mu / sigma / z on a toy corpus at caps none / 5 / 3); `DecisionTests` check the engine against
the direct formula, the window max, the hedge and its penalty, the lanes channel, the empirical threshold,
the bass-alone rule and the parent rule on in-memory corpora with planted borrowings (V2 evidence settings);
`CalibrationTests` cover the V2.1 settings: canonical keys are transposition-invariant, projections are the
pitch-only content, the figuration filter, a stock progression played in 14 songs in other keys weighs nothing
under `KeyFreeDf`, the engine equals the direct formula under the calibrated defaults (canonical and projection
weights with true leave-two-out, size-adjusted null, filter, extra families), and the defaults themselves. The
end-to-end fixtures run at `TargetFpr` 1e-3: with 120-150 songs every time-ordered pair is in the null sample,
planted pairs included, so the 3e-5 default would put the threshold above them.
