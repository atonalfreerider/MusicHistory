# MusicHistory.Layout

The **layout** stage of MusicHistory (DESIGN.md §9): a GPU force-directed layout of the song
influence forest with **release time pinned to one axis**. It reads the graph database the
influence stage writes (§10), finds the two free coordinates of every song on the GPU, and writes
the positions, `node_layout_metadata` and a `layout_run` row back into the same file for Unity.

It is a new version of [GPU-FDG](https://github.com/atonalfreerider/GPU-FDG) (MIT, see
[LICENSE.md](LICENSE.md)): same idea (one ComputeSharp compute thread per node, O(N²) repulsion,
CSR springs, optional node-edge clearance), rebuilt for a temporal DAG. The social-graph input,
the anchor/gravity/hierarchy terms and the random demo were dropped; the time axis, typed
weighted edges, influence mass, ping-pong buffers, velocity damping and cooling were added.

## Build and run

Requires the .NET 10 SDK and a DirectX 12 device (any GPU, or Windows' WARP software device).

```powershell
# from the repository root, through the pipeline CLI (builds into data/tools/layout):
.venv\Scripts\python -m musichistory layout                    # lays out data/graph/music_graph.db
.venv\Scripts\python -m musichistory layout -- --timeAxis z    # extra options after --

# or directly:
dotnet build -c Release layout\MusicHistory.Layout\MusicHistory.Layout.csproj
layout\MusicHistory.Layout\bin\Release\net10.0-windows\MusicHistory.Layout.exe data\graph\music_graph.db
```

| Command | What it does |
|---|---|
| `MusicHistory.Layout <graph.db> [options]` (or `run <graph.db>`) | validate, lay out, write in place |
| `run <graph.db> --out <copy.db>` | copy first, write the copy (input untouched) |
| `run <graph.db> --dry-run [--trace 100]` | compute and report only |
| `demo --out <db> [--nodes 1000] [--seed 42] [--roots 0.04] [--no-layout]` | write a synthetic §10 graph, then lay it out |
| `validate <graph.db> [--lenient]` | check the §10 invariants only |
| `check <graph.db> [--json]` | measure a laid-out file (time-axis error, NaN, clustering, spacing, positions hash) |
| `devices` | list DirectX 12 devices |
| `themes <themes_graph.db> [options] [--out <copy.db>] [--dry-run] [--json]` | lay the lyric themes graph out (DESIGN.md §12, see "Lyric themes" below) |
| `themes-demo --out <db> [--songs 1012] [--seed 42] [--from <music_graph.db>] [--no-layout]` | write a synthetic §12 themes graph, then lay it out |

Exit codes: 0 ok, 1 unexpected error, 2 bad command line, 3 the graph breaks a §10 invariant,
4 no usable GPU / GPU failure, 5 non-finite positions (nothing written), 6 `check` found a
problem. (GPU-FDG ended with `Environment.Exit(0)`, which reported crashes as success.)

### Options (defaults)

| Option | Default | Meaning |
|---|---|---|
| `--iterations` | 1500 | force iterations |
| `--yearScale` | 2 | world units per year on the time axis |
| `--timeAxis y\|z` | y | y: a vertical trunk; z: a fly-through corridor |
| `--timeDirection up\|down` | up | up: oldest at 0, newer at +axis; down: newer at −axis |
| `--spring` | 0.08 | global spring stiffness k |
| `--treeSpring` / `--secondarySpring` | 1.0 / 0.25 | per-kind multipliers |
| `--springWeight weight\|similarity\|none` | weight | edge column that scales each spring |
| `--restLength` | 1.5 | spring rest length |
| `--repulsion` | 1.0 | node-node repulsion (× m_i·m_j) |
| `--softening` | 0.05 | added to d² in the repulsion |
| `--centering` | 0.02 | pull toward the time axis (× m_i) |
| `--damping` | 0.8 | velocity damping |
| `--edgeRepulsion` / `--edgeClearance` | 0 (off) / 0.5 | node-edge clearance, O(N·E) |
| `--massOutDegree` | 0 | extra mass `A·log2(1 + out_degree)` |
| `--startTemperature` / `--minTemperature` | 3 / 0.01 | step limit, cooled quadratically |
| `--rootSpacing` / `--childSpacing` | 6 / 2 | deterministic start |
| `--pinLargestRoot` | true | hold the largest tree's root on the axis |
| `--radiusScale` | 0.2 | `display_radius = radiusScale·sqrt(1 + descendants)` |
| `--batch` | 100 | iterations per GPU command list |
| `--device` | default | `default`, `warp`, an index from `devices`, or a name substring |
| `--lenient` | off | recompute mismatched tree_root_node / tree_depth / descendants instead of failing |

Names are case-insensitive and dashes are ignored (`--yearScale`, `--year-scale`); `--name value`
and `--name=value` both work.

## The model

Time coordinate (exact, never integrated): `sign · (time_value − min_time) · yearScale`, `sign`
= +1 for `up`, −1 for `down`, on `y` (default) or `z`. The other two axes are free. Per free node
i, per iteration (one GPU thread each, `Shaders/TemporalForceShader.cs`):

```
spring      F += k · kindSpring · w_e · (|d| − restLength) · d/|d|       both endpoints of every edge
repulsion   F += repulsion · m_i · m_j · d / (|d|² + softening)^{3/2}   all pairs, 3-D distance
edge clear. F += edgeRepulsion · û / (dist² + edgeClearance²)            optional, edges not incident to i
centering   F_free −= centering · m_i · p_free
mass        m = 1 + log2(1 + descendants) [+ massOutDegree · log2(1 + out_degree)]
step        v = damping · v + F_free / (m + k · Σ kindSpring·w_e + 1),  |v| ≤ T,  p_free += v
cooling     T(it) = startTemperature · (1 − it/iterations)² + minTemperature
```

`w_e` is the edge's `weight` column by default (the influence stage's spring suggestion:
`0.5 + 0.5·similarity` for tree edges, `similarity` for secondary ones). Because time is pinned,
an edge spanning more than `restLength` in time always pulls its endpoints together on the free
plane, so lineages stack into strands; repulsion (3-D distance) spaces out songs of similar
date; mass makes influential songs push harder and sit nearer the axis.

**Deterministic and settled.** Positions are ping-ponged (every thread reads buffer A and writes
its own element of B), sums run in a fixed order, the start is deterministic (the largest
tree's root on the axis, the other roots on a ring whose circumference is `rootSpacing` × their
number, every child `childSpacing·sqrt(k+1)` from its parent at golden-angle offsets over its
siblings, first child pointing outward), and the per-node step `1/(m + kΣw + 1)` keeps hubs with
any number of children stable. Two runs on one device give bit-identical positions. Iterations
are recorded 100 per `ComputeContext` with UAV barriers (one CPU wait per batch; same bits as one
dispatch per iteration). Each run also reports the *residual*: the step one more undamped,
uncapped iteration would take, which the final mean move (capped by `minTemperature`) cannot show.

## Input checks (refused with exit code 3, every problem listed)

* `nodes.id` is 1..N contiguous and every node has one `song_node` row;
* ids are ordered by `time_value` (finite);
* every influence edge runs from a strictly earlier song (`source_node < target_node`,
  `time_value[source] < time_value[target]`), `kind` is `tree` or `secondary`, `similarity` in
  [0, 1], `weight` finite and ≥ 0, no duplicate pairs;
* every non-root has exactly one `tree` edge in, from `tree_parent_node`; roots have none;
* `tree_root_node`, `tree_depth` and `descendants` agree with the parent links (`--lenient`
  recomputes them with a warning).

## What it writes (DESIGN.md §10, SQLite 3.15 compatible, journal_mode=DELETE)

* `nodes.position_x/y/z` — parameterized `UPDATE ... WHERE id = $id`; the time axis value is
  written from the exact double, the free axes from the GPU's floats.
* `node_layout_metadata(node_id, mass, is_root, tree_depth, display_radius, time_axis_value)` —
  replaced on every run (a table with other columns, e.g. GPU-FDG's, is dropped and recreated).
* `layout_run(run_id, created_at, device, iterations, loop_ms, final_mean_move, time_axis,
  time_direction, year_scale, min_time, params_json)` — one row per run; readers use the largest
  `run_id`. `params_json` holds every parameter plus `stats` (counts, pinned node, ms/iteration,
  final mean/max move, residual mean/max, version). A year Y maps to the time axis at
  `sign · (Y − min_time) · year_scale`, which is how Unity can draw a decade ruler.

Only CREATE TABLE / INSERT / UPDATE / DELETE are used; the tests open the output with Unity's
own sqlite3.dll 3.15.0 and run `PRAGMA integrity_check` and the loader's joins.

## Demo graph

`demo --out <db> --nodes N` writes a synthetic graph in the exact §10 schema (deterministic in N
and `--seed`; the same arguments give a byte-identical file) and lays it out: songs
"Song 0001".."Song N" by "Artist 001".., original dates 1940–2025 (year, month or day
precision; `time_value` as in §8.1), keys with the relative `norm_shift`, BPM 60–190, 3/4/6-beat
bars, bar-aligned 8–24-bar excerpts (about one in eight with `entry_*`/`exit_*` keys set: a final
lift, a relative-key opening or a dominant bridge; own RNG, so the tree is unchanged), placeholder MIDI paths `../songs/<work_id>/score.mid` (the
files do not exist), and an influence forest grown by recency-weighted preferential attachment
among clearly earlier songs (≈4 % roots) with 0–3 secondary edges per song and plausible edge
channels, bits, z, q, similarity, weight, beat spans and evidence strings. `graph_meta.synthetic`
is `1`. No text from any real song is involved.

## Measured (RTX 2080 Ti, default options, 1500 iterations)

| Demo graph | ms / iteration | final mean move | residual mean / max | max time-axis error | NaN | mean free-plane distance: tree / secondary / random pairs |
|---|---|---|---|---|---|---|
| 1000 songs, 2114 edges, 41 roots | 0.08–0.11 | 1.4e-3 | 3.5e-4 / 3.4e-2 | 0 | 0 | 3.96 / 18.15 / 24.28 |
| 5000 songs, 10934 edges, 188 roots | 0.31–0.36 | 3.8e-3 | 2.0e-3 / 1.1e-1 | 0 | 0 | 7.30 / 37.22 / 51.06 |

Process wall time 0.9 s (1000) / 1.3 s (5000) including device start-up and SQLite I/O. A
20,000-song demo runs at 1.67 ms/iteration. With `--edgeRepulsion 0.05` the 1000-song run takes
0.42 ms/iteration. A graph exported by the influence stage from its own `make-fixture` pipeline
(200 songs, 157 roots, 83 edges) validates and lays out cleanly. GPU-FDG on the same machine:
1.04 ms/iteration at 1000 nodes, never settled (mean move 3.1–3.4 per iteration), and differed
by 9.3 units per node between two identical runs.

## Lyric themes (DESIGN.md §12)

`themes <themes_graph.db>` lays out the second graph of the project: where each song sits among
the ten lyrical themes. It reads `theme_anchor`, `theme_song` and `theme_score` (written by the
themes stage, `musichistory/themes/`), pins the ten anchors on a ring and writes the song
positions back. Through the pipeline: `.venv\Scripts\python -m musichistory layout -- themes
[options]` (lays out `data/graph/themes_graph.db`).

```powershell
layout\MusicHistory.Layout\bin\Release\net10.0-windows\MusicHistory.Layout.exe themes data\graph\themes_graph.db
MusicHistory.Layout themes data\graph\themes_graph.db --out copy.db --sharpen 3   # try settings on a copy
MusicHistory.Layout themes-demo --out data\graph\themes_demo.db --songs 1012 --from data\graph\music_graph.db
```

### The model

Anchor k (k = 1..10, DESIGN.md §12 order) is pinned at angle `36°·(k−1)` on a ring of radius R in
the x–z plane, `A_k = (R cos θ, 0, R sin θ)` (the themes stage's convention; `theme_anchor.angle` in
degrees). Per song i, per iteration (one GPU thread each, `Shaders/ThemesForceShader.cs`):

```
weights     w_ik = s_ik^γ / Σ_k s_ik^γ                          γ = --sharpen (2); Σ_k w_ik = 1
springs     F += spring · Σ_k w_ik (A_k − p_i)                  zero rest length, to all ten anchors
          = spring · (b_i − p_i),  b_i = Σ_k w_ik A_k            the weighted barycentre
repulsion   F += repulsion · Σ_j d / (|d|² + softening)^{3/2}   d = p_i − p_j, every other song
            (with --cutoff D: minus its value at |d| = D, and 0 beyond D)
plane       y = 0 exactly (default); --slab H: F_y −= spring·flatten·y and |y| ≤ H/2
step        v = damping·v + F / (spring + 2·repulsion·Σ_j (|d|²+softening)^{-3/2}),  |v| ≤ T(it)
cooling     T(it) = startTemperature · (1 − it/iterations)² + minTemperature
```

Without repulsion the equilibrium is exactly the barycentre b_i, so a song whose scores are all
"I love you" sits on that anchor, a 50/50 song halfway along the chord, a flat song at the centre.
The repulsion spreads songs that share (or nearly share) a barycentre into a cloud. Its default is
chosen so the mean displacement from the barycentre stays a few percent of R (measured below).
The step is Jacobi-preconditioned by each song's own stiffness (spring plus a bound of its
repulsion Jacobian): dense clouds stay stable, and no equilibrium moves. Deterministic like the
temporal kernel: ping-pong buffers, fixed summation order, damping, quadratic cooling, and a
seeded start (`b_i` plus an offset of at most `--jitter` in a random direction, from `--seed`) so
identical score vectors do not start on one point, where their repulsion would be 0. Two runs on
one device give bit-identical positions; batching does not change the bits.

| Option | Default | Meaning |
|---|---|---|
| `--iterations` | 1500 | force iterations |
| `--radius` | `themes_meta.ring_radius`, else 40 | ring radius R |
| `--sharpen` | 2 | γ: spring stiffness `score^γ`, normalized per song (higher pulls songs toward their top theme) |
| `--spring` | 1.0 | total spring stiffness per song |
| `--repulsion` | 0.35 | song–song repulsion; 0 puts every song exactly on its barycentre |
| `--softening` | 0.25 | added to d² in the repulsion |
| `--cutoff` | 0 (unlimited) | repulsion range (force shifted to 0 at the cutoff) |
| `--damping` | 0.7 | velocity damping |
| `--startTemperature` / `--minTemperature` | 2 / 0.002 | step limit, cooled quadratically |
| `--jitter` / `--seed` | 0.05 / 42 | seeded start offset around each barycentre |
| `--slab` / `--flatten` | 0 / 2 | 0: songs in the plane y = 0; H > 0: a slab, \|y\| ≤ H/2, vertical spring `spring·flatten` |
| `--batch`, `--device` | 100, default | as for the temporal layout |

Run options: `--out <copy.db>` (copy, then write the copy), `--dry-run`, `--trace N`, `--json`
(one JSON object: params and stats), `--quiet`.

### Input checks (exit code 3, every problem listed)

`theme_anchor` holds exactly the anchor ids 1..10; `theme_song.node_id` is 1..N contiguous;
`theme_score` has exactly one row per (song, anchor) and none for unknown songs or anchors; every
score is a finite number ≥ 0 and every song has a positive sum. Scores that do not sum to 1 are
normalized with a warning. The other exit codes are those of the temporal layout (2 command line,
4 GPU, 5 non-finite positions, nothing written).

### What it writes (one transaction, parameterized SQL, SQLite 3.15, journal_mode=DELETE)

* `theme_anchor.angle` and `position_x/y/z` of the ten anchors (at the radius used);
* `theme_song.position_x/y/z` of every song (y = 0 exactly in the plane mode);
* `themes_meta.ring_radius` = the radius used, so the meta always matches the anchors;
* one `themes_layout_run(run_id, created_at, device, iterations, sharpen, repulsion,
  final_mean_move, params_json)` row per run (created if missing; a table with other columns is
  replaced; readers take the largest run_id). `params_json` holds every parameter plus `stats`:
  ms/iteration, final mean/max move, residual mean/max, and the measurements below.

Measurements (`stats` in params_json; also printed as `check:`): displacement from the barycentre
(mean, `mean_displacement_over_radius`, median, p95, max); `outside_ring` (\|p\| > R: a cloud
centred on an anchor always straddles the ring) and `max_radius`; `crossed_sector` (songs whose
barycentre lies in an anchor's sector, \|b\| ≥ R/2, but whose position ends nearer another anchor);
nearest-neighbour spacing (mean, p5, min); songs with one score ≥ 0.9 (`peaked_songs`) and their
mean/max distance to that anchor; `max_abs_y`; non-finite positions; `positions_sha256`.

### Demo themes graph

`themes-demo --out <db> --songs N` writes a synthetic graph in the exact §12 schema (deterministic
in N, `--seed` and `--from`; with `--generated-at` the file is byte-identical) and lays it out:
songs "Song 0001".. by "Artist 001".., years 1940–2025, node ids by (year, work_id), and score
vectors of four kinds: *peaked* (one theme holds 0.85–0.99, most often "I love you" and "I miss
you"), *other* (peaked on anchor 10), *mixed* (two or three themes share 0.6–0.9) and *flat*.
About one song in ten is title-only (`text_source = 'title'`, scores pulled halfway to uniform;
instrumentals are always title-only); singers are male 60 %, female 27 %, mixed 7 %,
instrumental 3 %, unknown 2 %, nonbinary 1 %. `themes_meta` has `synthetic = 1` and
`backend = synthetic`, and no validation keys (nothing was measured). Playback columns are
placeholders (`../songs/<work_id>/score.mid`, which do not exist) unless `--from
<music_graph.db>` is given: then song k borrows year, `midi_path` (rebased to the output folder),
excerpt, key, tempo and meter of the music graph's node k, so click-to-play works with real MIDI,
while titles, artists, work ids, scores and genders stay synthetic (random themes are never shown
under a real song's name). No lyric text of any kind is involved.

`data/graph/themes_demo.db` is this demo for the viewer: 1012 songs, `--from
data/graph/music_graph.db` (all 1012 MIDI paths resolve), laid out with the defaults.

### Measured (RTX 2080 Ti, default options, 1500 iterations, R = 40)

| Input | ms / iteration | final mean / max move | residual mean / max | displacement from barycentre: mean (% of R) / median / p95 / max | crossed sector | spacing mean / p5 / min | peaked songs: mean / max distance to anchor |
|---|---|---|---|---|---|---|---|
| `themes_demo.db` (1012 songs) | 0.27–0.47 | 8.9e-5 / 1.5e-3 | 2.7e-5 / 4.7e-4 | 1.30 (3.3 %) / 1.23 / 3.18 / 3.88 | 1 of 752 | 1.11 / 0.57 / 0.52 | 331 songs: 1.94 / 3.87 |
| copy of the exported `themes_graph.db` (1012 songs, NLI backend) | 0.20–0.29 | 5.3e-5 / 7.5e-4 | 1.6e-5 / 2.2e-4 | 1.43 (3.6 %) / 0.73 / 4.68 / 5.43 | 2 of 709 | 1.10 / 0.50 / 0.46 | 252 songs: 3.21 / 5.43 |

No NaN, y = 0 for every song, process wall time 1.8–2.3 s including device start-up and SQLite I/O.
In the real graph about 250 songs are near-identical "Other" songs; they form the largest cloud
(radius ≈ 5.4, a fifth of the 24.7 between neighbouring anchors). On that copy: `--repulsion 0`
puts every song within 6e-6 of its barycentre (and songs with identical score vectors on one
point: spacing min 0); `--repulsion 0.7` gives mean displacement 2.02 (5.0 % of R), 5 crossed,
spacing p5 0.65; `--slab 4` gives mean displacement 1.34, spacing p5 0.76, \|y\| ≤ 1.59;
`--sharpen 3` moves more songs toward their top anchor (776 instead of 709 barycentres in a
sector) at mean displacement 1.49. In the tests, a song that is all "I love you", among 150 mixed
songs and a cloud of 60 identical "Other" songs, ends 0.039 from its anchor (the far-field push of
the others), and without repulsion every song of a 400-song demo converges to within 6e-6 of its
barycentre for γ = 1, 2 and 3.5.

## Tests

```powershell
dotnet test layout\MusicHistory.Layout.slnx -c Release
```

57 xunit tests. Temporal (36): the schema against the SQL block of docs/DESIGN.md, key names and shifts, demo
determinism and plausibility (entry/exit keys; seed-42 tree shape unchanged), every input refusal (and its exit code), option parsing, and on
the GPU: two runs bit-identical, settled, exact time axis, NaN-free, lineages clustered; batching
invariance; axis/direction relabelling; output contract (columns, metadata values, journal mode,
file header); edge cases (no edges, one song, edge clearance on, unpinned); WARP; and reading
the result with SQLite 3.15.0 (Unity-FDG's `Assets/Plugins/x86_64/sqlite3.dll`, or
`MUSICHISTORY_SQLITE315`; the test logs SKIPPED when neither exists).

Lyric themes (21, `ThemesTests.cs`): the §12 schema against the SQL block of docs/DESIGN.md; anchors
equally spaced (angle 36°·(k−1), radius R, y = 0, equal chords, zero sum); sharpened weights and
barycentres; demo determinism (byte-identical file) and contract (ids by (year, work_id), scores
≥ 0 summing to 1, top anchor/score, genders, title-only share, peaked/other/mixed songs present);
`--from` playback borrowing and path rebasing; every input refusal (exit 3) and the renormalizing
warning; command-line errors (exit 2); and on the GPU: convergence to the exact barycentre without
repulsion (γ = 1, 2, 3.5; < 1e-4), bit-identical runs (a different `--seed` differs), batching
invariance, default layout quality on 1012 demo songs (mean displacement < 5 % of R, max < 20 %,
≤ 1 % crossed, spacing > 0.3, peaked songs near their anchors, settled, no NaN, y = 0), an
all-one-theme song on its anchor, the output contract (columns, run row, radius written to anchors
and meta, journal mode, file header, `--dry-run`/`--json`), the slab mode, edge cases (one song,
120 identical vectors, a flat vector, `--cutoff`, `--sharpen 12`), WARP, the `themes-demo`
command, and reading the result with SQLite 3.15.0. Fixtures use placeholder titles and numbers
only.

## Files

| File | Role |
|---|---|
| `MusicHistory.Layout/Program.cs` | command line, exit codes |
| `MusicHistory.Layout/LayoutParams.cs` | parameters, defaults, option parsing |
| `MusicHistory.Layout/GraphInput.cs` | reads and validates the §10 graph |
| `MusicHistory.Layout/TemporalGraph.cs` | CSR springs, mass, deterministic start, GPU loop, residual |
| `MusicHistory.Layout/Shaders/TemporalForceShader.cs` | the compute kernel |
| `MusicHistory.Layout/GraphOutput.cs` | positions, node_layout_metadata, layout_run |
| `MusicHistory.Layout/LayoutQuality.cs` | the `check` measurements |
| `MusicHistory.Layout/DemoGraph.cs` | synthetic §10 graphs |
| `MusicHistory.Layout/GraphSchema.cs` | §10 DDL (tested against DESIGN.md) |
| `MusicHistory.Layout/Gpu.cs` | device selection |
| `MusicHistory.Layout/ThemesCommand.cs` | `themes` / `themes-demo` command line |
| `MusicHistory.Layout/ThemesParams.cs` | themes parameters, defaults, option parsing |
| `MusicHistory.Layout/ThemesInput.cs` | reads and validates a §12 themes graph |
| `MusicHistory.Layout/ThemesLayout.cs` | weights, barycentres, seeded start, GPU loop, measurements |
| `MusicHistory.Layout/Shaders/ThemesForceShader.cs` | the themes kernel |
| `MusicHistory.Layout/ThemesOutput.cs` | anchors, song positions, ring_radius, themes_layout_run |
| `MusicHistory.Layout/ThemesDemo.cs` | synthetic §12 themes graphs |
| `MusicHistory.Layout/ThemesSchema.cs` | §12 DDL and the ten themes (tested against DESIGN.md) |

## Credits

* [GPU-FDG](https://github.com/atonalfreerider/GPU-FDG) (MIT, © 2021 john), of which this is a
  new version; see [LICENSE.md](LICENSE.md).
* [ComputeSharp](https://github.com/Sergio0694/ComputeSharp) by Sergio Pedri (MIT): C# compute
  shaders on DirectX 12.
* [Microsoft.Data.Sqlite](https://learn.microsoft.com/dotnet/standard/data/sqlite/) (MIT) and
  SQLite (public domain).
