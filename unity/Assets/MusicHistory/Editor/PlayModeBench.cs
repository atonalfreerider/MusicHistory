#nullable enable
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
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
    /// Play-mode performance run, for
    /// <c>Unity.exe -batchmode -projectPath unity -executeMethod MusicHistory.EditorTools.PlayModeBench.Run -musicHistoryDb &lt;db&gt; -logFile ...</c>
    /// (no -quit, no -nographics). Enters play mode so the scene builds itself through
    /// SongGraphLoader.Start, then measures the player loop and a full camera render per frame
    /// in four phases: idle overview, hover churn (a new focus every 10 frames), a running
    /// lineage tour (time scale 30, so excerpts finish), and the live axis-locked simulation.
    /// Writes data/screens/playmode_bench.json; exit code 0 when the checks pass.
    /// </summary>
    [InitializeOnLoad]
    public static class PlayModeBench
    {
        const string ActiveKey = "MusicHistory.PlayModeBench.Active";
        const string ScenePath = "Assets/Scenes/SongInfluenceGraph.unity";
        const int Width = 1920, Height = 1080;

        enum Phase { Waiting, Warmup, Idle, Hover, Tour, Simulation, Done }

        static Phase phase = Phase.Waiting;
        static int phaseFrame;
        static int waitTicks;
        static ProfilerRecorder playerLoop;
        static ProfilerRecorder gcAlloc;
        static RenderTexture? target;
        static Texture2D? probe;
        static readonly Dictionary<Phase, List<double>> loopMs = new();
        static readonly Dictionary<Phase, List<double>> renderMs = new();
        static readonly Dictionary<Phase, List<double>> gcBytes = new();
        static readonly List<double> focusMs = new();
        static readonly List<(string name, bool ok, string detail)> checks = new();
        static float[]? timeCoordinates;
        static int tourStartStep;
        static int exitCode = -1;

        static PlayModeBench()
        {
            if (!SessionState.GetBool(ActiveKey, false)) return;
            EditorApplication.update -= Tick;
            EditorApplication.update += Tick;
        }

        public static void Run()
        {
            EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
            SessionState.SetBool(ActiveKey, true);
            EditorApplication.update -= Tick;
            EditorApplication.update += Tick;
            EditorApplication.EnterPlaymode();
        }

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
            SongGraphLoader? loader = Object.FindAnyObjectByType<SongGraphLoader>();
            Camera? cam = Camera.main;
            if (loader == null || cam == null || loader.Data == null || loader.Nodes.Count == 0)
            {
                if (++waitTicks > 400000) Finish("graph was not built in play mode");
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
                loader.FrameOverview(cam);
                Check("scene built itself in play mode (SongGraphLoader.Start)", true,
                    $"{loader.Nodes.Count} songs, {loader.Edges.Count} edges, {loader.BuildMilliseconds:0} ms, db {Path.GetFileName(loader.ResolvedDbPath)} [{loader.DbPathReason}]");
                Enter(Phase.Warmup);
                return;
            }

            Sample(loader, cam);
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
                    if (phaseFrame % 10 == 0)
                    {
                        SongNode node = loader.Nodes[(phaseFrame * 7919) % loader.Nodes.Count];
                        Stopwatch sw = Stopwatch.StartNew();
                        loader.Highlighter.ApplyFocus(node);
                        focusMs.Add(sw.Elapsed.TotalMilliseconds);
                    }
                    if (phaseFrame >= 300)
                    {
                        loader.Highlighter.ApplyFocus(null, force: true);
                        Check("hover churn leaves no secondary edge visible", loader.Edges.Where(e => !e.IsTree).All(e => !e.Shown));
                        Time.timeScale = 30f;
                        SongNode deepest = loader.NodeById(TourPlanner.DefaultStart(loader.Data, TourMode.Lineage));
                        Check("tour starts in play mode", loader.Director.StartTour(TourMode.Lineage, deepest));
                        tourStartStep = loader.Director.StepIndex;
                        Enter(Phase.Tour);
                    }
                    break;
                case Phase.Tour when phaseFrame >= 600 || loader.Director.TourComplete:
                    int advanced = loader.Director.StepIndex - tourStartStep;
                    Check("tour advances on the player's Finished events", advanced >= 2 || loader.Director.TourComplete,
                        $"{advanced} steps in {phaseFrame} frames at time scale 30; player {loader.Director.PlayerDescription}");
                    Capture(loader, cam, "playmode_tour.png");
                    loader.Director.Exit();
                    Time.timeScale = 1f;
                    timeCoordinates = loader.Nodes.Select(n => Vector3.Dot(n.transform.position, loader.Frame.TimeDir)).ToArray();
                    loader.ToggleSimulation();
                    Check("F starts the live simulation", loader.Simulation.IsRunning);
                    Enter(Phase.Simulation);
                    break;
                case Phase.Simulation when phaseFrame >= 120:
                    loader.ToggleSimulation();
                    float drift = 0;
                    for (int i = 0; i < loader.Nodes.Count; i++)
                        drift = Mathf.Max(drift, Mathf.Abs(Vector3.Dot(loader.Nodes[i].transform.position, loader.Frame.TimeDir) - timeCoordinates![i]));
                    Check("120 live-simulation frames keep every song on its date", drift < 1e-3f, $"max drift {drift:0.000000} units");
                    Capture(loader, cam, "playmode_simulation.png");
                    Finish(null);
                    break;
            }
        }

        static void Enter(Phase next)
        {
            phase = next;
            phaseFrame = 0;
        }

        static void Sample(SongGraphLoader loader, Camera cam)
        {
            if (!loopMs.ContainsKey(phase))
            {
                loopMs[phase] = new List<double>();
                renderMs[phase] = new List<double>();
                gcBytes[phase] = new List<double>();
            }
            // Previous frame's player loop (scripts, animation, the silent clock, labels, timeline).
            if (playerLoop.Valid && playerLoop.LastValue > 0) loopMs[phase].Add(playerLoop.LastValue / 1e6);
            if (gcAlloc.Valid) gcBytes[phase].Add(gcAlloc.LastValue);

            // One full 1920x1080 MSAA render, synchronised with a 1-pixel readback.
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

        static void Capture(SongGraphLoader loader, Camera cam, string file)
        {
            string dir = Path.Combine(SongGraphLoader.RepoRoot(), "data", "screens");
            Directory.CreateDirectory(dir);
            RenderTexture? previous = cam.targetTexture;
            cam.targetTexture = target;
            loader.RefreshView(cam);
            loader.Hud.RenderInto(cam);
            cam.Render();
            RenderTexture.active = target;
            Texture2D tex = new(Width, Height, TextureFormat.RGB24, false);
            tex.ReadPixels(new Rect(0, 0, Width, Height), 0, 0);
            tex.Apply();
            File.WriteAllBytes(Path.Combine(dir, file), tex.EncodeToPNG());
            Object.Destroy(tex);
            RenderTexture.active = null;
            loader.Hud.RenderInto(null);
            cam.targetTexture = previous;
        }

        static void Check(string name, bool ok, string detail = "")
        {
            checks.Add((name, ok, detail));
            Debug.Log($"[bench] {(ok ? "PASS" : "FAIL")} {name}{(detail.Length > 0 ? " — " + detail : "")}");
        }

        static void Finish(string? failure)
        {
            if (failure != null) Check(failure, false);
            StringBuilder json = new();
            json.Append("{\n  \"graphics_device\": \"").Append(SystemInfo.graphicsDeviceName).Append("\",\n  \"phases\": {");
            List<string> phases = new();
            foreach (Phase p in new[] { Phase.Idle, Phase.Hover, Phase.Tour, Phase.Simulation })
            {
                if (!loopMs.ContainsKey(p)) continue;
                string entry = $"\n    \"{p.ToString().ToLowerInvariant()}\": {{\"frames\": {renderMs[p].Count}, " +
                               $"\"player_loop_ms\": {Stats(loopMs[p])}, \"render_ms\": {Stats(renderMs[p])}, " +
                               $"\"gc_bytes_per_frame\": {Stats(gcBytes[p])}}}";
                phases.Add(entry);
                Debug.Log($"[bench] {p}: player loop {Summary(loopMs[p])} ms, render+sync {Summary(renderMs[p])} ms, GC {Summary(gcBytes[p])} B/frame");
            }
            json.Append(string.Join(",", phases)).Append("\n  },\n");
            json.Append($"  \"focus_change_ms\": {Stats(focusMs)},\n");
            Debug.Log($"[bench] focus change (1000 bubbles + edges re-materialled): {Summary(focusMs)} ms");
            json.Append("  \"checks\": [").Append(string.Join(",", checks.Select(c =>
                $"\n    {{\"name\": \"{c.name.Replace("\"", "'")}\", \"ok\": {(c.ok ? "true" : "false")}, \"detail\": \"{c.detail.Replace("\"", "'").Replace("\\", "/")}\"}}")));
            json.Append("\n  ]\n}\n");
            string dir = Path.Combine(SongGraphLoader.RepoRoot(), "data", "screens");
            Directory.CreateDirectory(dir);
            File.WriteAllText(Path.Combine(dir, "playmode_bench.json"), json.ToString(), new UTF8Encoding(false));
            int failures = checks.Count(c => !c.ok);
            Debug.Log($"[bench] {checks.Count - failures}/{checks.Count} checks passed");
            exitCode = failures == 0 ? 0 : 1;
            playerLoop.Dispose();
            gcAlloc.Dispose();
            if (target != null) target.Release();
            phase = Phase.Done;
            EditorApplication.ExitPlaymode();
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
