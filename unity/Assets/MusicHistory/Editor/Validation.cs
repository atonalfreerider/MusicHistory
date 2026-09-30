#nullable enable
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using FDG;
using Mono.Data.Sqlite;
using MusicHistory.Playback;
using MusicHistory.Viewer;
using MusicHistory.Walkthrough;
using Unity.Profiling;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using Debug = UnityEngine.Debug;
using Object = UnityEngine.Object;

namespace MusicHistory.EditorTools
{
    /// <summary>
    /// Headless validation of the viewer, for
    /// <c>Unity.exe -batchmode -projectPath unity -executeMethod MusicHistory.EditorTools.Validation.Run -logFile ...</c>
    /// (without -nographics, so the camera can render; without -quit, the method exits itself).
    ///
    /// Opens the scene, builds the graph in edit mode from a real graph DB (default
    /// data/graph/demo_graph.db, else music_graph.db) and from the fixture DB (NULL positions,
    /// real MIDI files), checks counts and §10 invariants, hover, tours, the silent player's morph
    /// clock and the axis-locked live simulation, renders PNGs to data/screens/ and writes
    /// data/screens/validation.json. Exit code 0 = every check passed.
    ///
    /// Options: -validationDb &lt;path&gt; -validationFixtureDb &lt;path&gt; -validationOut &lt;dir&gt;
    /// -validationWidth 1920 -validationHeight 1080.
    /// </summary>
    public static partial class Validation
    {
        const string ScenePath = "Assets/Scenes/SongInfluenceGraph.unity";

        sealed class Report
        {
            public readonly List<(string name, bool ok, string detail)> Checks = new();
            public readonly SortedDictionary<string, string> Numbers = new(StringComparer.Ordinal);
            public int Failures => Checks.Count(c => !c.ok);

            public bool Check(string name, bool ok, string detail = "")
            {
                Checks.Add((name, ok, detail));
                Debug.Log($"[validation] {(ok ? "PASS" : "FAIL")} {name}{(detail.Length > 0 ? " — " + detail : "")}");
                return ok;
            }

            public void Number(string name, double value, string format = "0.###")
            {
                Numbers[name] = value.ToString(format, CultureInfo.InvariantCulture);
                Debug.Log($"[validation] {name} = {Numbers[name]}");
            }

            public void Text(string name, string value)
            {
                Numbers[name] = value;
                Debug.Log($"[validation] {name} = {value}");
            }
        }

        [MenuItem("MusicHistory/Run Validation (writes data/screens)")]
        public static void RunFromMenu() => Execute(exitWhenDone: false);

        public static void Run() => Execute(exitWhenDone: true);

        static void Execute(bool exitWhenDone)
        {
            Report report = new();
            string outDir = Arg("-validationOut") ?? Path.Combine(SongGraphLoader.RepoRoot(), "data", "screens");
            Directory.CreateDirectory(outDir);
            try
            {
                RunAll(report, outDir);
            }
            catch (Exception e)
            {
                Debug.LogException(e);
                report.Check("no exception", false, e.GetType().Name + ": " + e.Message);
            }
            WriteJson(report, Path.Combine(outDir, "validation.json"));
            Debug.Log($"[validation] {report.Checks.Count - report.Failures}/{report.Checks.Count} checks passed; report in {outDir}");
            if (exitWhenDone) EditorApplication.Exit(report.Failures == 0 ? 0 : 1);
        }

        static void RunAll(Report report, string outDir)
        {
            string repo = SongGraphLoader.RepoRoot();
            string graphDir = Path.Combine(repo, "data", "graph");
            string dbPath = PathArg("-validationDb") ?? FirstExisting(
                Path.Combine(graphDir, "demo_graph.db"), Path.Combine(graphDir, "music_graph.db")) ?? "";
            string fixturePath = PathArg("-validationFixtureDb") ??
                                 Path.Combine(repo, "data", "fixtures", "unity", "graph", "fixture_graph.db");
            int width = int.TryParse(Arg("-validationWidth"), out int w) ? w : 1920;
            int height = int.TryParse(Arg("-validationHeight"), out int h) ? h : 1080;
            report.Text("unity_version", Application.unityVersion);
            report.Text("graphics_device", SystemInfo.graphicsDeviceName + " / " + SystemInfo.graphicsDeviceType);
            report.Text("db", dbPath);

            EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
            SongGraphLoader? loader = Object.FindAnyObjectByType<SongGraphLoader>();
            if (!report.Check("scene has SongGraphLoader", loader != null)) return;
            report.Check("scene loader has ForceDirectedGraph", loader!.GetComponent<ForceDirectedGraph>() != null);
            report.Check("scene is in build settings", EditorBuildSettings.scenes.Any(s => s.path == ScenePath && s.enabled));
            Camera? cam = Camera.main;
            if (!report.Check("scene has a MainCamera with CameraControl", cam != null && cam.GetComponent<CameraControl>() != null)) return;
            cam!.aspect = (float)width / height;

            string resolved = SongGraphLoader.ResolveDatabasePath("", out string reason);
            string? cliDb = PathArg(SongGraphLoader.CommandLineFlag);
            if (cliDb != null)
                report.Check("-musicHistoryDb overrides the default path",
                    PathsEqual(resolved, cliDb) && reason == "command line", $"{resolved} ({reason})");
            else
                report.Text("default_db_resolution", $"{resolved} ({reason})");

            if (!report.Check("graph database exists", File.Exists(dbPath), dbPath)) return;
            ValidateMainGraph(report, loader, cam, dbPath, outDir, width, height);
            ValidateLineage(report, loader, cam, outDir, width, height);

            if (File.Exists(fixturePath)) ValidateFixture(report, loader, cam, fixturePath, outDir, width, height);
            else report.Check("fixture database exists (python tests/unity/graph_fixture.py build)", false, fixturePath);

            ValidateSilentPlayer(report);
            loader.Clear();
        }

        // ------------------------------------------------------------------ main graph
        static void ValidateMainGraph(Report report, SongGraphLoader loader, Camera cam, string dbPath, string outDir, int width, int height)
        {
            Stopwatch clock = Stopwatch.StartNew();
            loader.Build(dbPath);
            clock.Stop();
            report.Number("build_ms", clock.Elapsed.TotalMilliseconds, "0");
            SongGraphData data = loader.Data!;
            int n = data.Songs.Count;
            report.Number("songs", n, "0");
            report.Number("edges", data.Edges.Count, "0");
            report.Number("tree_edges", data.TreeEdgeCount, "0");
            report.Number("roots", data.RootCount, "0");
            report.Check("§10 invariants hold", data.Problems.Count == 0, string.Join(" | ", data.Problems.Take(5)));
            ValidateRegionKeys(report, loader, dbPath, "main graph");
            report.Check("one SongNode per song", loader.Nodes.Count == n && Object.FindObjectsByType<SongNode>().Length == n);
            report.Check("song_count matches graph_meta", data.MetaInt("song_count") is not int sc || sc == n, $"meta {data.MetaInt("song_count")}");
            report.Check("edge_count matches graph_meta", data.MetaInt("edge_count") is not int ec || ec == data.Edges.Count);
            report.Check("one InfluenceEdge per edge row", loader.Edges.Count == data.Edges.Count);
            report.Check("tree edges = songs - roots", data.TreeEdgeCount == n - data.RootCount);
            report.Check("every tree edge visible", loader.Edges.Where(e => e.IsTree).All(e => e.Shown && e.Renderer.enabled));
            report.Check("secondary edges hidden until hover", loader.Edges.Where(e => !e.IsTree).All(e => !e.Shown));
            report.Check("tree edge runs parent → child", loader.Nodes.All(node =>
                node.Song.TreeParent == null ? node.TreeEdge == null
                    : node.TreeEdge != null && node.TreeEdge.Source == node.TreeParent && node.TreeEdge.Target == node));

            GraphFrame frame = loader.Frame;
            report.Text("layout", frame.Description);
            report.Number("time_fit_units_per_year", frame.Slope, "0.0000");
            report.Number("time_fit_max_residual_years", frame.MaxResidualYears, "0.00000");
            report.Check("time runs left → right", Vector3.Dot(frame.TimeDir, Vector3.right) > .999f);
            report.Check("songs sit on their dates (residual < 0.05 y)", frame.MaxResidualYears < .05);
            bool monotone = true;
            for (int i = 1; i < n; i++)
                if (loader.Nodes[i].transform.position.x < loader.Nodes[i - 1].transform.position.x - 1e-3f) monotone = false;
            report.Check("x increases with node id (= date order)", monotone);

            // Area ∝ descendants + 1 for every unclamped bubble.
            List<double> ratios = new();
            foreach (SongNode node in loader.Nodes)
            {
                if (node.Radius <= loader.MinBubbleRadius + 1e-4f || node.Radius >= loader.MaxBubbleRadius - 1e-4f) continue;
                ratios.Add(node.Radius * node.Radius / (node.Song.Descendants + 1.0));
            }
            if (ratios.Count > 1)
            {
                double spread = ratios.Max() / ratios.Min();
                report.Check("bubble area ∝ descendants + 1", spread < 1.02, $"{ratios.Count} unclamped bubbles, max/min area ratio {spread:0.0000}");
            }
            SongNode biggest = loader.Nodes.OrderByDescending(x => x.Song.Descendants).First();
            report.Number("largest_bubble_radius", biggest.Radius, "0.00");

            int expectedLabels = Math.Min(loader.AlwaysLabelledSongs, n);
            report.Check("top songs are always labelled", loader.PinnedLabelNodes.Count == expectedLabels &&
                loader.PinnedLabelNodes.All(p => p.Label != null && p.Label.Visible), $"{loader.PinnedLabelNodes.Count} pinned");
            int minPinnedDesc = loader.PinnedLabelNodes.Count > 0 ? loader.PinnedLabelNodes.Min(p => p.Song.Descendants) : 0;
            report.Check("pinned labels are the most influential", loader.Nodes.Where(x => !x.LabelPinned).All(x => x.Song.Descendants <= minPinnedDesc));

            // Shared materials only.
            report.Check("bubbles use shared MusicHistory/SongBubble materials", loader.Nodes.All(x =>
                GraphMaterials.IsShared(x.BubbleRenderer.sharedMaterial) && x.BubbleRenderer.sharedMaterial.shader.name == GraphMaterials.BubbleShaderName));
            report.Check("edges use shared MusicHistory/GlowingEdge materials", loader.Edges.All(e =>
                GraphMaterials.IsShared(e.Renderer.sharedMaterial) && e.Renderer.sharedMaterial.shader.name == GraphMaterials.EdgeShaderName));
            report.Number("bubble_materials", GraphMaterials.BubbleMaterialCount, "0");
            report.Number("edge_materials", GraphMaterials.EdgeMaterialCount, "0");

            // Timeline.
            TimelineAxis axis = loader.Timeline;
            report.Check("timeline covers every song", axis.FirstYear <= data.MinTime && axis.LastYear >= data.MaxTime,
                $"{axis.FirstYear}–{axis.LastYear}, songs {data.MinTime:0.0}–{data.MaxTime:0.0}");
            bool ticksAligned = axis.DecadeYears.All(y => Mathf.Abs(Vector3.Dot(axis.AxisPoint(y), frame.TimeDir) - frame.TimeCoord(y)) < 1e-3f);
            report.Check("decade ticks sit at their years", ticksAligned, $"{axis.DecadeYears.Count} decades");
            SongNode first = loader.Nodes[0];
            report.Check("first song lies between its decade ticks",
                first.transform.position.x >= frame.TimeCoord(axis.FirstYear) - 1e-3f &&
                first.transform.position.x <= frame.TimeCoord(axis.FirstYear + 10) + 1e-3f);

            // Overview render + timing.
            loader.Highlighter.ApplyFocus(null, force: true);
            loader.FrameOverview(cam);
            string overview = Path.Combine(outDir, "overview.png");
            double renderMs = Capture(cam, loader, overview, width, height, out RenderStats stats);
            report.Number("overview_render_ms_first", renderMs, "0.0");
            List<double> times = new();
            for (int i = 0; i < 10; i++)
            {
                times.Add(Capture(cam, loader, null, width, height, out stats));
            }
            report.Number("overview_render_ms_mean_of_10", times.Average(), "0.00");
            report.Number("overview_render_ms_max_of_10", times.Max(), "0.00");
            if (stats.DrawCalls > 0) report.Number("overview_draw_calls", stats.DrawCalls, "0");
            if (stats.SetPass > 0) report.Number("overview_setpass_calls", stats.SetPass, "0");
            if (stats.Batches > 0) report.Number("overview_batches", stats.Batches, "0");
            report.Check("overview.png written", File.Exists(overview) && new FileInfo(overview).Length > 10000, overview);
            CheckLabels(report, loader, "overview");

            ValidateHover(report, loader, cam, outDir, width, height);
            ValidateTours(report, loader, cam, outDir, width, height);
            ValidateSimulation(report, loader);
        }

        static void ValidateHover(Report report, SongGraphLoader loader, Camera cam, string outDir, int width, int height)
        {
            // A mid-sized song, so both influencers and influenced exist.
            SongNode focus = loader.Nodes
                .Where(x => x.Incoming.Count > 0 && x.Outgoing.Count > 0)
                .OrderByDescending(x => x.Song.Descendants).ThenBy(x => x.NodeId)
                .FirstOrDefault() ?? loader.Nodes.OrderByDescending(x => x.Song.Descendants).First();
            loader.Highlighter.ApplyFocus(focus, force: true);
            HashSet<SongNode> related = new(focus.Influencers().Concat(focus.Influenced()));
            report.Check("hover: focus glows", focus.State == BubbleState.Focus);
            report.Check("hover: influencers and influenced glow", related.All(x => x.State == BubbleState.Related),
                $"{related.Count} related songs");
            report.Check("hover: every other song dims",
                loader.Nodes.Where(x => x != focus && !related.Contains(x)).All(x => x.State == BubbleState.Dimmed));
            report.Check("hover: the song's secondary edges appear", loader.Edges
                .Where(e => !e.IsTree).All(e => e.Shown == (e.Source == focus || e.Target == focus)));
            report.Check("hover: HUD shows title, key, BPM and lineage", loader.Hud.InfoText.Contains(focus.Song.Title) &&
                loader.Hud.InfoText.Contains("Key") && loader.Hud.InfoText.Contains("BPM") && loader.Hud.InfoText.Contains("Lineage"));
            CheckHudSemantics(report, loader, focus, "hover");
            // Frame the focus with its neighbourhood for the screenshot.
            List<(Vector3, float)> items = new() { (focus.transform.position, focus.Radius) };
            items.AddRange(related.Select(x => (x.transform.position, x.Radius)));
            (Vector3 pos, Quaternion rot) = CameraFraming.Frame(cam, items, loader.Frame.ViewForward, 1.1f, 6f, new Rect(.03f, .1f, .6f, .62f));
            cam.transform.SetPositionAndRotation(pos, rot);
            string path = Path.Combine(outDir, "hover.png");
            Capture(cam, loader, path, width, height, out _);
            report.Check("hover.png written", File.Exists(path), $"focus node {focus.NodeId} '{focus.Song.Title}'");
            CheckLabels(report, loader, "hover");
            report.Check("hover: the focus label is drawn", focus.Label != null && focus.Label.Visible && focus.Label.Placed);
            loader.Highlighter.ApplyFocus(null, force: true);
            report.Check("hover cleared: secondary edges hidden again", loader.Edges.Where(e => !e.IsTree).All(e => !e.Shown));
        }

        static void ValidateTours(Report report, SongGraphLoader loader, Camera cam, string outDir, int width, int height)
        {
            SongGraphData data = loader.Data!;
            WalkthroughDirector director = loader.Director;
            report.Text("player", director.PlayerDescription);
            Type? audioType = SongPlayerDiscovery.FindAudioPlayerType();
            report.Text("audio_player_type_present", audioType != null ? audioType.FullName ?? "yes" : "no");
            if (audioType != null)
                report.Check("discovery picks MusicHistory.Audio.SongPlayer when present",
                    director.Player != null && director.Player.GetType() == audioType);
            else
                report.Check("discovery falls back to SilentSongPlayer", director.Player is SilentSongPlayer);

            // Lineage from the deepest song.
            int deepest = TourPlanner.DefaultStart(data, TourMode.Lineage);
            List<int> lineage = TourPlanner.Plan(data, TourMode.Lineage, deepest);
            bool chainOk = lineage.Count == data.Song(deepest).TreeDepth + 1 && data.Song(lineage[0]).IsRoot && lineage[^1] == deepest;
            for (int i = 1; i < lineage.Count; i++) chainOk &= data.Song(lineage[i]).TreeParent == lineage[i - 1];
            report.Check("lineage tour: root → … → target along tree parents", chainOk, $"{lineage.Count} steps to node {deepest}");

            // Subtree from the largest root: preorder, parents before children, children by date.
            int root = TourPlanner.DefaultStart(data, TourMode.Subtree);
            List<int> subtree = TourPlanner.Plan(data, TourMode.Subtree, root);
            HashSet<int> seen = new();
            bool preorder = true;
            foreach (int id in subtree)
            {
                if (id != root && !(data.Song(id).TreeParent is int p && seen.Contains(p))) preorder = false;
                seen.Add(id);
            }
            report.Check("subtree tour covers the tree in preorder", preorder && subtree.Count == data.Song(root).Descendants + 1,
                $"{subtree.Count} songs from root {root}");
            List<int> chrono = TourPlanner.Plan(data, TourMode.Chronological, Math.Max(1, data.Songs.Count / 2));
            bool sorted = chrono.Zip(chrono.Skip(1), (a, b) => data.Song(a).TimeValue <= data.Song(b).TimeValue).All(x => x);
            report.Check("chronological tour is in date order", sorted && chrono.Count == data.Songs.Count - Math.Max(1, data.Songs.Count / 2) + 1);

            // Drive a lineage tour in edit mode.
            SongNode target = loader.NodeById(deepest);
            loader.Highlighter.Select(target);
            report.Check("tour starts", director.StartTour(TourMode.Lineage, target));
            report.Check("tour: camera input disabled", !cam.GetComponent<CameraControl>().InputEnabled);
            report.Check("tour: step 1 plays natively", director.CurrentPlan.MorphBeats == 0 && director.PreviousClip == null);
            if (director.Steps.Count > 1)
            {
                director.Next();
                SongClip clip = director.CurrentClip!;
                SongClip prev = director.PreviousClip!;
                // The HUD shows the plan the player actually plays; the silent clock's is median-based.
                MorphPlan playing = director.ActivePlayer!.CurrentPlan;
                MorphPlan expected = director.ActivePlayer is SilentSongPlayer ? Morph.Plan(prev, clip, director.MorphBars) : playing;
                report.Check("tour: step 2 morphs from the previous song (the player's own plan)",
                    prev != null && prev.NodeId == director.Steps[0] &&
                    SamePlan(director.CurrentPlan, playing) && SamePlan(playing, expected) && director.CurrentPlan.MorphBeats > 0,
                    $"start {expected.StartSemitones:+0;-0;0} st, tempo ×{expected.StartTempoRatio:0.000}, {expected.MorphBeats} beats");
                InfluenceEdge? edge = loader.NodeById(clip.NodeId).TreeEdge;
                report.Check("tour: tree edge starts collapsed", edge != null && edge.VisibleFraction < .01f);
                for (int i = 0; i < 12; i++) TickDirector(director, .1f);
                report.Check("tour: camera flight in progress", director.Flying);
                string mid = Path.Combine(outDir, "walkthrough_flight.png");
                Capture(cam, loader, mid, width, height, out _);
                for (int i = 0; i < 30; i++) TickDirector(director, .1f);
                report.Check("tour: flight finished and edge fully grown", !director.Flying && edge != null && edge.VisibleFraction > .999f);
                SongNode child = loader.NodeById(clip.NodeId);
                Vector3 viewport = cam.WorldToViewportPoint(child.transform.position);
                report.Check("tour: the song is framed on screen", viewport.z > 0 && viewport.x > 0 && viewport.x < 1 && viewport.y > 0 && viewport.y < 1,
                    $"viewport {viewport.x:0.00},{viewport.y:0.00}");
                if (child.TreeParent != null)
                {
                    Vector3 pv = cam.WorldToViewportPoint(child.TreeParent.transform.position);
                    report.Check("tour: the parent is framed on screen", pv.z > 0 && pv.x > 0 && pv.x < 1 && pv.y > 0 && pv.y < 1);
                }
                report.Check("tour: only the step's songs are labelled", loader.Labels.Labels.Where(l => l.Placed && l.Visible)
                    .All(l => l.Priority >= 1e7f), "dimmed labels hidden during tours");
                report.Check("tour: HUD shows 'Key X → Y · BPM a → b'", director.HudText.Contains("Key ") && director.HudText.Contains(" → ") &&
                    director.HudText.Contains("BPM"), FirstLines(director.HudText, 3));
                double startBpm = director.ActivePlayer is IMorphReadout readout ? readout.PlanStartBpm : clip.NativeBpm * playing.StartTempoRatio;
                string startBpmText = $"BPM {startBpm.ToString("0.#", CultureInfo.InvariantCulture)} → ";
                report.Check("tour: HUD start BPM is the player's plan start", director.HudText.Contains(startBpmText), startBpmText);
                string walk = Path.Combine(outDir, "walkthrough.png");
                Capture(cam, loader, walk, width, height, out _);
                report.Check("walkthrough.png written", File.Exists(walk));

                // Let the silent clock run the excerpt out; the director must advance on Finished.
                if (director.ActivePlayer is SilentSongPlayer silent)
                {
                    int before = director.StepIndex;
                    double seconds = 0;
                    while (silent.IsPlaying && seconds < 600)
                    {
                        silent.Advance(.05);
                        seconds += .05;
                    }
                    TickDirector(director, .05f);
                    bool advanced = director.StepIndex == before + 1 || (before == director.Steps.Count - 1 && director.TourComplete);
                    report.Check("tour: advances when the excerpt finishes", advanced,
                        $"excerpt took {seconds:0.0} s of simulated time");
                }
                director.TogglePause();
                bool paused = director.ActivePlayer != null && director.ActivePlayer.Paused;
                director.TogglePause();
                report.Check("tour: Space pauses and resumes", paused && director.ActivePlayer != null && !director.ActivePlayer.Paused);

                // Apples to apples: the target key comes from the song's norm_shift (graph_meta normalization).
                director.ApplesToApples = true;
                director.GoTo(director.StepIndex);
                string normalized = WalkthroughDirector.NormalizedKeyName(director.CurrentClip!);
                report.Check("tour: apples-to-apples HUD names the normalized key from norm_shift, no morph",
                    director.HudText.Contains($"→ {normalized} (normalized)") && director.CurrentPlan.MorphBeats == 0,
                    $"{director.CurrentClip!.TonicPc}/{(director.CurrentClip.Minor ? "minor" : "major")} shift {director.CurrentClip.NormShift:+0;-0;0} → {normalized}");
                director.ApplesToApples = false;
                director.GoTo(director.StepIndex);

                int leaving = director.CurrentClip!.NodeId, stepBefore = director.StepIndex;
                director.Previous();
                report.Check("tour: previous step morphs from the song just heard, not from steps[i-1]",
                    director.StepIndex == Math.Max(0, stepBefore - 1) && director.PreviousClip != null && director.PreviousClip.NodeId == leaving,
                    $"back to step {director.StepIndex + 1}, previous clip node {director.PreviousClip?.NodeId} (left node {leaving})");
                int current = director.CurrentClip!.NodeId;
                director.GoTo(director.StepIndex);
                report.Check("tour: a restart morphs from the same song", director.PreviousClip != null && director.PreviousClip.NodeId == current);
            }
            director.Exit();
            report.Check("tour: Esc restores free camera and hover", !director.IsTouring && cam.GetComponent<CameraControl>().InputEnabled &&
                !loader.Highlighter.Suspended && loader.Edges.Where(e => e.IsTree).All(e => e.VisibleFraction > .999f));
            loader.Highlighter.Select(null);
        }

        static bool SamePlan(MorphPlan a, MorphPlan b) =>
            Math.Abs(a.StartSemitones - b.StartSemitones) < 1e-9 && Math.Abs(a.StartTempoRatio - b.StartTempoRatio) < 1e-9 &&
            Math.Abs(a.MorphBeats - b.MorphBeats) < 1e-9;

        /// <summary>
        /// song_node entry_* / exit_* (DESIGN.md §10): read when the columns exist, NULL (home key)
        /// on older databases, carried into clips, and used by the key handoff.
        /// </summary>
        static void ValidateRegionKeys(Report report, SongGraphLoader loader, string dbPath, string label)
        {
            SongGraphData data = loader.Data!;
            using SqliteConnection conn = new("URI=file:" + Path.GetFullPath(dbPath));
            conn.Open();
            HashSet<string> columns = SongGraphReader.TableColumns(conn, "song_node");
            bool present = SongGraphReader.OptionalSongColumns.All(columns.Contains);
            int readEntry = data.Songs.Count(x => x.EntryTonicPc != null), readExit = data.Songs.Count(x => x.ExitTonicPc != null);
            int readEntryMinor = data.Songs.Count(x => x.EntryMinor == true), readExitMinor = data.Songs.Count(x => x.ExitMinor == true);
            report.Number($"{Slug(label)}_excerpts_entering_outside_home_key", readEntry, "0");
            report.Number($"{Slug(label)}_excerpts_leaving_outside_home_key", readExit, "0");
            if (present)
            {
                int dbEntry = Count(conn, "SELECT COUNT(*) FROM song_node WHERE entry_tonic_pc IS NOT NULL");
                int dbExit = Count(conn, "SELECT COUNT(*) FROM song_node WHERE exit_tonic_pc IS NOT NULL");
                int dbEntryMinor = Count(conn, "SELECT COUNT(*) FROM song_node WHERE entry_mode = 'minor'");
                int dbExitMinor = Count(conn, "SELECT COUNT(*) FROM song_node WHERE exit_mode = 'minor'");
                report.Check($"{label}: entry/exit keys read from song_node", readEntry == dbEntry && readExit == dbExit &&
                    readEntryMinor == dbEntryMinor && readExitMinor == dbExitMinor,
                    $"entry {readEntry}/{dbEntry} ({readEntryMinor}/{dbEntryMinor} minor), exit {readExit}/{dbExit} ({readExitMinor}/{dbExitMinor} minor)");
            }
            else
            {
                report.Check($"{label}: song_node without entry/exit columns (older DB) reads them as NULL = home key",
                    readEntry == 0 && readExit == 0 && data.Songs.All(x => x.EntryMinor == null && x.ExitMinor == null) && data.Problems.Count == 0,
                    $"columns present: {string.Join(",", SongGraphReader.OptionalSongColumns.Where(columns.Contains))}");
            }
            report.Check($"{label}: clips carry the excerpt's entry/exit keys", data.Songs.All(x =>
            {
                SongClip c = x.ToClip();
                return c.EntryTonicPc == x.EntryTonicPc && c.EntryMinor == x.EntryMinor && c.ExitTonicPc == x.ExitTonicPc && c.ExitMinor == x.ExitMinor &&
                       c.EntryTonic == (x.EntryTonicPc ?? x.TonicPc) && c.ExitTonic == (x.ExitTonicPc ?? x.TonicPc);
            }));
            // A tree edge whose handoff differs when region keys are used: the player must use them.
            SongRecord? child = data.Songs.FirstOrDefault(x => x.TreeParent is int pid &&
                Morph.Wrap((data.Song(pid).ExitTonicPc ?? data.Song(pid).TonicPc) - (x.EntryTonicPc ?? x.TonicPc)) != Morph.Wrap(data.Song(pid).TonicPc - x.TonicPc));
            if (child == null)
            {
                if (present && (readEntry > 0 || readExit > 0)) report.Text($"{Slug(label)}_region_key_handoff", "no tree edge where the region keys change the handoff");
                return;
            }
            SongRecord parentSong = data.Song(child.TreeParent!.Value);
            SilentSongPlayer silent = loader.Director.Silent;
            bool apples = silent.ApplesToApples;
            silent.ApplesToApples = false;
            silent.Play(child.ToClip(), parentSong.ToClip());
            MorphPlan plan = silent.CurrentPlan;
            silent.Stop();
            silent.ApplesToApples = apples;
            int expected = Morph.Wrap((parentSong.ExitTonicPc ?? parentSong.TonicPc) - (child.EntryTonicPc ?? child.TonicPc));
            report.Check($"{label}: the key handoff runs from the previous excerpt's exit key to this excerpt's entry key",
                Math.Abs(plan.StartSemitones - expected) < 1e-9,
                $"node {parentSong.NodeId} → {child.NodeId}: {plan.StartSemitones:+0;-0;0} st (home keys would give {Morph.Wrap(parentSong.TonicPc - child.TonicPc):+0;-0;0})");
        }

        static int Count(SqliteConnection conn, string sql)
        {
            using SqliteCommand cmd = conn.CreateCommand();
            cmd.CommandText = sql;
            return Convert.ToInt32(cmd.ExecuteScalar(), CultureInfo.InvariantCulture);
        }

        static string Slug(string label) => label.Replace(' ', '_');

        /// <summary>Drawn labels must never overlap on screen (decluttering), and the years must all show.</summary>
        static void CheckLabels(Report report, SongGraphLoader loader, string view)
        {
            List<WorldLabel> drawn = loader.Labels.Labels.Where(l => l.Visible && l.Placed).ToList();
            int overlaps = 0;
            for (int i = 0; i < drawn.Count; i++)
                for (int j = i + 1; j < drawn.Count; j++)
                    if (drawn[i].ScreenRect.Overlaps(drawn[j].ScreenRect)) overlaps++;
            int pinnedDrawn = loader.PinnedLabelNodes.Count(n => n.Label != null && n.Label.Placed);
            Material? labelMaterial = loader.Labels.SharedLabelMaterial;
            report.Check($"{view}: labels draw on top (TMP overlay shader)", labelMaterial != null &&
                labelMaterial.shader.name == LabelLayer.OverlayShaderName, labelMaterial != null ? labelMaterial.shader.name : "none");
            report.Number($"{view}_labels_drawn", drawn.Count, "0");
            report.Number($"{view}_pinned_labels_drawn", pinnedDrawn, "0");
            report.Check($"{view}: drawn labels never overlap", overlaps == 0, $"{drawn.Count} labels, {overlaps} overlapping pairs");
            // Labels keep clear of the HUD panels and the screen edge (LabelLayer.KeepClear); focus labels are exempt.
            IReadOnlyList<Rect> panels = loader.Hud.PanelScreenRects(captureWidth, captureHeight);
            int hidden = drawn.Count(l => l.Priority < LabelLayer.FocusPriority &&
                (panels.Any(p => l.ScreenRect.Overlaps(p)) || l.ScreenRect.xMin < 0 || l.ScreenRect.yMin < 0 ||
                 l.ScreenRect.xMax > captureWidth || l.ScreenRect.yMax > captureHeight));
            report.Check($"{view}: no drawn label under a HUD panel or cut by the screen edge", hidden == 0 && panels.Count > 0,
                $"{panels.Count} panels, {hidden} labels under a panel or off screen");
        }

        static void TickDirector(WalkthroughDirector director, float dt)
        {
            director.Tick(dt);
            if (director.ActivePlayer is SilentSongPlayer silent) silent.Advance(dt);
        }

        static void ValidateSimulation(Report report, SongGraphLoader loader)
        {
            ForceDirectedGraph sim = loader.Simulation;
            report.Check("live simulation is off by default", !sim.IsRunning);
            Vector3[] before = loader.Nodes.Select(x => x.transform.position).ToArray();
            Stopwatch clock = Stopwatch.StartNew();
            const int steps = 3;
            for (int i = 0; i < steps; i++) sim.StepOnce();
            clock.Stop();
            report.Number("fdg_step_ms", clock.Elapsed.TotalMilliseconds / steps, "0.0");
            float maxAlong = 0, maxAcross = 0;
            for (int i = 0; i < before.Length; i++)
            {
                Vector3 d = loader.Nodes[i].transform.position - before[i];
                float along = Mathf.Abs(Vector3.Dot(d, loader.Frame.TimeDir));
                maxAlong = Mathf.Max(maxAlong, along);
                maxAcross = Mathf.Max(maxAcross, (d - Vector3.Dot(d, loader.Frame.TimeDir) * loader.Frame.TimeDir).magnitude);
            }
            report.Check("live simulation never moves songs along the time axis", maxAlong < 1e-3f && maxAcross > 0,
                $"max along {maxAlong:0.000000}, max across {maxAcross:0.000}");
            for (int i = 0; i < before.Length; i++) loader.Nodes[i].transform.position = before[i];
            foreach (SongNode node in loader.Nodes) node.OnMoved();
        }

        // ------------------------------------------------------------------ fixture
        static void ValidateFixture(Report report, SongGraphLoader loader, Camera cam, string fixturePath, string outDir, int width, int height)
        {
            loader.Build(fixturePath);
            SongGraphData data = loader.Data!;
            report.Number("fixture_songs", data.Songs.Count, "0");
            report.Check("fixture: §10 invariants hold", data.Problems.Count == 0, string.Join(" | ", data.Problems.Take(5)));
            ValidateRegionKeys(report, loader, fixturePath, "fixture");
            report.Check("fixture: positions NULL → fallback layout", !data.HasPositions && loader.Frame.Description.StartsWith("fallback"));
            report.Check("fixture: fallback pins time exactly", loader.Frame.MaxResidualYears < 1e-3, $"{loader.Frame.MaxResidualYears:0.000000} y");
            string folder = Path.GetDirectoryName(fixturePath) ?? "";
            report.Check("fixture: MIDI paths resolve against the DB folder", data.Songs.All(s =>
                File.Exists(s.MidiPath) && s.MidiPath.StartsWith(Path.GetFullPath(Path.Combine(folder, "..")), StringComparison.OrdinalIgnoreCase)));
            report.Check("fixture: every clip carries excerpt and key facts", data.Songs.All(s =>
            {
                SongClip c = s.ToClip();
                return c.ExcerptEndBeat > c.ExcerptStartBeat && c.NativeBpm > 0 && c.BeatsPerBar > 0 && Path.IsPathRooted(c.MidiPath);
            }));
            float minGap = float.MaxValue;
            for (int i = 0; i < loader.Nodes.Count; i++)
                for (int j = i + 1; j < loader.Nodes.Count; j++)
                {
                    SongNode a = loader.Nodes[i], b = loader.Nodes[j];
                    minGap = Mathf.Min(minGap, Vector3.Distance(a.transform.position, b.transform.position) - a.Radius - b.Radius);
                }
            report.Check("fixture: fallback layout keeps bubbles apart", minGap > 0, $"min gap {minGap:0.00}");

            WalkthroughDirector director = loader.Director;
            int deepest = TourPlanner.DefaultStart(data, TourMode.Lineage);
            director.StartTour(TourMode.Lineage, loader.NodeById(deepest));
            if (director.Steps.Count > 1) director.Next();
            bool usesReal = director.Player != null && !(director.Player is SilentSongPlayer);
            report.Check("fixture: director uses the synth for existing MIDI files (or silent when none is installed)",
                usesReal ? ReferenceEquals(director.ActivePlayer, director.Player) : director.ActivePlayer is SilentSongPlayer,
                director.PlayerDescription);
            for (int i = 0; i < 45; i++) TickDirector(director, .1f);
            string path = Path.Combine(outDir, "fixture_walkthrough.png");
            Capture(cam, loader, path, width, height, out _);
            director.Exit();
            ValidateDirectorWithProbe(report, loader, deepest);
            loader.FrameOverview(cam);
            string overview = Path.Combine(outDir, "fixture_overview.png");
            Capture(cam, loader, overview, width, height, out _);
            report.Check("fixture PNGs written", File.Exists(path) && File.Exists(overview));

            // The same graph with time running bottom → top.
            loader.TimeAxis = TimeAxisView.BottomToTop;
            loader.Build(fixturePath);
            GraphFrame vertical = loader.Frame;
            report.Check("bottom-to-top view: time runs up and stays pinned", Vector3.Dot(vertical.TimeDir, Vector3.up) > .999f &&
                vertical.MaxResidualYears < 1e-3 && loader.Timeline.DecadeYears.All(y =>
                    Mathf.Abs(Vector3.Dot(loader.Timeline.AxisPoint(y), vertical.TimeDir) - vertical.TimeCoord(y)) < 1e-3f));
            Capture(cam, loader, Path.Combine(outDir, "fixture_vertical.png"), width, height, out _);
            loader.TimeAxis = TimeAxisView.LeftToRight;
        }

        /// <summary>
        /// A recording <see cref="ISongPlayer"/>: the director's calls into a real synth, observable.
        /// </summary>
        sealed class ProbePlayer : ISongPlayer
        {
            public readonly List<string> Calls = new();
            public SongClip? Clip, Previous;
            public bool Paused { get; set; }
            public bool IsPlaying { get; private set; }
            public double CurrentBeat { get; set; }
            public double CurrentSemitones => 0;
            public double CurrentBpm => Clip?.NativeBpm ?? 0;
            public MorphPlan CurrentPlan { get; private set; } = MorphPlan.None;
            public float MorphBars { get; set; } = 4;
            public bool ApplesToApples { get; set; }
            public event Action<SongClip>? Started;
            public event Action<SongClip>? Finished;
            public int Stops => Calls.Count(c => c == "stop");

            public void Play(SongClip clip, SongClip previous)
            {
                Calls.Add($"play {clip.NodeId} after {(previous != null ? previous.NodeId.ToString(CultureInfo.InvariantCulture) : "none")}");
                Clip = clip;
                Previous = previous;
                IsPlaying = true;
                Paused = false;
                CurrentBeat = clip.ExcerptStartBeat;
                CurrentPlan = ApplesToApples ? MorphPlan.None : Morph.Plan(previous!, clip, MorphBars);
                Started?.Invoke(clip);
            }

            public void Stop()
            {
                Calls.Add("stop");
                IsPlaying = false;
            }

            /// <summary>The excerpt ends naturally.</summary>
            public void Finish()
            {
                if (Clip == null) return;
                IsPlaying = false;
                CurrentBeat = Clip.ExcerptEndBeat;
                Finished?.Invoke(Clip);
            }
        }

        /// <summary>
        /// The director's player protocol, with a probe standing in for the synth (fixture songs have
        /// real MIDI files): no Stop on the way to a step the same player plays (a Stop would throw
        /// away the bar-line handoff of a naturally finished clip), "previous" = the clip heard just
        /// before, and the progress-based watchdog.
        /// </summary>
        static void ValidateDirectorWithProbe(Report report, SongGraphLoader loader, int deepest)
        {
            WalkthroughDirector director = loader.Director;
            SongGraphData data = loader.Data!;
            ISongPlayer? original = director.Player;
            string originalDescription = director.PlayerDescription;
            ProbePlayer probe = new();
            director.UsePlayer(probe, "validation probe");
            try
            {
                director.StartTour(TourMode.Lineage, loader.NodeById(deepest));
                IReadOnlyList<int> s = director.Steps;
                if (!report.Check("probe: fixture lineage has at least 5 steps", s.Count >= 5, $"{s.Count} steps")) return;
                report.Check("probe: the tour's first song plays natively", probe.Calls.SequenceEqual(new[] { $"play {s[0]} after none" }),
                    string.Join("; ", probe.Calls));

                probe.Finish();
                director.Tick(.05f);
                report.Check("probe: natural advance calls Play without Stop (keeps the bar-line handoff)",
                    director.StepIndex == 1 && probe.Stops == 0 && probe.Previous?.NodeId == s[0], string.Join("; ", probe.Calls));

                director.Next();
                report.Check("probe: Next mid-clip replaces the clip on the same player without Stop",
                    director.StepIndex == 2 && probe.Stops == 0 && probe.Previous?.NodeId == s[1], string.Join("; ", probe.Calls));

                director.Previous();
                report.Check("probe: B/← plays from the song just heard (step 3), not steps[i-1]",
                    director.StepIndex == 1 && probe.Previous?.NodeId == s[2] && director.PreviousClip?.NodeId == s[2], probe.Calls[^1]);

                director.GoTo(director.StepIndex);
                report.Check("probe: a restart plays from the same song", probe.Previous?.NodeId == s[1] && probe.Stops == 0, probe.Calls[^1]);

                // A step whose MIDI file is missing moves to the silent clock: the synth is stopped once.
                SongRecord missing = data.Song(s[2]);
                string savedPath = missing.MidiPath;
                missing.MidiPath = Path.Combine(data.DbFolder, "missing-for-validation.mid");
                try
                {
                    director.Next();
                }
                finally
                {
                    missing.MidiPath = savedPath;
                }
                report.Check("probe: switching to the silent clock stops the synth", director.ActivePlayer is SilentSongPlayer && probe.Stops == 1,
                    string.Join("; ", probe.Calls.Skip(Math.Max(0, probe.Calls.Count - 3))));
                director.Next();
                report.Check("probe: back on the synth, playing from the song heard on the silent clock",
                    ReferenceEquals(director.ActivePlayer, probe) && probe.Previous?.NodeId == s[2] && probe.Stops == 1, probe.Calls[^1]);

                // Watchdog: a slow section keeps making progress past the old median-BPM budget.
                SongClip clip = director.CurrentClip!;
                int step = director.StepIndex;
                double length = clip.ExcerptEndBeat - clip.ExcerptStartBeat;
                double oldBudget = SilentSongPlayer.ExpectedSeconds(clip, director.CurrentPlan) * 1.25 + 8.0;
                double run = oldBudget + 10;
                double beatsPerSecond = Math.Min(0.4 * clip.NativeBpm / 60.0, 0.9 * length / run);
                for (double t = 0; t < run; t += .1)
                {
                    probe.CurrentBeat += .1 * beatsPerSecond;
                    director.Tick(.1f);
                }
                report.Check("watchdog: steady progress past the old duration budget is not cut",
                    director.StepIndex == step && ReferenceEquals(director.CurrentClip, clip),
                    $"{run:0} s at {beatsPerSecond * 60:0.#} BPM (median {clip.NativeBpm:0.#}); old budget {oldBudget:0.0} s");
                // ...but a player that stops moving is advanced after WatchdogSeconds.
                double stalled = 0;
                while (director.StepIndex == step && stalled < director.WatchdogSeconds + 5)
                {
                    director.Tick(.1f);
                    stalled += .1;
                }
                report.Check("watchdog: no progress for WatchdogSeconds advances the tour",
                    director.StepIndex == step + 1 && stalled > director.WatchdogSeconds && stalled < director.WatchdogSeconds + 1,
                    $"advanced after {stalled:0.0} s without progress (WatchdogSeconds {director.WatchdogSeconds:0})");
                director.TogglePause();
                for (int i = 0; i < 300; i++) director.Tick(.1f);
                report.Check("watchdog: a paused player is never advanced", director.StepIndex == step + 1 && director.StallSeconds == 0);
                director.TogglePause();
            }
            finally
            {
                director.Exit();
                if (original != null) director.UsePlayer(original, originalDescription);
            }
        }

        // ------------------------------------------------------------------ player clock
        static void ValidateSilentPlayer(Report report)
        {
            GameObject go = new("Silent Player Test") { hideFlags = HideFlags.DontSave };
            try
            {
                SilentSongPlayer player = go.AddComponent<SilentSongPlayer>();
                SongClip previous = new() { NodeId = 1, Title = "A", TonicPc = 7, Minor = false, NativeBpm = 118, BeatsPerBar = 4, ExcerptStartBeat = 0, ExcerptEndBeat = 64 };
                SongClip next = new() { NodeId = 2, Title = "B", TonicPc = 4, Minor = false, NativeBpm = 96, BeatsPerBar = 4, ExcerptStartBeat = 32, ExcerptEndBeat = 96, NormShift = -4 };
                int finished = 0, started = 0;
                player.Finished += _ => finished++;
                player.Started += _ => started++;
                player.MorphBars = 4;
                player.Play(next, previous);
                MorphPlan plan = player.Plan;
                report.Check("silent player: CurrentPlan is the plan it plays", SamePlan(player.CurrentPlan, plan) &&
                    Math.Abs(player.PlanStartBpm - 118) < 1e-6);
                report.Check("silent player: starts in the previous key (+3 st) and folded BPM (118)",
                    Math.Abs(player.CurrentSemitones - 3) < 1e-9 && Math.Abs(player.CurrentBpm - 118) < 1e-6,
                    $"{player.CurrentSemitones:0.00} st, {player.CurrentBpm:0.00} BPM");
                double seconds = 0, atMorphEnd = double.NaN;
                const double dt = 1.0 / 60.0;
                while (player.IsPlaying && seconds < 300)
                {
                    player.Advance(dt);
                    seconds += dt;
                    if (double.IsNaN(atMorphEnd) && player.CurrentBeat - next.ExcerptStartBeat >= plan.MorphBeats)
                        atMorphEnd = player.CurrentSemitones + Math.Abs(player.CurrentBpm - 96);
                }
                double expected = SilentSongPlayer.ExpectedSeconds(next, plan);
                report.Check("silent player: glides to native key and BPM after morphBars bars", Math.Abs(atMorphEnd) < 1e-6);
                report.Check("silent player: excerpt time matches ∫60/(N·r(b))db within 0.5%", Math.Abs(seconds - expected) / expected < .005,
                    $"{seconds:0.000} s vs {expected:0.000} s");
                report.Check("silent player: Started and Finished raised once", started == 1 && finished == 1);
                report.Number("silent_excerpt_seconds", seconds, "0.000");

                player.Play(next, previous);
                player.Paused = true;
                player.Advance(1.0);
                report.Check("silent player: paused clock stands still", Math.Abs(player.CurrentBeat - next.ExcerptStartBeat) < 1e-12);
                player.Paused = false;
                player.ApplesToApples = true;
                player.Play(next, previous);
                report.Check("silent player: apples-to-apples plays at 120 BPM, shifted by norm_shift, no morph",
                    Math.Abs(player.CurrentBpm - 120) < 1e-9 && Math.Abs(player.CurrentSemitones - next.NormShift) < 1e-9 && player.Plan.MorphBeats == 0);
                player.Play(next, null!);
                report.Check("silent player: first song plays natively (no previous)", player.Plan.MorphBeats == 0);
                player.ApplesToApples = false;
                SongClip leavesInD = new() { NodeId = 3, Title = "C", TonicPc = 0, NativeBpm = 118, BeatsPerBar = 4, ExcerptStartBeat = 0, ExcerptEndBeat = 64, ExitTonicPc = 2, ExitMinor = false };
                SongClip entersInF = new() { NodeId = 4, Title = "D", TonicPc = 4, NativeBpm = 96, BeatsPerBar = 4, ExcerptStartBeat = 0, ExcerptEndBeat = 64, EntryTonicPc = 5, EntryMinor = false };
                player.Play(entersInF, leavesInD);
                report.Check("silent player: the key handoff runs from the exit key (D) to the entry key (F): -3 st",
                    Math.Abs(player.CurrentPlan.StartSemitones - (-3)) < 1e-9, $"{player.CurrentPlan.StartSemitones:+0;-0;0} st");
                report.Check("walkthrough: normalized target key follows norm_shift (relative and parallel)",
                    WalkthroughDirector.NormalizedKeyName(new SongClip { TonicPc = 2, Minor = true, NormShift = -5 }) == "A minor" &&
                    WalkthroughDirector.NormalizedKeyName(new SongClip { TonicPc = 2, Minor = true, NormShift = -2 }) == "C minor" &&
                    WalkthroughDirector.NormalizedKeyName(new SongClip { TonicPc = 9, Minor = true, NormShift = 3 }) == "C minor" &&
                    WalkthroughDirector.NormalizedKeyName(new SongClip { TonicPc = 4, Minor = false, NormShift = -4 }) == "C major");
            }
            finally
            {
                Object.DestroyImmediate(go);
            }
        }

        // ------------------------------------------------------------------ rendering
        struct RenderStats
        {
            public long DrawCalls;
            public long SetPass;
            public long Batches;
        }

        /// <summary>Renders <paramref name="cam"/> (with the HUD) at width x height; writes a PNG when <paramref name="path"/> is set. Returns ms.</summary>
        /// <summary>Size of the last capture: label screen rects are in its pixels.</summary>
        static int captureWidth = 1920, captureHeight = 1080;

        static double Capture(Camera cam, SongGraphLoader loader, string? path, int width, int height, out RenderStats stats)
        {
            captureWidth = width;
            captureHeight = height;
            RenderTexture rt = new(width, height, 24, RenderTextureFormat.ARGB32) { antiAliasing = 4, name = "Validation Capture" };
            rt.Create();
            RenderTexture? previousTarget = cam.targetTexture;
            RenderTexture? previousActive = RenderTexture.active;
            Texture2D texture = new(width, height, TextureFormat.RGB24, false);
            ProfilerRecorder drawCalls = ProfilerRecorder.StartNew(ProfilerCategory.Render, "Draw Calls Count");
            ProfilerRecorder setPass = ProfilerRecorder.StartNew(ProfilerCategory.Render, "SetPass Calls Count");
            ProfilerRecorder batches = ProfilerRecorder.StartNew(ProfilerCategory.Render, "Batches Count");
            try
            {
                cam.targetTexture = rt;
                cam.aspect = (float)width / height;
                loader.RefreshView(cam);
                loader.Hud.RenderInto(cam);
                Stopwatch clock = Stopwatch.StartNew();
                cam.Render();
                RenderTexture.active = rt;
                texture.ReadPixels(new Rect(0, 0, width, height), 0, 0);
                texture.Apply();
                clock.Stop();
                stats = new RenderStats { DrawCalls = drawCalls.CurrentValue, SetPass = setPass.CurrentValue, Batches = batches.CurrentValue };
                if (path != null) File.WriteAllBytes(path, texture.EncodeToPNG());
                return clock.Elapsed.TotalMilliseconds;
            }
            finally
            {
                drawCalls.Dispose();
                setPass.Dispose();
                batches.Dispose();
                loader.Hud.RenderInto(null);
                cam.targetTexture = previousTarget;
                RenderTexture.active = previousActive;
                rt.Release();
                Object.DestroyImmediate(rt);
                Object.DestroyImmediate(texture);
            }
        }

        // ------------------------------------------------------------------ helpers
        static string? Arg(string name)
        {
            string[] args = Environment.GetCommandLineArgs();
            for (int i = 0; i + 1 < args.Length; i++)
                if (string.Equals(args[i], name, StringComparison.OrdinalIgnoreCase)) return args[i + 1];
            return null;
        }

        static string? PathArg(string name)
        {
            string? value = Arg(name);
            return value == null ? null : SongGraphLoader.ResolveUserPath(value);
        }

        static string? FirstExisting(params string[] paths) => paths.FirstOrDefault(File.Exists);

        static bool PathsEqual(string a, string b) =>
            string.Equals(Path.GetFullPath(a), Path.GetFullPath(b), StringComparison.OrdinalIgnoreCase);

        static string FirstLines(string text, int lines) =>
            string.Join(" / ", text.Split('\n').Take(lines)).Replace("\"", "'");

        static void WriteJson(Report report, string path)
        {
            StringBuilder b = new();
            b.Append("{\n  \"passed\": ").Append(report.Failures == 0 ? "true" : "false");
            b.Append(",\n  \"checks_passed\": ").Append(report.Checks.Count - report.Failures);
            b.Append(",\n  \"checks_total\": ").Append(report.Checks.Count);
            b.Append(",\n  \"generated_at\": \"").Append(DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture)).Append('"');
            b.Append(",\n  \"numbers\": {");
            b.Append(string.Join(",", report.Numbers.Select(kv => $"\n    \"{J(kv.Key)}\": \"{J(kv.Value)}\"")));
            b.Append("\n  },\n  \"checks\": [");
            b.Append(string.Join(",", report.Checks.Select(c =>
                $"\n    {{\"name\": \"{J(c.name)}\", \"ok\": {(c.ok ? "true" : "false")}, \"detail\": \"{J(c.detail)}\"}}")));
            b.Append("\n  ]\n}\n");
            File.WriteAllText(path, b.ToString(), new UTF8Encoding(false));
        }

        static string J(string s)
        {
            StringBuilder b = new(s.Length + 8);
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': b.Append("\\\""); break;
                    case '\\': b.Append("\\\\"); break;
                    case '\n': b.Append("\\n"); break;
                    case '\r': break;
                    case '\t': b.Append("\\t"); break;
                    default:
                        if (c < 0x20) b.Append($"\\u{(int)c:x4}");
                        else b.Append(c);
                        break;
                }
            }
            return b.ToString();
        }
    }
}
