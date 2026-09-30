#nullable enable
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using Mono.Data.Sqlite;
using MusicHistory.Playback;
using MusicHistory.Themes;
using MusicHistory.Viewer;
using Unity.Profiling;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering.Universal;
using UnityEngine.SceneManagement;
using Debug = UnityEngine.Debug;
using Object = UnityEngine.Object;

namespace MusicHistory.EditorTools
{
    /// <summary>
    /// Headless validation of the LyricThemes viewer (docs/DESIGN.md §12), for
    /// <c>Unity.exe -batchmode -projectPath unity -executeMethod MusicHistory.EditorTools.ThemesValidation.Run -logFile ...</c>
    /// (without -nographics, so the camera can render; the method exits itself, 0 = every check passed).
    ///
    /// Opens the scene and builds it in edit mode from each themes DB (default: data/graph/themes_demo.db,
    /// then data/graph/themes_graph.db when it exists; -themesValidationDb &lt;path&gt; to name one), and checks
    /// counts against the DB, equally spaced anchors, colours by singer gender, the hover card (only
    /// title, artist, year, singer, text source, theme labels and scores, for every song), picking,
    /// click-to-play through a recording ISongPlayer (native key/BPM, second click stops), the theme
    /// filter, labels, and renders data/screens/themes_overview.png (the DB the scene opens by default)
    /// plus per-DB overview and hover PNGs; writes data/screens/themes_validation.json.
    ///
    /// <c>-executeMethod MusicHistory.EditorTools.ThemesValidation.CreateScene</c> (re)creates
    /// Assets/Scenes/LyricThemes.unity and adds it to the build settings.
    /// Options: -themesValidationDb &lt;path&gt; -validationOut &lt;dir&gt; -validationWidth 1920 -validationHeight 1080.
    /// </summary>
    public static class ThemesValidation
    {
        public const string ScenePath = "Assets/Scenes/LyricThemes.unity";

        sealed class Report
        {
            public readonly List<(string name, bool ok, string detail)> Checks = new();
            public readonly SortedDictionary<string, string> Numbers = new(StringComparer.Ordinal);
            public int Failures => Checks.Count(c => !c.ok);

            public bool Check(string name, bool ok, string detail = "")
            {
                Checks.Add((name, ok, detail));
                Debug.Log($"[themes-validation] {(ok ? "PASS" : "FAIL")} {name}{(detail.Length > 0 ? " — " + detail : "")}");
                return ok;
            }

            public void Number(string name, double value, string format = "0.###")
            {
                Numbers[name] = value.ToString(format, CultureInfo.InvariantCulture);
                Debug.Log($"[themes-validation] {name} = {Numbers[name]}");
            }

            public void Text(string name, string value)
            {
                Numbers[name] = value;
                Debug.Log($"[themes-validation] {name} = {value}");
            }
        }

        // ------------------------------------------------------------------ scene creation

        [MenuItem("MusicHistory/Lyric Themes/Create Scene")]
        public static void CreateSceneFromMenu() => CreateSceneAsset();

        /// <summary>Batch entry point: creates the scene, then exits (0 = saved and in the build settings).</summary>
        public static void CreateScene()
        {
            bool ok;
            try
            {
                ok = CreateSceneAsset();
            }
            catch (Exception e)
            {
                Debug.LogException(e);
                ok = false;
            }
            EditorApplication.Exit(ok ? 0 : 1);
        }

        static bool CreateSceneAsset()
        {
            Scene scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            GameObject camera = new("Main Camera") { tag = "MainCamera" };
            Camera cam = camera.AddComponent<Camera>();
            cam.fieldOfView = 45f;
            cam.nearClipPlane = .3f;
            cam.farClipPlane = 3000f;
            cam.clearFlags = CameraClearFlags.Skybox;
            cam.allowHDR = true;
            cam.allowMSAA = true;
            camera.AddComponent<AudioListener>();
            UniversalAdditionalCameraData urp = cam.GetUniversalAdditionalCameraData();
            urp.renderPostProcessing = true;
            camera.AddComponent<ThemesOrbitCamera>();
            camera.transform.position = new Vector3(0f, 95f, -46f);
            camera.transform.LookAt(Vector3.zero);

            GameObject viewer = new("Lyric Themes");
            viewer.AddComponent<ThemesViewer>();

            bool saved = EditorSceneManager.SaveScene(scene, ScenePath);
            AssetDatabase.Refresh();
            string guid = AssetDatabase.AssetPathToGUID(ScenePath);
            List<EditorBuildSettingsScene> scenes = EditorBuildSettings.scenes.ToList();
            if (!scenes.Any(s => s.path == ScenePath))
            {
                scenes.Add(new EditorBuildSettingsScene(ScenePath, true));
                EditorBuildSettings.scenes = scenes.ToArray();
            }
            AssetDatabase.SaveAssets();
            bool inBuild = EditorBuildSettings.scenes.Any(s => s.path == ScenePath && s.enabled);
            Debug.Log($"[themes-validation] scene {ScenePath} saved={saved} guid={guid} in build settings={inBuild}");
            return saved && inBuild && !string.IsNullOrEmpty(guid);
        }

        // ------------------------------------------------------------------ validation

        [MenuItem("MusicHistory/Lyric Themes/Run Validation (writes data/screens)")]
        public static void RunFromMenu() => Execute(exitWhenDone: false);

        public static void Run() => Execute(exitWhenDone: true);

        static void Execute(bool exitWhenDone)
        {
            Report report = new();
            string outDir = Arg("-validationOut") ?? Path.Combine(ThemesGraphReader.RepoRoot(), "data", "screens");
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
            WriteJson(report, Path.Combine(outDir, "themes_validation.json"));
            Debug.Log($"[themes-validation] {report.Checks.Count - report.Failures}/{report.Checks.Count} checks passed; report in {outDir}");
            if (exitWhenDone) EditorApplication.Exit(report.Failures == 0 ? 0 : 1);
        }

        static void RunAll(Report report, string outDir)
        {
            string repo = ThemesGraphReader.RepoRoot();
            string graphDir = Path.Combine(repo, "data", "graph");
            int width = int.TryParse(Arg("-validationWidth"), out int w) ? w : 1920;
            int height = int.TryParse(Arg("-validationHeight"), out int h) ? h : 1080;
            report.Text("unity_version", Application.unityVersion);
            report.Text("graphics_device", SystemInfo.graphicsDeviceName + " / " + SystemInfo.graphicsDeviceType);

            EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
            ThemesViewer? viewer = Object.FindAnyObjectByType<ThemesViewer>();
            if (!report.Check("scene has a ThemesViewer", viewer != null)) return;
            report.Check("scene is in build settings", EditorBuildSettings.scenes.Any(s => s.path == ScenePath && s.enabled));
            report.Check("influence graph scene is in build settings (the back button/G key loads it)",
                ThemesViewer.InfluenceSceneInBuild && File.Exists(Path.Combine(Application.dataPath, "..", ThemesViewer.InfluenceScenePath)));
            Camera? cam = Camera.main;
            if (!report.Check("scene has a MainCamera with ThemesOrbitCamera and an AudioListener",
                    cam != null && cam.GetComponent<ThemesOrbitCamera>() != null && cam.GetComponent<AudioListener>() != null)) return;
            cam!.aspect = (float)width / height;

            string defaultDb = ThemesGraphReader.ResolveDatabasePath(viewer!.DbPath, out string reason);
            string? cliDb = PathArg(ThemesGraphReader.CommandLineFlag);
            if (cliDb != null)
                report.Check("-themesDb overrides the default path", PathsEqual(defaultDb, cliDb) && reason == "command line", $"{defaultDb} ({reason})");
            report.Text("default_db", $"{defaultDb} ({reason})");

            List<string> dbs = new();
            string? named = PathArg("-themesValidationDb");
            if (named != null) dbs.Add(named);
            else
            {
                foreach (string candidate in new[] { Path.Combine(graphDir, ThemesGraphReader.DemoFileName), Path.Combine(graphDir, ThemesGraphReader.DefaultFileName) })
                    if (File.Exists(candidate)) dbs.Add(Path.GetFullPath(candidate));
            }
            if (!report.Check("a themes database exists", dbs.Count > 0, string.Join(", ", dbs))) return;

            foreach (string db in dbs) ValidateDatabase(report, viewer, cam, db, outDir, width, height);

            // The overview the scene opens by default.
            if (File.Exists(defaultDb))
            {
                viewer.Build(defaultDb);
                viewer.FrameOverview(cam);
                string overview = Path.Combine(outDir, "themes_overview.png");
                Capture(cam, viewer, overview, width, height, out _);
                report.Check("themes_overview.png written (the DB the scene opens by default)",
                    File.Exists(overview) && new FileInfo(overview).Length > 10000, $"{overview} from {Path.GetFileName(defaultDb)}");
            }
            ValidateOrbitCamera(report, cam);
            viewer.Clear();
        }

        // ------------------------------------------------------------------ one database

        static void ValidateDatabase(Report report, ThemesViewer viewer, Camera cam, string dbPath, string outDir, int width, int height)
        {
            string tag = Path.GetFileNameWithoutExtension(dbPath);
            Stopwatch clock = Stopwatch.StartNew();
            viewer.Build(dbPath);
            clock.Stop();
            ThemesGraphData data = viewer.Data!;
            int n = data.Songs.Count, k = data.Anchors.Count;
            report.Number($"{tag}.build_ms", clock.Elapsed.TotalMilliseconds, "0");
            report.Number($"{tag}.songs", n, "0");
            report.Text($"{tag}.positions", viewer.PositionSource);
            report.Check($"{tag}: §12 contract holds (ids, order, scores sum to 1, top anchor, genders, sources, meta counts)",
                data.Problems.Count == 0, string.Join(" | ", data.Problems.Take(5)));

            using SqliteConnection conn = new("URI=file:" + dbPath);
            conn.Open();
            int dbSongs = Count(conn, "SELECT COUNT(*) FROM theme_song");
            int dbAnchors = Count(conn, "SELECT COUNT(*) FROM theme_anchor");
            int dbScores = Count(conn, "SELECT COUNT(*) FROM theme_score");
            int dbLyrics = Count(conn, "SELECT COUNT(*) FROM theme_song WHERE text_source = 'lyrics'");
            int dbMale = Count(conn, "SELECT COUNT(*) FROM theme_song WHERE singer_gender = 'male'");
            int dbFemale = Count(conn, "SELECT COUNT(*) FROM theme_song WHERE singer_gender = 'female'");
            report.Check($"{tag}: one bubble per theme_song row", n == dbSongs && viewer.Nodes.Count == dbSongs &&
                Object.FindObjectsByType<ThemeSongNode>().Length == dbSongs, $"{viewer.Nodes.Count} bubbles, {dbSongs} rows");
            report.Check($"{tag}: ten anchors, one pad and one label each", k == 10 && dbAnchors == 10 && viewer.Ring.Anchors.Count == 10 &&
                viewer.Ring.Anchors.All(a => a.Label != null && a.Pad != null));
            report.Check($"{tag}: theme_score has songs x anchors rows", dbScores == n * k && data.ScoreRows == dbScores, $"{dbScores} rows");
            report.Check($"{tag}: lyrics/title counts match the DB", data.LyricsCount == dbLyrics, $"{dbLyrics} lyrics, {n - dbLyrics} title only");
            report.Check($"{tag}: themes_meta song_count matches", data.MetaInt("song_count") is not int sc || sc == n, $"meta {data.MetaInt("song_count")}");
            report.Number($"{tag}.lyrics", dbLyrics, "0");
            report.Number($"{tag}.title_only", n - dbLyrics, "0");

            ValidateAnchors(report, viewer, conn, tag);
            ValidatePositions(report, viewer, conn, tag);
            ValidateColors(report, viewer, tag, dbMale, dbFemale);
            ValidateHoverText(report, viewer, tag);

            viewer.FrameOverview(cam);
            string overview = Path.Combine(outDir, $"themes_overview_{tag}.png");
            double first = Capture(cam, viewer, overview, width, height, out RenderStats stats);
            List<double> times = new();
            for (int i = 0; i < 10; i++) times.Add(Capture(cam, viewer, null, width, height, out stats));
            report.Number($"{tag}.overview_render_ms_first", first, "0.0");
            report.Number($"{tag}.overview_render_ms_mean_of_10", times.Average(), "0.00");
            report.Number($"{tag}.overview_render_ms_max_of_10", times.Max(), "0.00");
            if (stats.DrawCalls > 0) report.Number($"{tag}.overview_draw_calls", stats.DrawCalls, "0");
            if (stats.Batches > 0) report.Number($"{tag}.overview_batches", stats.Batches, "0");
            if (stats.SetPass > 0) report.Number($"{tag}.overview_setpass_calls", stats.SetPass, "0");
            report.Check($"{tag}: overview PNG written", File.Exists(overview) && new FileInfo(overview).Length > 10000, overview);
            Measure(cam, viewer, width, height, () => ValidateOverviewLabels(report, viewer, cam, tag, width, height));
            ValidatePicking(report, viewer, cam, tag, outDir, width, height);
            ValidatePlayback(report, viewer, tag);
            ValidateFilterAndLabels(report, viewer, cam, tag, outDir, width, height);
        }

        static void ValidateAnchors(Report report, ThemesViewer viewer, SqliteConnection conn, string tag)
        {
            ThemesGraphData data = viewer.Data!;
            int k = data.Anchors.Count;
            float radius = data.RingRadius;
            report.Number($"{tag}.ring_radius", radius, "0.###");
            bool labels = data.Anchors.Select(a => a.Label).SequenceEqual(ThemesGraphReader.DesignThemes);
            report.Check($"{tag}: anchor labels are the ten themes of DESIGN §12, in order", labels,
                labels ? "" : string.Join(" | ", data.Anchors.Select(a => a.Label)));
            double maxAngleErr = 0, maxRadiusErr = 0, maxGapErr = 0, maxSceneErr = 0;
            for (int i = 0; i < k; i++)
            {
                ThemeAnchorRecord a = data.Anchors[i];
                double expected = 360.0 * i / k;
                maxAngleErr = Math.Max(maxAngleErr, Math.Abs(Wrap180(a.AngleDegrees - expected)));
                double measured = Math.Atan2(a.Position.z, a.Position.x) * 180.0 / Math.PI;
                maxAngleErr = Math.Max(maxAngleErr, Math.Abs(Wrap180(measured - expected)));
                maxRadiusErr = Math.Max(maxRadiusErr, Math.Abs(new Vector2(a.Position.x, a.Position.z).magnitude - radius));
                ThemeAnchorRecord b = data.Anchors[(i + 1) % k];
                double gap = Wrap360(Math.Atan2(b.Position.z, b.Position.x) * 180.0 / Math.PI - measured);
                maxGapErr = Math.Max(maxGapErr, Math.Abs(gap - 360.0 / k));
                maxSceneErr = Math.Max(maxSceneErr, (viewer.Ring.Anchors[i].transform.position - a.Position).magnitude);
            }
            report.Check($"{tag}: anchors equally spaced on the ring (angle 36°·(k−1), same radius, x–z plane)",
                maxAngleErr < 1e-3 && maxGapErr < 1e-3 && maxRadiusErr < 1e-3 * radius && data.Anchors.All(a => Mathf.Abs(a.Position.y) < 1e-4f),
                $"max angle error {maxAngleErr:0.00000}°, max gap error {maxGapErr:0.00000}°, max radius error {maxRadiusErr:0.00000}");
            report.Check($"{tag}: pads sit at the anchor positions", maxSceneErr < 1e-4, $"max {maxSceneErr:0.000000}");
            report.Check($"{tag}: every theme label shows its theme text", viewer.Ring.Anchors.All(a =>
                Plain(a.Label.Box.TextField.text).Contains(a.Anchor.Label) && a.Label.Visible));
            int dbTop = Count(conn, "SELECT COUNT(*) FROM theme_song WHERE top_anchor BETWEEN 1 AND 10");
            report.Check($"{tag}: theme labels count their songs (top theme)", data.Anchors.Sum(a => a.TopCount) == dbTop &&
                viewer.Ring.Anchors.All(a => a.Label.Box.TextField.text.Contains($"{a.Anchor.TopCount} song")));
            report.Text($"{tag}.songs_per_top_theme", string.Join(", ", data.Anchors.Select(a => $"{a.AnchorId}:{a.TopCount}")));
        }

        static void ValidatePositions(Report report, ThemesViewer viewer, SqliteConnection conn, string tag)
        {
            ThemesGraphData data = viewer.Data!;
            int n = data.Songs.Count;
            int dbPositioned = Count(conn, "SELECT COUNT(*) FROM theme_song WHERE position_x IS NOT NULL AND position_y IS NOT NULL AND position_z IS NOT NULL");
            report.Number($"{tag}.db_positioned_songs", dbPositioned, "0");
            if (dbPositioned == n)
            {
                float maxErr = 0;
                for (int i = 0; i < n; i++)
                    maxErr = Mathf.Max(maxErr, (viewer.Nodes[i].transform.position - data.Songs[i].LayoutPosition!.Value).magnitude);
                report.Check($"{tag}: bubbles sit at the layout positions", viewer.PositionSource == "layout" && maxErr < 1e-4f, $"max {maxErr:0.000000}");
            }
            else
            {
                report.Check($"{tag}: NULL positions → viewer fallback layout", viewer.PositionSource == "viewer fallback" && dbPositioned == 0,
                    $"{dbPositioned}/{n} positioned in the DB");
                Vector3[] again = ThemesGraphReader.FallbackPositions(data, viewer.BubbleRadius * 2.1f);
                float drift = 0;
                for (int i = 0; i < n; i++) drift = Mathf.Max(drift, (again[i] - viewer.Nodes[i].transform.position).magnitude);
                report.Check($"{tag}: fallback layout is deterministic", drift < 1e-5f, $"max {drift:0.0000000}");
                float minGap = float.MaxValue;
                for (int i = 0; i < n; i++)
                    for (int j = i + 1; j < n; j++)
                        minGap = Mathf.Min(minGap, (viewer.Nodes[i].transform.position - viewer.Nodes[j].transform.position).magnitude);
                report.Number($"{tag}.fallback_min_center_distance", minGap, "0.000");
                report.Check($"{tag}: fallback layout keeps bubbles from overlapping much", minGap > viewer.BubbleRadius * 1.2f,
                    $"closest centres {minGap:0.000} (bubble radius {viewer.BubbleRadius})");
            }
            bool finite = viewer.Nodes.All(x => IsFinite(x.transform.position));
            report.Check($"{tag}: every position is finite", finite);

            // A song that is (almost) all one theme sits nearer that theme than any other.
            int peaked = 0, onOwn = 0;
            double distanceSum = 0;
            foreach (ThemeSongNode node in viewer.Nodes)
            {
                if (node.Song.TopScore < .9) continue;
                peaked++;
                Vector3 p = node.transform.position;
                int nearest = data.Anchors.OrderBy(a => (a.Position - p).sqrMagnitude).First().AnchorId;
                if (nearest == node.Song.TopAnchor) onOwn++;
                distanceSum += (data.Anchor(node.Song.TopAnchor).Position - p).magnitude;
            }
            report.Number($"{tag}.peaked_songs", peaked, "0");
            if (peaked > 0)
            {
                report.Number($"{tag}.peaked_mean_distance_to_own_theme", distanceSum / peaked, "0.00");
                report.Check($"{tag}: songs scoring ≥ 0.9 on one theme sit nearest that theme", onOwn >= .95 * peaked, $"{onOwn}/{peaked}");
            }
            float maxRadius = viewer.Nodes.Count > 0 ? viewer.Nodes.Max(x => new Vector2(x.transform.position.x, x.transform.position.z).magnitude) : 0;
            report.Number($"{tag}.max_song_radius", maxRadius, "0.00");
        }

        static void ValidateColors(Report report, ThemesViewer viewer, string tag, int dbMale, int dbFemale)
        {
            int blue = 0, pink = 0, grey = 0, wrong = 0;
            foreach (ThemeSongNode node in viewer.Nodes)
            {
                Material m = node.BubbleRenderer.sharedMaterial;
                Color expected = node.Song.Gender switch
                {
                    SingerGender.Male => ThemesPalette.Male,
                    SingerGender.Female => ThemesPalette.Female,
                    _ => ThemesPalette.Neutral
                };
                // Lyrics songs are filled with the singer colour; title-only songs have it as their rim.
                Color shown = node.Song.FromLyrics ? m.GetColor("_FillColor") : m.GetColor("_RingColor");
                if (!Same(shown, expected)) wrong++;
                if (Same(shown, ThemesPalette.Male)) blue++;
                else if (Same(shown, ThemesPalette.Female)) pink++;
                else if (Same(shown, ThemesPalette.Neutral)) grey++;
            }
            int n = viewer.Nodes.Count;
            report.Check($"{tag}: bubbles are blue for male, pink for female, grey otherwise", wrong == 0 && blue == dbMale && pink == dbFemale &&
                grey == n - dbMale - dbFemale, $"blue {blue}/{dbMale}, pink {pink}/{dbFemale}, grey {grey}/{n - dbMale - dbFemale}, wrong {wrong}");
            report.Check($"{tag}: blue and pink are distinct hues, grey is unsaturated", HueDistance(ThemesPalette.Male, ThemesPalette.Female) > .25f &&
                Saturation(ThemesPalette.Neutral) < .15f);
            report.Check($"{tag}: bubbles share cached SongBubble materials", viewer.Nodes.All(x =>
                ThemesMaterials.IsSharedBubble(x.BubbleRenderer.sharedMaterial) && x.BubbleRenderer.sharedMaterial.shader.name == ThemesMaterials.BubbleShaderName) &&
                ThemesMaterials.BubbleMaterialCount <= 24, $"{ThemesMaterials.BubbleMaterialCount} bubble materials");
            report.Number($"{tag}.bubble_materials", ThemesMaterials.BubbleMaterialCount, "0");
            string legend = viewer.Hud.LegendText;
            report.Check($"{tag}: legend explains the colours", legend.Contains("male singer") && legend.Contains("female singer") &&
                legend.Contains("mixed, nonbinary, unknown or instrumental") && legend.Contains(ThemesPalette.ToHex(ThemesPalette.Male)) &&
                legend.Contains(ThemesPalette.ToHex(ThemesPalette.Female)) && legend.Contains(ThemesPalette.ToHex(ThemesPalette.Neutral)) &&
                legend.Contains("hollow"));
            report.Check($"{tag}: ground materials use the ThemesGround shader", viewer.Ring.Dial.sharedMaterial.shader.name == ThemesMaterials.GroundShaderName &&
                viewer.Ring.Anchors.All(a => a.Pad.sharedMaterial.shader.name == ThemesMaterials.GroundShaderName));
        }

        static readonly Regex Tags = new("<[^>]*>", RegexOptions.Compiled);

        /// <summary>Rich text without markup, line breaks as spaces (for labels whose text contains no markup characters).</summary>
        static string Plain(string richText) => Tags.Replace(richText, "").Replace('\n', ' ');
        static readonly Regex Allowed = new(@"^[\s·●]*$", RegexOptions.Compiled);

        /// <summary>
        /// The hover card of every song shows only title, artist, year, singer, text source, the three
        /// theme labels and their scores: removing exactly those must leave nothing but separators.
        /// </summary>
        static void ValidateHoverText(Report report, ThemesViewer viewer, string tag)
        {
            ThemesGraphData data = viewer.Data!;
            int bad = 0, badOrder = 0, badBars = 0;
            string firstBad = "";
            Stopwatch clock = Stopwatch.StartNew();
            foreach (ThemeSongNode node in viewer.Nodes)
            {
                viewer.Hud.ShowCard(node, data);
                ThemeSongRecord s = node.Song;
                List<string> strings = viewer.Hud.CardStrings();
                List<(int anchorId, double score)> top = s.TopThemes(3);
                string leftover = Leftover(strings, s, top, data);
                if (!Allowed.IsMatch(leftover))
                {
                    bad++;
                    if (firstBad.Length == 0) firstBad = $"node {s.NodeId}: '{leftover.Trim()}'";
                }
                // Rows: the top three themes, best first, with their scores and proportional bars.
                bool order = strings.Count == 1 + 2 * top.Count;
                for (int i = 0; order && i < top.Count; i++)
                {
                    order &= strings[1 + 2 * i] == GraphHud.Esc(data.Anchor(top[i].anchorId).Label) &&
                             strings[2 + 2 * i] == ThemesPalette.Score(top[i].score) &&
                             (i == 0 || top[i - 1].score >= top[i].score);
                    if (Mathf.Abs(viewer.Hud.RowFillFraction(i) - Mathf.Clamp01((float)top[i].score)) > 1e-4f) badBars++;
                }
                if (!order || top.Count == 0 || top[0].anchorId != s.TopAnchor) badOrder++;
            }
            clock.Stop();
            viewer.Hud.ShowCard(null, data);
            report.Check($"{tag}: hover card holds only title, artist, year, singer, source, theme labels and scores (every song)",
                bad == 0, bad == 0 ? $"{viewer.Nodes.Count} cards" : $"{bad} cards with extra text; first {firstBad}");
            report.Check($"{tag}: hover card lists the top three themes, best first, with their scores", badOrder == 0, $"{badOrder} wrong");
            report.Check($"{tag}: hover card bars are proportional to the scores", badBars == 0, $"{badBars} wrong");
            report.Number($"{tag}.card_build_ms_per_song", clock.Elapsed.TotalMilliseconds / Math.Max(1, viewer.Nodes.Count), "0.000");
        }

        static string Leftover(List<string> strings, ThemeSongRecord s, List<(int anchorId, double score)> top, ThemesGraphData data)
        {
            // Escaped database strings first (they may contain anything), then markup, then fixed words.
            List<string> escaped = new() { GraphHud.Esc(s.Title), GraphHud.Esc(s.Artist) };
            List<string> plain = new() { s.Year.ToString(CultureInfo.InvariantCulture), ThemesPalette.SingerPhrase(s.Gender), ThemesPalette.SourcePhrase(s.FromLyrics) };
            foreach ((int anchorId, double score) in top)
            {
                escaped.Add(GraphHud.Esc(data.Anchor(anchorId).Label));
                plain.Add(ThemesPalette.Score(score));
            }
            StringBuilder all = new();
            foreach (string text in strings)
            {
                string t = text;
                foreach (string e in escaped.OrderByDescending(x => x.Length))
                    if (e.Length > 0) t = t.Replace(e, " ");
                t = Tags.Replace(t, " ");
                foreach (string p in plain.OrderByDescending(x => x.Length))
                    if (p.Length > 0) t = t.Replace(p, " ");
                all.Append(t).Append('\n');
            }
            return all.ToString();
        }

        static void ValidateOverviewLabels(Report report, ThemesViewer viewer, Camera cam, string tag, int width, int height)
        {
            ThemesRing ring = viewer.Ring;
            Vector3 center = cam.WorldToScreenPoint(ring.transform.position);
            int outside = 0, placed = 0, onScreen = 0;
            foreach (ThemeAnchorNode a in ring.Anchors)
            {
                if (a.Label.Placed && a.Label.Visible) placed++;
                Rect r = a.Label.ScreenRect;
                Vector3 sa = cam.WorldToScreenPoint(a.transform.position);
                Vector2 toAnchor = new(sa.x - center.x, sa.y - center.y);
                Vector2 toLabel = r.center - new Vector2(center.x, center.y);
                // The label is beyond its pad, on the outward side, and does not cover the anchor.
                if (Vector2.Dot(toLabel, toAnchor.normalized) > toAnchor.magnitude && !r.Contains(new Vector2(sa.x, sa.y))) outside++;
                if (r.xMin >= 0 && r.yMin >= 0 && r.xMax <= width && r.yMax <= height) onScreen++;
            }
            report.Check($"{tag}: all ten theme labels drawn in the overview", placed == ring.Anchors.Count, $"{placed} placed");
            report.Check($"{tag}: theme labels sit outside the ring, beyond their theme", outside == ring.Anchors.Count, $"{outside}/{ring.Anchors.Count}");
            report.Check($"{tag}: theme labels are fully on screen", onScreen == ring.Anchors.Count, $"{onScreen}/{ring.Anchors.Count}");
            CheckLabelOverlap(report, viewer, $"{tag}: overview");
            report.Number($"{tag}.overview_labels_drawn", viewer.Labels.PlacedCount, "0");
            int pinnedDrawn = viewer.PinnedLabelNodes.Count(x => x.Label != null && x.Label.Placed && x.Label.Visible);
            report.Number($"{tag}.overview_exemplar_labels_drawn", pinnedDrawn, "0");
            report.Check($"{tag}: exemplar songs are labelled (the clearest per theme)", viewer.PinnedLabelNodes.Count > 0 &&
                viewer.PinnedLabelNodes.All(x => x.Label != null && x.Label.Visible), $"{viewer.PinnedLabelNodes.Count} pinned, {pinnedDrawn} drawn");
            Rect legend = ScreenRect(viewer.Hud, "Legend", cam);
            int covered = ring.Anchors.Count(a => a.Label.ScreenRect.Overlaps(legend));
            report.Check($"{tag}: the legend covers no theme label", covered == 0, $"{covered} covered");
        }

        static Rect ScreenRect(ThemesHud hud, string panelName, Camera cam)
        {
            Transform? t = hud.Canvas.transform.Find(panelName);
            if (t == null) return Rect.zero;
            Vector3[] corners = new Vector3[4];
            ((RectTransform)t).GetWorldCorners(corners);
            if (hud.Canvas.renderMode != RenderMode.ScreenSpaceOverlay)
                for (int i = 0; i < 4; i++) corners[i] = cam.WorldToScreenPoint(corners[i]);
            float xMin = corners.Min(c => c.x), yMin = corners.Min(c => c.y);
            return new Rect(xMin, yMin, corners.Max(c => c.x) - xMin, corners.Max(c => c.y) - yMin);
        }

        static void CheckLabelOverlap(Report report, ThemesViewer viewer, string view)
        {
            List<WorldLabel> drawn = viewer.Labels.Labels.Where(l => l.Visible && l.Placed).ToList();
            int overlaps = 0;
            for (int i = 0; i < drawn.Count; i++)
                for (int j = i + 1; j < drawn.Count; j++)
                    if (drawn[i].ScreenRect.Overlaps(drawn[j].ScreenRect)) overlaps++;
            Material? m = viewer.Labels.SharedLabelMaterial;
            report.Check($"{view}: labels draw on top (TMP overlay shader)", m != null && m.shader.name == LabelLayer.OverlayShaderName);
            report.Check($"{view}: drawn labels never overlap", overlaps == 0, $"{drawn.Count} labels, {overlaps} overlapping pairs");
        }

        static void ValidatePicking(Report report, ThemesViewer viewer, Camera cam, string tag, string outDir, int width, int height)
        {
            Measure(cam, viewer, width, height, () =>
            {
                int exact = 0, near = 0;
                Stopwatch clock = Stopwatch.StartNew();
                foreach (ThemeSongNode node in viewer.Nodes)
                {
                    Vector3 sp = cam.WorldToScreenPoint(node.transform.position);
                    ThemeSongNode? hit = viewer.Pick(cam, new Vector2(sp.x, sp.y));
                    if (hit == node) exact++;
                    if (hit != null && (hit.transform.position - node.transform.position).magnitude < node.Radius * 2.2f) near++;
                }
                clock.Stop();
                int n = viewer.Nodes.Count;
                report.Number($"{tag}.pick_ms_per_call", clock.Elapsed.TotalMilliseconds / Math.Max(1, n), "0.000");
                report.Check($"{tag}: picking a bubble's centre finds it (or the bubble drawn over it)",
                    exact >= .9 * n && near == n, $"{exact}/{n} exact, {near}/{n} it or an overlapping neighbour");
                report.Check($"{tag}: picking empty space finds nothing", viewer.Pick(cam, new Vector2(3, 3)) == null);
            });

            // Hover the song nearest the centre of its theme's cluster with a lyrics source, prefer a known title.
            ThemeSongNode focus = viewer.Nodes.FirstOrDefault(x => x.Song.Title == "The Man I Love")
                                  ?? viewer.Nodes.Where(x => x.Song.FromLyrics).OrderByDescending(x => x.Song.TopScore).ThenBy(x => x.NodeId).FirstOrDefault()
                                  ?? viewer.Nodes[0];
            viewer.SetHover(focus);
            List<(int anchorId, double score)> top = focus.Song.TopThemes(3);
            report.Check($"{tag}: hover glows the song and shows its card", focus.State == ThemeBubbleState.Hover && viewer.Hud.CardVisible &&
                viewer.Hud.CardSong == focus && viewer.Hud.CardStrings()[0].Contains(GraphHud.Esc(focus.Song.Title)));
            bool tethers = true;
            for (int i = 0; i < viewer.Tethers.Count; i++)
            {
                LineRenderer line = viewer.Tethers[i];
                bool expected = i < top.Count && top[i].score > 1e-3;
                tethers &= line.enabled == expected;
                if (expected)
                    tethers &= (line.GetPosition(0) - focus.transform.position).magnitude < 1e-4f &&
                               (line.GetPosition(1) - viewer.Data!.Anchor(top[i].anchorId).Position).magnitude < 1e-4f;
            }
            report.Check($"{tag}: hover tethers the song to its top themes", tethers);
            report.Check($"{tag}: hover lights its top themes", viewer.Ring.Anchors.All(a =>
                (a.Look == ThemeAnchorNode.AnchorLook.Lit) == top.Any(t => t.anchorId == a.Anchor.AnchorId)));
            report.Check($"{tag}: hover labels the song", focus.Label != null && focus.Label.Visible);
            // Frame the song and a little of its neighbourhood for the screenshot.
            ThemesOrbitCamera orbit = cam.GetComponent<ThemesOrbitCamera>();
            Vector3 p = focus.transform.position;
            orbit.SetView(p + new Vector3(0f, 0f, 3f), viewer.Data!.RingRadius * 1.15f, viewer.OverviewYaw, 58f, immediate: true);
            string hover = Path.Combine(outDir, $"themes_hover_{tag}.png");
            Capture(cam, viewer, hover, width, height, out _);
            report.Check($"{tag}: hover PNG written", File.Exists(hover), $"'{focus.Song.Title}' ({focus.Song.Artist}, {focus.Song.Year})");
            Measure(cam, viewer, width, height, () =>
            {
                report.Check($"{tag}: hover card sits next to its song, on screen", CardNextToSong(viewer, cam, width, height));
                CheckLabelOverlap(report, viewer, $"{tag}: hover");
            });
            report.Text($"{tag}.hover_example", $"{focus.Song.Title} — {focus.Song.Artist} ({focus.Song.Year}): " +
                string.Join(", ", top.Select(t => $"{viewer.Data.Anchor(t.anchorId).Short} {ThemesPalette.Score(t.score)}")));
            viewer.SetHover(null);
            report.Check($"{tag}: hover cleared: card hidden, tethers off", !viewer.Hud.CardVisible && viewer.Tethers.All(l => !l.enabled) &&
                focus.State == ThemeBubbleState.Normal);
            viewer.FrameOverview(cam);
        }

        static bool CardNextToSong(ThemesViewer viewer, Camera cam, int width, int height)
        {
            ThemeSongNode? song = viewer.Hud.CardSong;
            if (song == null) return false;
            Vector3[] corners = new Vector3[4];
            viewer.Hud.CardRect.GetWorldCorners(corners);
            for (int i = 0; i < 4; i++) corners[i] = cam.WorldToScreenPoint(corners[i]);
            Rect card = Rect.MinMaxRect(corners.Min(c => c.x), corners.Min(c => c.y), corners.Max(c => c.x), corners.Max(c => c.y));
            Vector3 sp = cam.WorldToScreenPoint(song.transform.position);
            bool onScreen = card.xMin >= -1 && card.yMin >= -1 && card.xMax <= width + 1 && card.yMax <= height + 1;
            float gap = Mathf.Min(Mathf.Abs(card.xMin - sp.x), Mathf.Abs(card.xMax - sp.x));
            return onScreen && !card.Contains(new Vector2(sp.x, sp.y)) && gap < 120f && card.width > 100f;
        }

        /// <summary>Runs <paramref name="measure"/> with <paramref name="cam"/> (and the HUD) rendering to a width x height target, labels refreshed.</summary>
        static void Measure(Camera cam, ThemesViewer viewer, int width, int height, Action measure)
        {
            RenderTexture rt = new(width, height, 24) { name = "Themes Validation Measure" };
            rt.Create();
            RenderTexture? previous = cam.targetTexture;
            try
            {
                cam.targetTexture = rt;
                cam.aspect = (float)width / height;
                viewer.Hud.RenderInto(cam);
                viewer.RefreshView(cam);
                measure();
            }
            finally
            {
                viewer.Hud.RenderInto(null);
                cam.targetTexture = previous;
                rt.Release();
                Object.DestroyImmediate(rt);
            }
        }

        /// <summary>A recording <see cref="ISongPlayer"/>: what the viewer asks the synth to do.</summary>
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
            public bool ApplesAtPlay;
            public event Action<SongClip>? Started;
            public event Action<SongClip>? Finished;

            public void Play(SongClip clip, SongClip previous)
            {
                Calls.Add($"play {clip.NodeId} after {(previous != null ? previous.NodeId.ToString(CultureInfo.InvariantCulture) : "none")}");
                Clip = clip;
                Previous = previous;
                ApplesAtPlay = ApplesToApples;
                IsPlaying = true;
                CurrentBeat = clip.ExcerptStartBeat;
                CurrentPlan = ApplesToApples || previous == null ? MorphPlan.None : Morph.Plan(previous, clip, MorphBars);
                Started?.Invoke(clip);
            }

            public void Stop()
            {
                Calls.Add("stop");
                IsPlaying = false;
            }

            public void Finish()
            {
                if (Clip == null) return;
                IsPlaying = false;
                CurrentBeat = Clip.ExcerptEndBeat;
                Finished?.Invoke(Clip);
            }
        }

        static void ValidatePlayback(Report report, ThemesViewer viewer, string tag)
        {
            ThemesGraphData data = viewer.Data!;
            List<ThemeSongNode> playable = viewer.Nodes.Where(x => x.Song.MidiPath != null && File.Exists(x.Song.MidiPath) &&
                                                                  x.Song.ExcerptEndBeat > x.Song.ExcerptStartBeat).ToList();
            report.Number($"{tag}.songs_with_midi", playable.Count, "0");
            report.Check($"{tag}: MIDI paths resolve against the DB folder", viewer.Nodes.All(x => x.Song.MidiPath == null ||
                x.Song.MidiPath.StartsWith(Path.GetFullPath(Path.Combine(data.DbFolder, "..")), StringComparison.OrdinalIgnoreCase)));
            report.Text($"{tag}.audio_player_type_present", SongPlayerDiscovery.FindAudioPlayerType()?.FullName ?? "no");
            if (!report.Check($"{tag}: at least two songs have a MIDI file", playable.Count >= 2, $"{playable.Count}")) return;

            ProbePlayer probe = new();
            viewer.UsePlayer(probe, "validation probe");
            ThemeSongNode a = playable[0], b = playable[playable.Count / 2];
            bool started = viewer.TogglePlay(a);
            SongClip? clip = probe.Clip;
            ThemeSongRecord s = a.Song;
            report.Check($"{tag}: click plays the excerpt natively (no previous clip, no morph, native file)",
                started && probe.Calls.SequenceEqual(new[] { $"play {s.NodeId} after none" }) && probe.Previous == null && !probe.ApplesAtPlay &&
                probe.CurrentPlan.MorphBeats == 0 && viewer.Playing == a, string.Join("; ", probe.Calls));
            report.Check($"{tag}: the clip carries the song's MIDI, excerpt, key and tempo", clip != null &&
                clip.MidiPath == s.MidiPath && clip.ExcerptStartBeat == s.ExcerptStartBeat && clip.ExcerptEndBeat == s.ExcerptEndBeat &&
                clip.TonicPc == s.TonicPc && clip.Minor == s.Minor && clip.NativeBpm == s.NativeBpm && clip.BeatsPerBar == s.BeatsPerBar &&
                clip.NormalizedMidiPath == null, clip != null ? $"{Path.GetFileName(Path.GetDirectoryName(clip.MidiPath))}, beats {clip.ExcerptStartBeat}–{clip.ExcerptEndBeat}, {clip.NativeBpm} BPM" : "no clip");
            viewer.RefreshStatus(force: true);
            report.Check($"{tag}: playing song glows and the status line names it", a.State == ThemeBubbleState.Playing &&
                viewer.Hud.StatusText.Contains(GraphHud.Esc(s.Title)) && a.Label != null && a.Label.Visible);
            viewer.TogglePlay(a);
            report.Check($"{tag}: a second click stops it", probe.Calls.Count == 2 && probe.Calls[1] == "stop" && viewer.Playing == null &&
                a.State == ThemeBubbleState.Normal, string.Join("; ", probe.Calls));
            viewer.TogglePlay(a);
            viewer.TogglePlay(b);
            report.Check($"{tag}: clicking another song switches at once (stop, then play it natively)",
                probe.Calls.Skip(2).SequenceEqual(new[] { $"play {a.NodeId} after none", "stop", $"play {b.NodeId} after none" }) && viewer.Playing == b,
                string.Join("; ", probe.Calls.Skip(2)));
            probe.Finish();
            report.Check($"{tag}: the excerpt's end clears the playing state", viewer.Playing == null && b.State == ThemeBubbleState.Normal);

            ThemeSongRecord missing = a.Song;
            string? saved = missing.MidiPath;
            missing.MidiPath = Path.Combine(data.DbFolder, "missing-for-validation.mid");
            int calls = probe.Calls.Count;
            bool played = viewer.TogglePlay(a);
            missing.MidiPath = saved;
            viewer.RefreshStatus(force: true);
            report.Check($"{tag}: a missing MIDI file plays nothing and says so", !played && probe.Calls.Count == calls &&
                viewer.Message != null && viewer.Message.Contains("No MIDI file") && viewer.Hud.StatusText.Contains("No MIDI file"));

            // The silent clock (no synth installed) plays natively too.
            GameObject host = new("Silent Probe Host") { hideFlags = HideFlags.DontSave };
            try
            {
                SilentSongPlayer silent = host.AddComponent<SilentSongPlayer>();
                viewer.UsePlayer(silent, "silent");
                viewer.TogglePlay(a);
                report.Check($"{tag}: silent player: native BPM, no transposition", silent.IsPlaying && silent.CurrentPlan.MorphBeats == 0 &&
                    Math.Abs(silent.CurrentBpm - a.Song.NativeBpm) < 1e-9 && Math.Abs(silent.CurrentSemitones) < 1e-12);
                viewer.StopPlayback();
            }
            finally
            {
                Object.DestroyImmediate(host);
            }
            viewer.UsePlayer(probe, "validation probe");
        }

        static void ValidateFilterAndLabels(Report report, ThemesViewer viewer, Camera cam, string tag, string outDir, int width, int height)
        {
            ThemesGraphData data = viewer.Data!;
            int theme = data.Anchors.OrderByDescending(a => a.TopCount).ThenBy(a => a.AnchorId).First().AnchorId;
            viewer.SetFilter(theme);
            bool states = viewer.Nodes.All(x => x.State == (x.Song.TopAnchor == theme ? ThemeBubbleState.Normal : ThemeBubbleState.Dimmed));
            bool pads = viewer.Ring.Anchors.All(a => a.Look == (a.Anchor.AnchorId == theme ? ThemeAnchorNode.AnchorLook.Lit : ThemeAnchorNode.AnchorLook.Faded));
            viewer.RefreshStatus(force: true);
            report.Check($"{tag}: key {ThemeAnchorNode.KeyName(theme)} shows only that theme's songs", states && pads &&
                viewer.Hud.StatusText.Contains(GraphHud.Esc(data.Anchor(theme).Label)), $"theme {theme}, {data.Anchor(theme).TopCount} songs");
            string filtered = Path.Combine(outDir, $"themes_filter_{tag}.png");
            Capture(cam, viewer, filtered, width, height, out _);
            Measure(cam, viewer, width, height, () =>
            {
                int picked = 0;
                foreach (ThemeSongNode other in viewer.Nodes.Where(x => x.Song.TopAnchor != theme))
                {
                    Vector3 sp = cam.WorldToScreenPoint(other.transform.position);
                    ThemeSongNode? hit = viewer.Pick(cam, new Vector2(sp.x, sp.y));
                    if (hit != null && hit.Song.TopAnchor != theme) picked++;
                }
                report.Check($"{tag}: filtered-out songs are not picked", picked == 0, $"{picked} picked");
                CheckLabelOverlap(report, viewer, $"{tag}: filter");
                Rect status = ScreenRect(viewer.Hud, "Status", cam);
                int covered = viewer.Ring.Anchors.Count(a => a.Label.ScreenRect.Overlaps(status));
                report.Check($"{tag}: the status line covers no theme label", status.width > 0 && covered == 0, $"{covered} covered");
            });
            report.Check($"{tag}: filter labels its clearest songs", viewer.Exemplars(theme, viewer.FilterLabels).All(x => x.Label != null && x.Label.Visible));
            viewer.SetFilter(theme);
            report.Check($"{tag}: the same key again shows every theme", viewer.FilterAnchor == 0 &&
                viewer.Nodes.All(x => x.State == ThemeBubbleState.Normal) && viewer.Ring.Anchors.All(a => a.Look == ThemeAnchorNode.AnchorLook.Normal));

            viewer.SetAllLabels(true);
            bool all = viewer.Nodes.All(x => x.Label != null && x.Label.Visible);
            Capture(cam, viewer, Path.Combine(outDir, $"themes_all_labels_{tag}.png"), width, height, out _);
            Measure(cam, viewer, width, height, () =>
            {
                CheckLabelOverlap(report, viewer, $"{tag}: all labels");
                report.Number($"{tag}.all_labels_drawn", viewer.Labels.PlacedCount, "0");
            });
            viewer.SetAllLabels(false);
            bool back = viewer.Nodes.All(x => x.Label == null || x.Label.Visible == (x.LabelPinned));
            report.Check($"{tag}: L labels every song, and again only the exemplars", all && back);
            report.Check($"{tag}: theme labels stay visible with every label on", viewer.Ring.Anchors.All(a => a.Label.Visible));
        }

        static void ValidateOrbitCamera(Report report, Camera cam)
        {
            ThemesOrbitCamera orbit = cam.GetComponent<ThemesOrbitCamera>();
            orbit.SetView(new Vector3(3f, 0f, -2f), 50f, 30f, 45f, immediate: true);
            Vector3 f = cam.transform.forward;
            bool looks = (cam.transform.position + f * 50f - new Vector3(3f, 0f, -2f)).magnitude < 1e-3f;
            orbit.AdoptTransform(immediate: true);
            bool round = (orbit.Pivot - new Vector3(3f, 0f, -2f)).magnitude < 1e-3f && Mathf.Abs(orbit.Distance - 50f) < 1e-3f &&
                         Mathf.Abs(Mathf.DeltaAngle(orbit.Yaw, 30f)) < 1e-3f && Mathf.Abs(orbit.Pitch - 45f) < 1e-3f;
            orbit.SetView(Vector3.zero, 1e6f, 0f, 120f, immediate: true);
            bool clamped = orbit.Distance <= orbit.MaxDistance && orbit.Pitch <= orbit.MaxPitch;
            report.Check("orbit camera: pivot/distance/yaw/pitch pose, round trip and clamps", looks && round && clamped);
        }

        // ------------------------------------------------------------------ rendering

        struct RenderStats
        {
            public long DrawCalls;
            public long SetPass;
            public long Batches;
        }

        /// <summary>Renders <paramref name="cam"/> (with the HUD) at width x height; writes a PNG when <paramref name="path"/> is set. Returns ms.</summary>
        static double Capture(Camera cam, ThemesViewer viewer, string? path, int width, int height, out RenderStats stats)
        {
            RenderTexture rt = new(width, height, 24, RenderTextureFormat.ARGB32) { antiAliasing = 4, name = "Themes Validation Capture" };
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
                viewer.Hud.RenderInto(cam);
                viewer.RefreshView(cam);
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
                viewer.Hud.RenderInto(null);
                cam.targetTexture = previousTarget;
                RenderTexture.active = previousActive;
                rt.Release();
                Object.DestroyImmediate(rt);
                Object.DestroyImmediate(texture);
            }
        }

        // ------------------------------------------------------------------ helpers

        static int Count(SqliteConnection conn, string sql)
        {
            using SqliteCommand cmd = conn.CreateCommand();
            cmd.CommandText = sql;
            return Convert.ToInt32(cmd.ExecuteScalar(), CultureInfo.InvariantCulture);
        }

        static bool Same(Color a, Color b) => Mathf.Abs(a.r - b.r) < 2e-3f && Mathf.Abs(a.g - b.g) < 2e-3f && Mathf.Abs(a.b - b.b) < 2e-3f;

        static float HueDistance(Color a, Color b)
        {
            Color.RGBToHSV(a, out float ha, out _, out _);
            Color.RGBToHSV(b, out float hb, out _, out _);
            float d = Mathf.Abs(ha - hb);
            return Mathf.Min(d, 1f - d);
        }

        static float Saturation(Color c)
        {
            Color.RGBToHSV(c, out _, out float s, out _);
            return s;
        }

        static double Wrap180(double degrees) => ((degrees % 360 + 540) % 360) - 180;
        static double Wrap360(double degrees) => (degrees % 360 + 360) % 360;
        static bool IsFinite(Vector3 v) => !(float.IsNaN(v.x) || float.IsNaN(v.y) || float.IsNaN(v.z) || float.IsInfinity(v.x) || float.IsInfinity(v.y) || float.IsInfinity(v.z));

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
            return value == null ? null : ThemesGraphReader.ResolveUserPath(value);
        }

        static bool PathsEqual(string a, string b) =>
            string.Equals(Path.GetFullPath(a), Path.GetFullPath(b), StringComparison.OrdinalIgnoreCase);

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

    /// <summary>
    /// Play-mode run of the LyricThemes scene, for
    /// <c>Unity.exe -batchmode -projectPath unity -executeMethod MusicHistory.EditorTools.ThemesPlayModeBench.Run -logFile ...</c>
    /// (no -quit, no -nographics). Enters play mode so the scene builds itself (ThemesViewer.Start),
    /// then measures the player loop and a full 1920x1080 MSAA render per frame while idle, while the
    /// hover moves to another song every 5 frames (picking included) and with every song labelled;
    /// plays a song through the discovered ISongPlayer (the synth when it is installed) and stops it
    /// with a second click; finally loads the SongInfluenceGraph scene the way the back button does.
    /// Writes themes_playmode_bench.json to data/screens (or -validationOut); exit code 0 when every check passes.
    /// </summary>
    [InitializeOnLoad]
    public static class ThemesPlayModeBench
    {
        const string ActiveKey = "MusicHistory.ThemesPlayModeBench.Active";
        const int Width = 1920, Height = 1080;

        enum Phase { Waiting, Warmup, Idle, Hover, AllLabels, Play, Back, Done }

        static Phase phase = Phase.Waiting;
        static int phaseFrame;
        static int waitTicks;
        static double phaseStarted;
        static ProfilerRecorder playerLoop;
        static ProfilerRecorder gcAlloc;
        static RenderTexture? target;
        static Texture2D? probe;
        static readonly Dictionary<Phase, List<double>> loopMs = new();
        static readonly Dictionary<Phase, List<double>> renderMs = new();
        static readonly Dictionary<Phase, List<double>> gcBytes = new();
        static readonly List<double> hoverMs = new();
        static readonly List<(string name, bool ok, string detail)> checks = new();
        static ThemeSongNode? played;
        static double playStartBeat;
        static int exitCode = -1;

        static ThemesPlayModeBench()
        {
            if (!SessionState.GetBool(ActiveKey, false)) return;
            EditorApplication.update -= Tick;
            EditorApplication.update += Tick;
        }

        public static void Run()
        {
            EditorSceneManager.OpenScene(ThemesValidation.ScenePath, OpenSceneMode.Single);
            SessionState.SetBool(ActiveKey, true);
            EditorApplication.update -= Tick;
            EditorApplication.update += Tick;
            EditorApplication.EnterPlaymode();
        }

        static double Now => EditorApplication.timeSinceStartup;

        static void Tick()
        {
            if (exitCode >= 0)
            {
                if (EditorApplication.isPlaying) return;
                SessionState.SetBool(ActiveKey, false);
                EditorApplication.update -= Tick;
                EditorApplication.Exit(exitCode);
                return;
            }
            if (!EditorApplication.isPlaying)
            {
                if (++waitTicks > 200000) Finish("play mode never started");
                return;
            }
            if (phase == Phase.Back)
            {
                BackPhase();
                return;
            }
            ThemesViewer? viewer = Object.FindAnyObjectByType<ThemesViewer>();
            Camera? cam = Camera.main;
            if (viewer == null || cam == null || viewer.Data == null || viewer.Nodes.Count == 0)
            {
                if (++waitTicks > 400000) Finish("the themes scene was not built in play mode");
                return;
            }

            if (phase == Phase.Waiting)
            {
                playerLoop = ProfilerRecorder.StartNew(ProfilerCategory.Internal, "PlayerLoop");
                gcAlloc = ProfilerRecorder.StartNew(ProfilerCategory.Memory, "GC Allocated In Frame");
                target = new RenderTexture(Width, Height, 24, RenderTextureFormat.ARGB32) { antiAliasing = 4 };
                target.Create();
                probe = new Texture2D(1, 1, TextureFormat.RGB24, false);
                cam.aspect = (float)Width / Height;
                viewer.FrameOverview(cam);
                Check("scene built itself in play mode (ThemesViewer.Start)", true,
                    $"{viewer.Nodes.Count} songs, {viewer.BuildMilliseconds:0} ms, db {Path.GetFileName(viewer.ResolvedDbPath)} [{viewer.DbPathReason}], positions {viewer.PositionSource}");
                Type? audio = SongPlayerDiscovery.FindAudioPlayerType();
                Check("the viewer discovered its player at start", viewer.Player != null &&
                    (audio == null ? viewer.Player is SilentSongPlayer : viewer.Player.GetType() == audio), viewer.PlayerDescription);
                Enter(Phase.Warmup);
                return;
            }

            Sample(cam);
            phaseFrame++;
            switch (phase)
            {
                case Phase.Warmup when phaseFrame >= 60:
                    Enter(Phase.Idle);
                    break;
                case Phase.Idle when phaseFrame >= 300:
                    Enter(Phase.Hover);
                    break;
                case Phase.Hover:
                    if (phaseFrame % 5 == 0)
                    {
                        ThemeSongNode node = viewer.Nodes[(phaseFrame * 7919) % viewer.Nodes.Count];
                        Vector3 sp = cam.WorldToScreenPoint(node.transform.position);
                        Stopwatch sw = Stopwatch.StartNew();
                        ThemeSongNode? hit = viewer.Pick(cam, new Vector2(sp.x, sp.y));
                        viewer.SetHover(hit);
                        hoverMs.Add(sw.Elapsed.TotalMilliseconds);
                    }
                    if (phaseFrame >= 300)
                    {
                        viewer.SetHover(null);
                        Check("hover churn ends with the card hidden and tethers off", !viewer.Hud.CardVisible && viewer.Tethers.All(t => !t.enabled));
                        viewer.SetAllLabels(true);
                        Enter(Phase.AllLabels);
                    }
                    break;
                case Phase.AllLabels when phaseFrame >= 120:
                    viewer.SetAllLabels(false);
                    played = viewer.Nodes.FirstOrDefault(x => x.Song.MidiPath != null && File.Exists(x.Song.MidiPath) &&
                                                               x.Song.ExcerptEndBeat > x.Song.ExcerptStartBeat);
                    if (played == null)
                    {
                        Check("a song with a MIDI file to play", false);
                        Enter(Phase.Back);
                        viewer.GoToInfluenceGraph();
                        break;
                    }
                    bool started = viewer.TogglePlay(played);
                    playStartBeat = played.Song.ExcerptStartBeat;
                    Check("click starts the song's excerpt", started && viewer.Playing == played && viewer.Player!.IsPlaying,
                        $"'{played.Song.Title}' via {viewer.PlayerDescription}");
                    phaseStarted = Now;
                    Enter(Phase.Play);
                    break;
                case Phase.Play:
                    ISongPlayer player = viewer.Player!;
                    bool moving = player.CurrentBeat > playStartBeat + 1.0;
                    if (!moving && Now - phaseStarted < 90) break;
                    Check("the excerpt plays: the player's beat clock advances (SoundFont loaded within 90 s)", moving,
                        $"beat {player.CurrentBeat:0.00} (excerpt {played!.Song.ExcerptStartBeat}–{played.Song.ExcerptEndBeat}) after {Now - phaseStarted:0.0} s");
                    Check("it plays natively: no transposition, no morph", Math.Abs(player.CurrentSemitones) < 1e-6 && player.CurrentPlan.MorphBeats == 0 &&
                        !player.ApplesToApples, $"{player.CurrentSemitones:0.000} st, {player.CurrentBpm:0.0} BPM (median {played.Song.NativeBpm:0.0})");
                    viewer.RefreshStatus(force: true);
                    Check("the status line shows the playing song", viewer.Hud.StatusText.Contains(GraphHud.Esc(played.Song.Title)));
                    viewer.TogglePlay(played);
                    Check("a second click stops it", !player.IsPlaying && viewer.Playing == null);
                    Enter(Phase.Back);
                    phaseStarted = Now;
                    viewer.GoToInfluenceGraph();
                    break;
            }
        }

        static void BackPhase()
        {
            SongGraphLoader? loader = Object.FindAnyObjectByType<SongGraphLoader>();
            if (loader != null && loader.Data != null && loader.Nodes.Count > 0)
            {
                Check("G / the back button loads the SongInfluenceGraph scene", Object.FindAnyObjectByType<ThemesViewer>() == null,
                    $"{loader.Nodes.Count} songs built in {loader.BuildMilliseconds:0} ms");
                Finish(null);
                return;
            }
            if (Now - phaseStarted > 60) Finish("the SongInfluenceGraph scene did not load within 60 s");
        }

        static void Enter(Phase next)
        {
            phase = next;
            phaseFrame = 0;
        }

        static void Sample(Camera cam)
        {
            if (!loopMs.ContainsKey(phase))
            {
                loopMs[phase] = new List<double>();
                renderMs[phase] = new List<double>();
                gcBytes[phase] = new List<double>();
            }
            if (playerLoop.Valid && playerLoop.LastValue > 0) loopMs[phase].Add(playerLoop.LastValue / 1e6);
            if (gcAlloc.Valid) gcBytes[phase].Add(gcAlloc.LastValue);
            Stopwatch sw = Stopwatch.StartNew();
            RenderTexture? previous = cam.targetTexture;
            cam.targetTexture = target;
            cam.Render();
            RenderTexture.active = target;
            probe!.ReadPixels(new Rect(0, 0, 1, 1), 0, 0, false);
            RenderTexture.active = null;
            cam.targetTexture = previous;
            renderMs[phase].Add(sw.Elapsed.TotalMilliseconds);
        }

        static void Check(string name, bool ok, string detail = "")
        {
            checks.Add((name, ok, detail));
            Debug.Log($"[themes-bench] {(ok ? "PASS" : "FAIL")} {name}{(detail.Length > 0 ? " — " + detail : "")}");
        }

        static void Finish(string? failure)
        {
            if (failure != null) Check(failure, false);
            StringBuilder json = new();
            json.Append("{\n  \"graphics_device\": \"").Append(SystemInfo.graphicsDeviceName).Append("\",\n  \"phases\": {");
            List<string> phases = new();
            foreach (Phase p in new[] { Phase.Idle, Phase.Hover, Phase.AllLabels, Phase.Play })
            {
                if (!loopMs.ContainsKey(p)) continue;
                phases.Add($"\n    \"{p.ToString().ToLowerInvariant()}\": {{\"frames\": {renderMs[p].Count}, " +
                           $"\"player_loop_ms\": {Stats(loopMs[p])}, \"render_ms\": {Stats(renderMs[p])}, " +
                           $"\"gc_bytes_per_frame\": {Stats(gcBytes[p])}}}");
                Debug.Log($"[themes-bench] {p}: player loop {Summary(loopMs[p])} ms, render+sync {Summary(renderMs[p])} ms, GC {Summary(gcBytes[p])} B/frame");
            }
            json.Append(string.Join(",", phases)).Append("\n  },\n");
            json.Append($"  \"hover_pick_and_apply_ms\": {Stats(hoverMs)},\n");
            Debug.Log($"[themes-bench] hover (pick + apply states, ~1000 bubbles): {Summary(hoverMs)} ms");
            json.Append("  \"checks\": [").Append(string.Join(",", checks.Select(c =>
                $"\n    {{\"name\": \"{c.name.Replace("\"", "'")}\", \"ok\": {(c.ok ? "true" : "false")}, \"detail\": \"{c.detail.Replace("\"", "'").Replace("\\", "/")}\"}}")));
            json.Append("\n  ]\n}\n");
            string dir = ArgValue("-validationOut") ?? Path.Combine(ThemesGraphReader.RepoRoot(), "data", "screens");
            Directory.CreateDirectory(dir);
            File.WriteAllText(Path.Combine(dir, "themes_playmode_bench.json"), json.ToString(), new UTF8Encoding(false));
            int failures = checks.Count(c => !c.ok);
            Debug.Log($"[themes-bench] {checks.Count - failures}/{checks.Count} checks passed");
            exitCode = failures == 0 ? 0 : 1;
            playerLoop.Dispose();
            gcAlloc.Dispose();
            if (target != null) target.Release();
            phase = Phase.Done;
            EditorApplication.ExitPlaymode();
        }

        static string? ArgValue(string name)
        {
            string[] args = Environment.GetCommandLineArgs();
            for (int i = 0; i + 1 < args.Length; i++)
                if (string.Equals(args[i], name, StringComparison.OrdinalIgnoreCase)) return args[i + 1];
            return null;
        }

        static string Stats(List<double> values)
        {
            if (values.Count == 0) return "null";
            List<double> sorted = values.OrderBy(v => v).ToList();
            double p95 = sorted[Math.Min(sorted.Count - 1, (int)Math.Ceiling(sorted.Count * .95) - 1)];
            return string.Format(CultureInfo.InvariantCulture, "{{\"mean\": {0:0.###}, \"median\": {1:0.###}, \"p95\": {2:0.###}, \"max\": {3:0.###}}}",
                values.Average(), sorted[sorted.Count / 2], p95, sorted[^1]);
        }

        static string Summary(List<double> values)
        {
            if (values.Count == 0) return "n/a";
            List<double> sorted = values.OrderBy(v => v).ToList();
            double p95 = sorted[Math.Min(sorted.Count - 1, (int)Math.Ceiling(sorted.Count * .95) - 1)];
            return string.Format(CultureInfo.InvariantCulture, "mean {0:0.00} / median {1:0.00} / p95 {2:0.00} / max {3:0.00}",
                values.Average(), sorted[sorted.Count / 2], p95, sorted[^1]);
        }
    }
}
