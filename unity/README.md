# MusicHistory viewer (Unity)

A Unity 6000.6.3f1 URP project that shows the song influence graph (`docs/DESIGN.md` §10–§11)
and walks through it with the music playing. It is a fork of Unity-FDG. The scene and the
reusable pieces (TextBox, CameraControl, the tapered edge mesh and the force-directed simulation)
keep their original script GUIDs.

## Open and run

1. In Unity Hub, choose **Add project from disk** and pick this `unity/` folder. Open it with
   **6000.6.3f1**.
2. Open `Assets/Scenes/SongInfluenceGraph.unity` and press Play.

The loader looks for a graph database in this order:

| Order | Source |
|---|---|
| 1 | `-musicHistoryDb <path>` on the command line (player or editor) |
| 2 | `DbPath` on the **MusicHistory Graph** object (inspector) |
| 3 | `<repo>/data/graph/music_graph.db` (the pipeline's output) |
| 4 | `Assets/StreamingAssets/music_graph.db` (a DB copied next to a player build) |
| 5 | `<repo>/data/graph/demo_graph.db` (synthetic graph from `MusicHistory.Layout demo`) |

MIDI paths in the DB are resolved against the DB's own folder, so `../songs/<work_id>/score.mid`
next to `data/graph/` finds `data/songs/`.

## Controls

| Input | Action |
|---|---|
| Hover | Highlights the song, the songs that influenced it and the songs it influenced. Shows its secondary edges and fills the info panel (title, artist, year, key, BPM, main loop, lineage, and the tree edge: bits and z for a strict-evidence graph, `Shares: <identity>` for identity lineages). |
| Left click | Selects a song: a sticky focus that becomes the walkthrough's target. A click on no bubble but within 7 px (`EdgePickPixels`) of a drawn edge selects the edge: its card replaces the song info, and a family tour plays its identity. Clicking empty space clears both. |
| Right drag · W A S D · Q E · wheel | Look · move · down/up · dolly. Hold Shift to move faster. |
| R / Home | Reset to the overview. |
| L | Label every song (by default only the 40 most influential songs are labelled, plus whatever is in focus). |
| V | Show all secondary edges. |
| F | Toggle the live force-directed simulation. It is axis-locked, so songs never leave their dates. |
| H | Show or hide the controls. |
| 1 / 2 / 3 / 4 · M | Walkthrough mode: **lineage** (root → selected song), **subtree** (depth-first from the selected song, or from its root when it has no children; children in date order), **chronological** (every song from the selected one onward), **family** (identity lineages only: every song of one shared identity in time order, see below). M steps to the next mode (family only when the graph has families). |
| Enter | Start the walkthrough from the selected song (or the selected edge's later song). With nothing selected, it starts from the deepest lineage, the largest tree, the first song, or the largest family, depending on the mode. |
| Space · N / → · B / ← · Esc | Pause/resume · next step · previous step · leave the tour (the free camera comes back). |
| C | Toggle "compare in C / 120 BPM": plays the normalized MIDI without the key/BPM glide. |

Each walkthrough step does four things:

- The camera flies to frame the song and its tree parent.
- The tree edge grows out of the influencer.
- The song's excerpt plays. It starts in the key and BPM of the song heard just before it and glides to its own over `MorphBars` bars (`Morph.Plan` in the Contracts): from the key the previous excerpt ended in to the key this one starts in, and from the tempo the previous excerpt's last bar was actually played at.
- The panel at the bottom shows `Key X → Y · BPM a → b` with the live transposition and tempo, from the plan the player actually plays (`ISongPlayer.CurrentPlan`). In compare mode the target key is the song's home key moved by its `norm_shift` (A minor under relative normalization, C minor under parallel).

"The song heard just before" is the clip the tour was on when the step changed, whichever way it moved: the previous step on a forward advance, the step you left on B/←, the same song on a restart (Enter, C). Only a tour's first song plays natively.

The next step starts when the player raises `Finished`. When the next step plays on the same player, the director does not stop it first, so a song that ended naturally hands off on its bar line. A watchdog advances only when the player makes no progress (its beat stands still, unpaused) for `WatchdogSeconds` (20 s), so an excerpt in a section slower than the song's median tempo is never cut short.

## Identity lineages (DESIGN §8b)

The influence stage can write two kinds of graph. The viewer reads `graph_meta.edge_semantics` to tell them apart:

| `edge_semantics` | Meaning | How the viewer describes an edge |
|---|---|---|
| `identity_lineage` | The two songs share a musical identity: a loop, a chord schema or progression, or an exact passage. It is shared DNA, not proven copying. | `Shares: <identity> · family of N songs`, plus `strong match (z …)` on a strong match. Bits are never shown. |
| `strict_evidence`, or the key is missing (older databases and `--mode evidence`) | Strict v2 borrowing evidence. | `Via <channel> · N bits · z …`, plus the evidence text. Unchanged from before. |

**Families.** For identity lineages the viewer also reads `identity_family` and `song_family` when both tables exist. Each edge's family is the one family both songs belong to whose `label` equals the edge's `evidence`. It must be exactly one, otherwise the edge is reported as a contract problem. On the real graph all 1,713 edges resolve. An edge is a **strong match** when its family has kind `strong`. Without the tables, the viewer falls back to `z > 0`. In the real graph, 66 edges are strong matches, and those are exactly the edges with `z > 0`.

**HUD.** The info panel shows `Shares: <identity>` for the song's tree edge, and says `Shares identities with N earlier · M later` instead of "influenced by". Click an edge to see its **edge card**, which shows:

- the two songs;
- the identity, the family size and the strong-match z;
- the kind of identity (repeating chord loop, named chord schema, chord progression schema, or exact shared passage);
- whether it is a tree edge or a secondary edge;
- the note "a shared musical identity, not proven copying".

The edge lights up, its two songs glow, the rest of its family stays undimmed and everything else dims.

**Legend.** It explains edges as shared musical identities and says they are not proven copying. It lists the identity colours (chord progression orange, loop gold, bass line green, melody blue, melody + harmony white) and highlights strong matches. Strong-match tree edges are drawn in the brightest tier (`EdgeTier.Strong`).

**Family walkthrough (4 or M, then Enter).** The tour plays every member of one family in time order: `song_family` members by node id, which is `(time_value, work_id)` order. The viewer picks the family as follows:

| Selection | Family played |
|---|---|
| An edge | The edge's family |
| A song | The song's strongest family: the highest `song_family.strength` among the families it shares with another song, ties to the smaller family, then the lower id |
| Nothing | The largest family |

A song with no family has nothing to play. The idle panel says which family Enter will play, and why that one.

Each step morphs from the song heard just before, exactly as in the other modes (`WalkthroughDirector.GoTo`). The camera frames the song together with the previous song of the family. The family's edge into the song grows: the one from that previous song if there is one, otherwise the tree edge, otherwise the family's highest-scoring edge. The panel shows `Shares: <identity> · family of N songs`.

Each song plays where the identity sounds in it (`TourPlanner.Window`):

| Window | Used when | Count on the real graph |
|---|---|---|
| Its own excerpt | The excerpt starts on the bar of the identity's first visit (`song_family.first_beat`). The excerpt's entry and exit keys apply. | 727 |
| An edge span | Otherwise, if the influence stage exported a span for an edge of this family at this song. | 250 |
| 8 bars from the bar of the first visit | Neither of the above. | 171 |

The counts are over the 1,148 memberships in families that more than one song shares.

**Limitations.**

- The graph stores no key region for a window outside the song's own excerpt, so for those windows the key handoff uses the home key (the entry and exit columns read as NULL). The panel marks such windows with "home key assumed".
- The graph stores no song length, so an 8-bar first-visit window can run past the end of a short file. The tail is then silent.
- Mono.Data.Sqlite reads `REAL` columns as single precision, so window comparisons allow `TourPlanner.BeatTolerance` (0.001 beat).

## Visual encoding

| Element | Meaning |
|---|---|
| Time | Runs left → right (`TimeAxis` on the loader: `LeftToRight`, `BottomToTop`, or `AsLaidOut`). Decade ticks and year labels sit under the graph, and a faint ring marks each decade. Positions come from the layout stage. When they are NULL (layout has not run), a deterministic fallback pins each song to its date. |
| Bubble | Area ∝ descendants + 1: the layout's `display_radius`, else 0.2·√(descendants+1), never smaller than 4 px on screen. |
| Bubble fill | Key colour. The hue is the key signature on the circle of fifths: C major and A minor share a hue. Minor keys are darker. |
| Bubble ring | Decade colour. |
| Tree edge | Always visible. Tapered, wide at the influencer. Coloured by the channel that carried the influence: chords orange, melody blue, melody + harmony white, bass green, loop gold. Edges leading into big subtrees (trunks) are wider and brighter. Resting edges are drawn with additive light, so bundles of edges read as flows rather than a tangle. |
| Strong match (identity lineages) | A tree edge whose shared identity is an exact passage above the calibrated v2 threshold. Drawn brightest (`EdgeTier.Strong`), in its channel colour. |
| Secondary edge | Hidden until hover (or V). A selected edge's family shows all of its edges. |
| Labels | Keep a constant size on screen, independent of bubble size. Always drawn on top. When two labels would overlap, the lower-priority one is hidden, in this order: focus, then related songs, then the most influential songs. A label that would sit under a HUD panel or be cut by the screen edge is hidden too, unless it is the focus label (`LabelLayer.KeepClear`, fed by `GraphHud.PanelScreenRects`). |

## Data contract (read-only)

The viewer reads `graph_meta`, `nodes`, `song_node` and `influence_edges` exactly as specified in
DESIGN §10. `node_layout_metadata` (`mass`, `display_radius`) and the latest `layout_run`
(`time_axis`, `time_direction`, `year_scale`, `min_time`, `params_json`) are optional. The
`song_node` columns `entry_tonic_pc`, `entry_mode`, `exit_tonic_pc` and `exit_mode` are read
when present (`PRAGMA table_info`); older databases without them read as NULL, which means the
home key. `graph_meta.edge_semantics` and the DESIGN §8b tables `identity_family` and
`song_family` are optional too. A missing key reads as `strict_evidence`, and missing tables as a
graph with no families.

The viewer also relies on these invariants, and reports any that fail as warnings (validation
treats them as failures):

- Node ids run 1..N with no gaps, in order of (time_value, work_id).
- Every non-root node has exactly one tree edge, and it comes from `tree_parent_node`.
- Every edge's source is earlier than its target.
- `descendants`, `tree_depth`, `tree_root_node`, `in_degree` and `out_degree` agree with the edges.
- For identity lineages with family tables: every `song_family` row names an existing song and family, each family's `size` equals its number of rows, `family_count` matches, and each edge's evidence names exactly one family that both of its songs share.

The database must be readable by Unity's bundled SQLite 3.15. That rules out STRICT tables,
generated columns, and window functions or UPSERT inside views or triggers. Use
`journal_mode=DELETE`.

The viewer never reads or shows lyrics. Every string shown from the database is escaped
(`<noparse>`).

## Code map

| Path | Role |
|---|---|
| `Assets/MusicHistory/Viewer/SongGraphLoader.cs` | Scene entry point (GUID of the old `LoadFromSqliteDb`). Resolves the DB path, builds nodes, edges, timeline, HUD, hover and walkthrough, and frames the overview. Handles the F/L/V/H/R keys. |
| `Viewer/SongGraphData.cs` | `SongGraphReader`: Mono.Data.Sqlite reader for §10, MIDI path resolution, invariant checks. It also reads `edge_semantics`, `identity_family`, `song_family` and each edge's family (DESIGN §8b). |
| `Viewer/GraphFrame.cs` | Maps layout positions to the display time axis, fits time_value → coordinate, fallback layout. |
| `Viewer/SongNode.cs`, `InfluenceEdge.cs` | Song bubble (sphere impostor) and tapered edge, with an animatable `VisibleFraction`. |
| `Viewer/GraphMaterials.cs` | Shared material cache: 201 bubble and 10–30 edge materials for the 1000-song demo graph. No renderer owns a material. |
| `Viewer/LabelLayer.cs`, `TextBox.cs` | Constant-screen-size world labels with priority decluttering. |
| `Viewer/TimelineAxis.cs` | Decade axis, ticks, year labels and rings. |
| `Viewer/HoverHighlighter.cs`, `GraphHud.cs` | Hover, song and edge selection (screen-space edge pick) and highlighting. Screen HUD: legend, song info or edge card (`Shares: …` for identity lineages), walkthrough panel. |
| `Viewer/CameraControl.cs`, `SceneLook.cs` | Free-fly camera (`InputEnabled`, `SyncRotationFromTransform`, null-safe input); gradient sky and bloom. |
| `Walkthrough/TourPlanner.cs`, `WalkthroughDirector.cs`, `CameraFraming.cs` | Tour sequences (including the family tour's family choice, order and per-song windows), the step state machine, and perspective-correct framing that keeps the HUD clear. |
| `Playback/SilentSongPlayer.cs` | Timer-based `ISongPlayer`: the same morph tempo maths, raises `Finished`. |
| `Playback/SongPlayerDiscovery.cs` | Chooses the player: an `ISongPlayer` component on the loader object; else `MusicHistory.Audio.SongPlayer`, added by reflection; else the silent player. The silent player also covers songs whose MIDI file is missing. |
| `Assets/FDG/ForceDirectedGraph.cs` | Unity-FDG simulation with an axis lock, a Burst job, persistent buffers and a `Stepped` event. |
| `Assets/Resources/SongBubble.shader`, `GlowingEdge.shader` | Unlit URP shaders with properties in the `UnityPerMaterial` CBUFFER (SRP Batcher compatible). |
| `Assets/MusicHistory/Editor/Validation.cs`, `LineageValidation.cs`, `PlayModeBench.cs` | Headless checks (identity lineages in `LineageValidation.cs`), screenshots and the play-mode benchmark. |
| `Assets/MusicHistory/Contracts/` | Shared with the audio module (foundation, do not edit). |

## Performance (measured)

Measured on 2026-09-29 with the 1000-song demo graph (`data/graph/demo_graph.db`: 2114 edges,
959 of them tree edges). Hardware: RTX 2080 Ti, D3D11, editor play mode run headlessly by
`PlayModeBench`.

| Phase | Player loop, mean (p95) | Full 1920×1080 MSAA×4 render + GPU sync, mean (p95) |
|---|---|---|
| Idle overview | 0.5 ms (0.9) | 5.4 ms (8.6) |
| Hover churn, a new focus every 10 frames | 0.8 ms (2.3) | 5.3 ms (7.9) |
| Running walkthrough | 0.9 ms (2.0) | 5.6 ms (7.9) |
| Live simulation (F) | 10.0 ms (11.8) | 5.4 ms (7.6) |

Other measurements:

- Building the graph from the DB takes 0.7–1.3 s.
- A focus change takes 2.7 ms at the median. It creates labels lazily the first time a song is hovered.
- Garbage collection when idle is about 140 B per frame.
- Materials are shared: 201 for bubbles and 20 for edges, all SRP Batcher compatible.

Measured again on 2026-09-30, after the identity-lineage changes, on the same hardware. Two other
Unity editors were open during these runs. The family tour played the 74-song "I-IV-V-I cadence"
family at time scale 30.

| Phase | Demo graph: player loop, mean (p95) | Demo graph: render + sync | `music_graph.db` (identity lineages): player loop | `music_graph.db`: render + sync |
|---|---|---|---|---|
| Idle overview | 0.58 ms (0.69) | 5.2 ms (6.4) | 0.56 ms (0.65) | 5.6 ms (6.6) |
| Hover churn | 0.81 ms (2.49) | 6.9 ms (9.7) | 0.79 ms (2.22) | 5.7 ms (8.0) |
| Running walkthrough (lineage) | 0.89 ms (2.03) | 5.3 ms (6.9) | 0.80 ms (1.92) | 5.1 ms (6.5) |
| Running family tour | not run (no families) | not run | 0.85 ms (2.07) | 5.2 ms (6.3) |
| Live simulation (F) | 9.46 ms (11.48) | 8.5 ms (10.5) | 8.05 ms (9.42) | 8.5 ms (11.5) |

A focus change took 2.6 ms at the median on the demo graph and 1.9 ms on `music_graph.db`, where
the graph built in 647 ms. An earlier run on `music_graph.db` in the same session was slower
throughout (family tour 2.10 ms (6.42) loop and 11.3 ms (39.9) render; idle render 8.3 ms (20.1)),
so these numbers vary with machine load.

## Headless validation

Run from the repo root. Keep `-batchmode` but leave out `-nographics`, so the camera can render.
Close any editor that has this project open first.

```bash
# Build the fixture graph: 13 real MIDI files, NULL positions.
.venv/Scripts/python.exe tests/unity/graph_fixture.py build
.venv/Scripts/python.exe -m pytest tests/unity -q

UNITY="C:/Program Files/Unity/Hub/Editor/6000.6.3f1/Editor/Unity.exe"
# Edit-mode validation: counts, invariants, hover, tours, the silent player's clock,
# axis lock, screenshots. Writes data/screens/*.png and validation.json.
"$UNITY" -batchmode -projectPath "$PWD/unity" -executeMethod MusicHistory.EditorTools.Validation.Run   -musicHistoryDb "$PWD/data/graph/demo_graph.db" -logFile "$PWD/validate.log"
# Options: -validationDb <db> -validationFixtureDb <db> -validationLineageDb <db> -validationOut <dir> -validationWidth/-validationHeight

# Play-mode benchmark (the scene builds itself through Start). Writes data/screens/playmode_bench.json.
"$UNITY" -batchmode -projectPath "$PWD/unity" -executeMethod MusicHistory.EditorTools.PlayModeBench.Run   -musicHistoryDb "$PWD/data/graph/demo_graph.db" -logFile "$PWD/bench.log"
```

Relative `-musicHistoryDb` paths are tried against Unity's working directory (the project
folder), then against the repository root.

The identity-lineage checks (`Editor/LineageValidation.cs`) run on `data/graph/music_graph.db`
when its `edge_semantics` is `identity_lineage`; `-validationLineageDb <db>` picks another database.
They check the following:

- The semantics key and the family tables are read in full.
- Every edge's family is resolved, and matches an independent SQL join.
- Strong matches are exactly the edges with `z > 0`.
- The HUD, the edge card and the legend wording hold for both kinds of graph: `Shares:` and never
  bits for identity lineages, bits and z for strict evidence. The strict-evidence case is also
  checked on a copy stripped of `edge_semantics` and the family tables, which stands in for an
  older database.
- The family tour order equals the time order of `song_family` for every family (SQL).
- The strongest family agrees with SQL for every song.
- Every family window is bar aligned and contains the identity's first visit.
- The family tour morphs from the song heard before, on Next, B/← and a natural advance.
- No drawn label lies under a HUD panel.

They write `lineage_overview.png`, `lineage_walkthrough.png`, `lineage_edge_card.png` and
`family_tour.png`. When the graph has families, `PlayModeBench` adds a family-tour phase with the
real synth and writes `playmode_family_tour.png`.

Both commands exit with code 0 only when every check passes. In the editor, the same checks
run from **MusicHistory → Run Validation**.

## Lyric themes

`Assets/Scenes/LyricThemes.unity` shows the second graph from DESIGN §12: where each song sits
among ten lyrical themes. Open the scene and press Play. It is in the build settings after
`SongInfluenceGraph`.

The viewer (`Assets/MusicHistory/Themes/ThemesViewer.cs`) looks for a themes database in this order:
`-themesDb <path>` on the command line, then `DbPath` on the **Lyric Themes** object, then
`<repo>/data/graph/themes_graph.db`, then `StreamingAssets/themes_graph.db`, then
`<repo>/data/graph/themes_demo.db`. If the layout stage has not written positions yet
(`position_x` is NULL), the viewer places the songs itself and says so in the legend. It puts each
song at the barycentre of the anchors weighted by score², then pushes overlapping songs apart.
The result is deterministic.

| Element | Meaning |
|---|---|
| Gold pads on the ring | The ten themes, 36° apart. Each label shows the theme text, its filter key and how many songs have it as their top theme. |
| Bubble position | The layout position: where the pulls of the song's theme scores balance. A song that is all one theme sits on that theme. |
| Bubble colour | Blue for a male singer, pink for a female singer, grey for mixed, nonbinary, unknown or instrumental. |
| Filled / hollow bubble | Classified from the lyrics / from the title only. |
| Faint ring, spokes, half-radius circle | Guides on the ring plane. |

| Input | Action |
|---|---|
| Hover | Shows a card with the title, artist, year, singer, "lyrics" or "title only", and the top three themes with bars and scores. Gold tethers link the song to those themes. |
| Left click | Plays the song's excerpt in its own key and BPM, with no morph. Click it again, or press Space, to stop. |
| Left or middle drag · right drag · wheel | Pan · orbit · zoom toward the pointer. |
| W A S D / arrows · Q E · Z X | Pan · rotate · zoom. Hold Shift to go faster. |
| 1–9, 0 | Show only the songs whose top theme is that theme. Press the key again, or Esc, to show all. |
| L | Label every song (labels that would overlap are hidden). |
| R / Home · H | Reset the view · show the controls. |
| G / Backspace / the top-right button | Back to `SongInfluenceGraph`. |

The themes database holds no lyrics, and the viewer never shows any. Validation checks every
song's hover card: it may contain only the title, artist, year, singer, text source, theme labels
and scores.

```bash
# Edit-mode checks against themes_demo.db and themes_graph.db (-themesValidationDb <db> for one).
# Writes data/screens/themes_overview.png (the DB the scene opens by default), per-DB
# themes_{overview,hover,filter,all_labels}_<db>.png and themes_validation.json.
"$UNITY" -batchmode -projectPath "$PWD/unity" -executeMethod MusicHistory.EditorTools.ThemesValidation.Run -logFile "$PWD/themes_validate.log"
# Play mode: frame times, a click through the real SongPlayer, and the switch back to the influence graph.
"$UNITY" -batchmode -projectPath "$PWD/unity" -executeMethod MusicHistory.EditorTools.ThemesPlayModeBench.Run -logFile "$PWD/themes_bench.log"
# Rebuild the scene file (and its build-settings entry) from code.
"$UNITY" -batchmode -projectPath "$PWD/unity" -executeMethod MusicHistory.EditorTools.ThemesValidation.CreateScene -logFile "$PWD/themes_scene.log"
```

In the editor, the same checks run from **MusicHistory → Lyric Themes**.

Measured on 2026-09-30 with the RTX 2080 Ti on D3D11, at 1920×1080 with MSAA ×4. The scene
had 1,012 songs from `themes_graph.db` (fallback positions). With the scene idle, the player loop
took 0.54 ms on average (0.76 ms at p95), and a render plus GPU sync took 3.8 ms (5.8 ms at p95).
Moving the hover to a new song took 0.79 ms at the median; that includes picking and all bubble
states, with 24 or fewer shared bubble materials. Building the scene took about 0.9 s.
