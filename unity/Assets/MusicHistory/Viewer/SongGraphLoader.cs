#nullable enable
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using FDG;
using MusicHistory.Walkthrough;
using UnityEngine;
using UnityEngine.InputSystem;
using Debug = UnityEngine.Debug;

namespace MusicHistory.Viewer
{
    /// <summary>
    /// Builds the song influence graph from the graph database (docs/DESIGN.md §10; fork of
    /// Unity-FDG's LoadFromSqliteDb, bound to the scene by the same script GUID).
    ///
    /// One bubble per song at its layout position (time runs left to right by default), tree
    /// edges always visible, secondary edges on hover, a decade timeline, a HUD, hover/click
    /// highlighting and the walkthrough. The live force-directed simulation is off by default
    /// (F toggles it) and axis-locked so it can never move a song off its date.
    ///
    /// DB path: -musicHistoryDb &lt;path&gt; on the command line, else the inspector's DbPath, else
    /// &lt;repo&gt;/data/graph/music_graph.db, else StreamingAssets/music_graph.db (player builds), else
    /// &lt;repo&gt;/data/graph/demo_graph.db. MIDI paths are resolved against the DB's folder.
    /// </summary>
    [RequireComponent(typeof(ForceDirectedGraph))]
    public sealed class SongGraphLoader : MonoBehaviour
    {
        public const string CommandLineFlag = "-musicHistoryDb";

        [Header("Graph database")]
        [Tooltip("Blank: -musicHistoryDb <path>, else <repo>/data/graph/music_graph.db, else StreamingAssets/music_graph.db, else <repo>/data/graph/demo_graph.db.")]
        public string DbPath = "";
        [Tooltip("Build in Start (off for editor tools that build explicitly).")]
        public bool BuildOnStart = true;

        [Header("Layout")]
        public TimeAxisView TimeAxis = TimeAxisView.LeftToRight;

        [Header("Bubbles (area proportional to descendants + 1)")]
        [Tooltip("Radius = scale x sqrt(descendants + 1) when the layout gives no display_radius.")]
        [Min(.01f)] public float BubbleRadiusScale = .2f;
        [Min(.01f)] public float MinBubbleRadius = .22f;
        [Min(.1f)] public float MaxBubbleRadius = 6f;
        public bool UseLayoutDisplayRadius = true;

        [Header("Edges")]
        [Min(.001f)] public float TreeEdgeWidth = .16f;
        [Min(.001f)] public float SecondaryEdgeWidth = .08f;
        [Min(0f)] public float EdgePadding = .05f;
        [Tooltip("A tree edge leading into at least this many descendants is drawn as a trunk.")]
        [Min(1)] public int TrunkDescendants = 10;
        public bool ShowAllSecondaryEdges;

        [Header("Labels")]
        [Range(0, 300)] public int AlwaysLabelledSongs = 40;
        [Min(.001f)] public float LabelScreenSize = .1f;

        [Header("Camera")]
        [Tooltip("Camera driven by the viewer; blank = Camera.main.")]
        public Camera? ViewCameraOverride;
        [Tooltip("Screen area (normalized) the overview fills, leaving the HUD panels clear.")]
        public Rect OverviewViewport = new(.01f, .09f, .91f, .71f);

        public SongGraphData? Data { get; private set; }
        public GraphFrame Frame { get; private set; } = new();
        public string ResolvedDbPath { get; private set; } = "";
        public string DbPathReason { get; private set; } = "";
        public double BuildMilliseconds { get; private set; }
        public IReadOnlyList<SongNode> Nodes => nodes;
        public IReadOnlyList<InfluenceEdge> Edges => edges;
        public IReadOnlyList<SongNode> PinnedLabelNodes => pinned;
        public LabelLayer Labels { get; private set; } = null!;
        public HoverHighlighter Highlighter { get; private set; } = null!;
        public GraphHud Hud { get; private set; } = null!;
        public WalkthroughDirector Director { get; private set; } = null!;
        public TimelineAxis Timeline { get; private set; } = null!;
        public ForceDirectedGraph Simulation { get; private set; } = null!;
        public Camera? ViewCamera => ViewCameraOverride != null ? ViewCameraOverride : Camera.main;
        public bool AllLabels { get; private set; }

        public event Action<SongGraphLoader>? Built;

        readonly List<SongNode> nodes = new();
        readonly List<InfluenceEdge> edges = new();
        readonly List<SongNode> pinned = new();
        GameObject? graphRoot;

        public SongNode NodeById(int nodeId) => nodes[nodeId - 1];

        void Start()
        {
            if (!BuildOnStart) return;
            string path = ResolveDatabasePath(DbPath, out string reason);
            if (!File.Exists(path))
            {
                Debug.LogError($"MusicHistory: graph database not found: {path}. Run the pipeline (influence + layout) or pass {CommandLineFlag} <path>.");
                enabled = false;
                return;
            }
            DbPathReason = reason;
            Build(path);
        }

        /// <summary>Reads <paramref name="dbPath"/> and (re)builds the whole scene graph.</summary>
        public void Build(string dbPath)
        {
            Stopwatch clock = Stopwatch.StartNew();
            Clear();
            ResolvedDbPath = Path.GetFullPath(dbPath);
            SongGraphData data = SongGraphReader.Read(ResolvedDbPath);
            Data = data;
            if (data.Problems.Count > 0)
                Debug.LogWarning($"MusicHistory: {data.Problems.Count} contract problems in {ResolvedDbPath}; first: " +
                                 string.Join(" | ", data.Problems.Take(5)));

            Vector3[] positions = GraphLayoutMapping.DisplayPositions(data, TimeAxis, out GraphFrame frame);
            Frame = frame;

            Simulation = GetComponent<ForceDirectedGraph>();
            Simulation.Clear();
            Simulation.Stepped -= RedrawEdges;
            Simulation.Stepped += RedrawEdges;
            Simulation.LockAxis = true;
            Simulation.LockedAxis = frame.TimeDir;

            graphRoot = new GameObject("Song Graph");
            graphRoot.transform.SetParent(transform, false);
            Transform songRoot = new GameObject("Songs").transform;
            songRoot.SetParent(graphRoot.transform, false);
            Transform treeRoot = new GameObject("Tree Edges").transform;
            treeRoot.SetParent(graphRoot.transform, false);
            Transform secondaryRoot = new GameObject("Secondary Edges").transform;
            secondaryRoot.SetParent(graphRoot.transform, false);

            for (int i = 0; i < data.Songs.Count; i++)
            {
                SongRecord song = data.Songs[i];
                SongNode node = SongNode.Create(song, positions[i], BubbleRadius(song, data.HasPositions), songRoot);
                nodes.Add(node);
                float mass = (float)(song.LayoutMass ?? 1.0 + Math.Log(1 + song.Descendants, 2));
                Simulation.AddNodeToGraph(node, song.NodeId - 1, Mathf.Max(.1f, mass));
            }
            foreach (SongNode node in nodes)
            {
                if (node.Song.TreeParent is int parent && parent >= 1 && parent <= nodes.Count)
                {
                    node.TreeParent = nodes[parent - 1];
                    node.TreeParent.TreeChildren.Add(node);
                }
            }

            foreach (EdgeRecord record in data.Edges)
            {
                if (record.Source < 1 || record.Source > nodes.Count || record.Target < 1 || record.Target > nodes.Count) continue;
                SongNode source = nodes[record.Source - 1], target = nodes[record.Target - 1];
                float similarity = Mathf.Clamp01((float)record.Similarity);
                // Tree edges into big subtrees are the trunks of the lineage: wider and brighter.
                int below = target.Song.Descendants;
                EdgeTier tier = below >= TrunkDescendants ? EdgeTier.Trunk : below >= 1 ? EdgeTier.Branch : EdgeTier.Twig;
                // Identity lineages: strong matches (exact shared passages) are drawn brightest.
                if (record.IsStrongMatch && data.IsIdentityLineage) tier = EdgeTier.Strong;
                float subtree = Mathf.Min(3f, 1f + .35f * Mathf.Log(1 + below, 2));
                float width = record.IsTree
                    ? TreeEdgeWidth * (.5f + .5f * similarity) * subtree
                    : SecondaryEdgeWidth * (.6f + .8f * similarity);
                InfluenceEdge edge = InfluenceEdge.Create(record, source, target, width, EdgePadding,
                    record.IsTree ? treeRoot : secondaryRoot, record.IsTree ? tier : EdgeTier.Branch);
                edges.Add(edge);
                source.Outgoing.Add(edge);
                target.Incoming.Add(edge);
                if (record.IsTree)
                {
                    target.TreeEdge = edge;
                    Simulation.AddEdgeToGraph(source, target);
                }
                edge.SetShown(record.IsTree || ShowAllSecondaryEdges);
            }

            Labels = GetOrAdd<LabelLayer>();
            Labels.ScreenSize = LabelScreenSize;
            foreach (SongNode node in nodes
                         .OrderByDescending(n => n.Song.Descendants)
                         .ThenByDescending(n => n.Song.RefCount)
                         .ThenBy(n => n.Song.CanonRank ?? int.MaxValue)
                         .ThenBy(n => n.NodeId)
                         .Take(AlwaysLabelledSongs))
            {
                node.LabelPinned = true;
                pinned.Add(node);
                node.RefreshLabel(Labels);
            }

            double minTime = data.MetaDouble("min_time") ?? data.MinTime;
            double maxTime = data.MetaDouble("max_time") ?? data.MaxTime;
            Timeline = TimelineAxis.Create(frame, Math.Min(minTime, data.MinTime), Math.Max(maxTime, data.MaxTime), Labels, graphRoot.transform);

            Camera? cam = ViewCamera;
            SceneLook.Apply(cam);

            Hud = GetOrAdd<GraphHud>();
            Hud.Build(this);
            Hud.SetLegend(LegendBody());
            // A label under a HUD panel or cut by the screen edge is hidden (focus labels excepted).
            Labels.KeepClear = c => Hud != null ? Hud.PanelScreenRects(c) : (IReadOnlyList<Rect>)Array.Empty<Rect>();

            Highlighter = GetOrAdd<HoverHighlighter>();
            Highlighter.Loader = this;
            Highlighter.FocusChanged -= OnFocusChanged;
            Highlighter.FocusChanged += OnFocusChanged;
            Highlighter.EdgeFocusChanged -= OnEdgeFocusChanged;
            Highlighter.EdgeFocusChanged += OnEdgeFocusChanged;
            Highlighter.ApplyFocus(null, force: true);

            Director = GetOrAdd<WalkthroughDirector>();
            Director.Initialize(this);

            if (cam != null) FrameOverview(cam);
            clock.Stop();
            BuildMilliseconds = clock.Elapsed.TotalMilliseconds;
            Debug.Log($"MusicHistory: built {nodes.Count} songs, {edges.Count} edges ({data.TreeEdgeCount} tree) from " +
                      $"{ResolvedDbPath} [{DbPathReason}] in {BuildMilliseconds:0} ms; {frame.Description}; " +
                      $"time fit residual {frame.MaxResidualYears:0.000} years; player: {Director.PlayerDescription}");
            Built?.Invoke(this);
        }

        /// <summary>After a live-simulation step: every edge follows its (moved) endpoints once.</summary>
        void RedrawEdges()
        {
            foreach (InfluenceEdge edge in edges) edge.UpdateGeometry();
        }

        void OnFocusChanged(SongNode? node)
        {
            if (Hud != null) Hud.ShowSong(node);
        }

        void OnEdgeFocusChanged(InfluenceEdge edge)
        {
            if (Hud != null) Hud.ShowEdge(edge);
        }

        T GetOrAdd<T>() where T : Component
        {
            T existing = GetComponent<T>();
            return existing != null ? existing : gameObject.AddComponent<T>();
        }

        float BubbleRadius(SongRecord song, bool hasLayout)
        {
            float radius = UseLayoutDisplayRadius && hasLayout && song.LayoutDisplayRadius is double d && d > 0
                ? (float)d
                : BubbleRadiusScale * Mathf.Sqrt(song.Descendants + 1);
            return Mathf.Clamp(radius, MinBubbleRadius, MaxBubbleRadius);
        }

        string LegendBody()
        {
            SongGraphData data = Data!;
            string m = GraphHud.Muted;
            StringBuilder b = new();
            bool lineage = data.IsIdentityLineage;
            b.Append($"<size=125%><b>MusicHistory</b></size>  <color={m}>{(lineage ? "shared-identity lineages" : "song influence graph")}</color>");
            if (data.IsSynthetic) b.Append("  <color=#ffcf4a>[synthetic data]</color>");
            b.Append(lineage
                ? $"\n{data.Songs.Count} songs · {data.Edges.Count} shared-identity links ({data.TreeEdgeCount} tree) · {data.RootCount} roots" +
                  (data.HasFamilies ? $" · {data.Families.Count} families" : "")
                : $"\n{data.Songs.Count} songs · {data.Edges.Count} influences ({data.TreeEdgeCount} tree) · {data.RootCount} roots");
            if (data.Songs.Count > 0) b.Append($" · {data.Songs[0].Year}–{data.Songs[data.Songs.Count - 1].Year}");
            b.Append(TimeAxis switch
            {
                TimeAxisView.LeftToRight => " · time →",
                TimeAxisView.BottomToTop => " · time ↑",
                _ => ""
            });
            b.Append($"\n<color={m}>Fill: key</color> ");
            int[] fifths = { 0, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10, 5 };
            foreach (int pc in fifths)
                b.Append($"<color={SongPalette.ToHex(SongPalette.KeyColor(pc, false))}>{SongPalette.PitchName(pc, false)}</color> ");
            b.Append($"<color={m}>(minor darker)</color>");
            b.Append($"\n<color={m}>Ring: decade</color> ");
            for (int decade = SongPalette.FirstDecade; decade <= SongPalette.LastDecade; decade += 10)
                b.Append($"<color={SongPalette.ToHex(SongPalette.DecadeColor(decade))}>{decade % 100:00}s</color> ");
            b.Append($"<color={m}>· area = descendants + 1</color>");
            if (lineage)
            {
                // DESIGN.md §8b: an edge is a shared musical identity, labelled as such.
                int strong = data.Edges.Count(e => e.IsStrongMatch);
                b.Append($"\n<color={m}>Edge = two songs share a musical identity (shared DNA, not proven copying) · wide end = earlier song</color>");
                b.Append($"\n<color={m}>Shared:</color> ");
                (EdgeChannel channel, string label)[] kinds =
                {
                    (EdgeChannel.Chord, "chord progression"), (EdgeChannel.Loop, "loop"), (EdgeChannel.Bass, "bass line"),
                    (EdgeChannel.Melody, "melody"), (EdgeChannel.Both, "melody + harmony")
                };
                b.Append(string.Join($" <color={m}>·</color> ",
                    kinds.Select(k => $"<color={SongPalette.ToHex(SongPalette.ChannelColor(k.channel))}>{k.label}</color>")));
                b.Append($"\n<color={GraphHud.StrongColor}><b>Bright edge = strong match</b></color>" +
                         $"<color={m}>: an exact shared passage above the calibrated evidence threshold ({strong} edges)</color>");
            }
            else
            {
                b.Append($"\n<color={m}>Edges</color> ");
                foreach (EdgeChannel c in new[] { EdgeChannel.Chord, EdgeChannel.Melody, EdgeChannel.Both, EdgeChannel.Bass, EdgeChannel.Loop })
                    b.Append($"<color={SongPalette.ToHex(SongPalette.ChannelColor(c))}>{SongPalette.ChannelLabel(c)}</color> ");
                b.Append($"<color={m}>· wide end = influencer</color>");
            }
            b.Append($"\n<size=80%><color={m}>{GraphHud.Esc(Path.GetFileName(ResolvedDbPath))} · {GraphHud.Esc(Frame.Description)}</color></size>");
            return b.ToString();
        }

        /// <summary>Places <paramref name="cam"/> so the whole graph and its timeline are in view.</summary>
        public void FrameOverview(Camera cam)
        {
            if (Data == null || nodes.Count == 0) return;
            List<(Vector3, float)> items = new(nodes.Count + 2);
            foreach (SongNode n in nodes) items.Add((n.transform.position, n.Radius));
            if (Timeline != null)
            {
                items.Add((Timeline.AxisPoint(Timeline.FirstYear), 1.5f));
                items.Add((Timeline.AxisPoint(Timeline.LastYear), 1.5f));
            }
            (Vector3 position, Quaternion rotation) = CameraFraming.Frame(cam, items, Frame.ViewForward, 1.02f, 5f, OverviewViewport);
            cam.transform.SetPositionAndRotation(position, rotation);
            float extent = Frame.Bounds.size.magnitude;
            cam.farClipPlane = Mathf.Max(cam.farClipPlane, Vector3.Distance(position, Frame.Bounds.center) + extent * 2f);
            CameraControl control = cam.GetComponent<CameraControl>();
            if (control != null)
            {
                control.SyncRotationFromTransform();
                control.Speed = Mathf.Max(control.Speed, extent * .12f);
            }
        }

        /// <summary>Labels, timeline widths and HUD for <paramref name="cam"/> now (edit-mode rendering).</summary>
        public void RefreshView(Camera cam)
        {
            Labels.Refresh(cam);
            Timeline.Refresh(cam);
            Labels.ForceMeshUpdate();
            Hud.ForceUpdate();
        }

        void Update()
        {
            Keyboard? k = Keyboard.current;
            if (k == null || Data == null) return;
            if (k.fKey.wasPressedThisFrame) ToggleSimulation();
            if (k.lKey.wasPressedThisFrame) SetAllLabels(!AllLabels);
            if (k.vKey.wasPressedThisFrame)
            {
                ShowAllSecondaryEdges = !ShowAllSecondaryEdges;
                if (Highlighter.Suspended) Highlighter.ApplyFocus(Highlighter.Focus, force: true);
                else Highlighter.Reapply();
            }
            if (k.hKey.wasPressedThisFrame) Hud.ToggleHelp();
            if ((k.rKey.wasPressedThisFrame || k.homeKey.wasPressedThisFrame) && !Director.IsTouring && ViewCamera != null)
                FrameOverview(ViewCamera);
            // The lyric-themes cloud of the same songs (its G key comes back here).
            if (k.tKey.wasPressedThisFrame && !Director.IsTouring
                && UnityEngine.SceneManagement.SceneUtility.GetBuildIndexByScenePath("Assets/Scenes/LyricThemes.unity") >= 0)
                UnityEngine.SceneManagement.SceneManager.LoadScene("LyricThemes");
        }

        public void ToggleSimulation()
        {
            Simulation.LockAxis = true;
            Simulation.LockedAxis = Frame.TimeDir;
            if (Simulation.IsRunning) Simulation.StopGraph();
            else Simulation.StartGraph();
        }

        public void SetAllLabels(bool all)
        {
            AllLabels = all;
            foreach (SongNode n in nodes)
            {
                n.LabelPinned = all || pinned.Contains(n);
                n.RefreshLabel(Labels);
            }
        }

        /// <summary>Destroys everything <see cref="Build"/> created.</summary>
        public void Clear()
        {
            if (Director != null) Director.Exit();
            if (Simulation != null) Simulation.Clear();
            if (Labels != null) Labels.Clear();
            if (graphRoot != null) Discard(graphRoot);
            graphRoot = null;
            if (Hud != null && Hud.Canvas != null) Discard(Hud.Canvas.gameObject);
            nodes.Clear();
            edges.Clear();
            pinned.Clear();
            AllLabels = false;
            GraphMaterials.Clear();
            SongNode.ReleaseSharedMesh();
            InfluenceEdge.ReleaseSharedMesh();
            Data = null;
        }

        static void Discard(UnityEngine.Object o)
        {
            if (Application.isPlaying) Destroy(o);
            else DestroyImmediate(o);
        }

        void OnDestroy()
        {
            GraphMaterials.Clear();
        }

        public static string RepoRoot() => Path.GetFullPath(Path.Combine(Application.dataPath, "..", ".."));

        /// <summary>
        /// A user-supplied path: absolute as given; relative paths are tried against the working
        /// directory (the Unity project folder in the editor), then against the repository root.
        /// </summary>
        public static string ResolveUserPath(string path)
        {
            if (Path.IsPathRooted(path)) return Path.GetFullPath(path);
            string fromCwd = Path.GetFullPath(path);
            if (File.Exists(fromCwd)) return fromCwd;
            string fromRepo = Path.GetFullPath(Path.Combine(RepoRoot(), path));
            return File.Exists(fromRepo) ? fromRepo : fromCwd;
        }

        public static string ResolveDatabasePath(string configured, out string reason)
        {
            string[] args = Environment.GetCommandLineArgs();
            for (int i = 0; i + 1 < args.Length; i++)
            {
                if (!string.Equals(args[i], CommandLineFlag, StringComparison.OrdinalIgnoreCase)) continue;
                reason = "command line";
                return ResolveUserPath(args[i + 1]);
            }
            if (!string.IsNullOrWhiteSpace(configured))
            {
                reason = "inspector";
                return ResolveUserPath(configured);
            }
            string repo = RepoRoot();
            (string path, string why)[] candidates =
            {
                (Path.Combine(repo, "data", "graph", "music_graph.db"), "pipeline graph"),
                (Path.Combine(Application.streamingAssetsPath, "music_graph.db"), "StreamingAssets"),
                (Path.Combine(repo, "data", "graph", "demo_graph.db"), "demo graph (music_graph.db not found)")
            };
            foreach ((string path, string why) in candidates)
            {
                if (!File.Exists(path)) continue;
                reason = why;
                return path;
            }
            reason = "missing";
            return candidates[0].path;
        }
    }
}
