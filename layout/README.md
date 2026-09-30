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

## Tests

```powershell
dotnet test layout\MusicHistory.Layout.slnx -c Release
```

36 xunit tests: the schema against the SQL block of docs/DESIGN.md, key names and shifts, demo
determinism and plausibility (entry/exit keys; seed-42 tree shape unchanged), every input refusal (and its exit code), option parsing, and on
the GPU: two runs bit-identical, settled, exact time axis, NaN-free, lineages clustered; batching
invariance; axis/direction relabelling; output contract (columns, metadata values, journal mode,
file header); edge cases (no edges, one song, edge clearance on, unpinned); WARP; and reading
the result with SQLite 3.15.0 (Unity-FDG's `Assets/Plugins/x86_64/sqlite3.dll`, or
`MUSICHISTORY_SQLITE315`; the test logs SKIPPED when neither exists).

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

## Credits

* [GPU-FDG](https://github.com/atonalfreerider/GPU-FDG) (MIT, © 2021 john), of which this is a
  new version; see [LICENSE.md](LICENSE.md).
* [ComputeSharp](https://github.com/Sergio0694/ComputeSharp) by Sergio Pedri (MIT): C# compute
  shaders on DirectX 12.
* [Microsoft.Data.Sqlite](https://learn.microsoft.com/dotnet/standard/data/sqlite/) (MIT) and
  SQLite (public domain).
