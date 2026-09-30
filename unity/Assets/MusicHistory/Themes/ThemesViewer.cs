#nullable enable
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using MusicHistory.Playback;
using MusicHistory.Viewer;
using MusicHistory.Walkthrough;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.SceneManagement;
using Debug = UnityEngine.Debug;
#if UNITY_EDITOR
using UnityEditor.SceneManagement;
#endif

namespace MusicHistory.Themes
{
    /// <summary>
    /// Entry point of the LyricThemes scene (docs/DESIGN.md §12): where each song sits among ten
    /// lyrical themes. The themes are gold pads equally spaced on a ring with their text as labels;
    /// every song is a bubble at its layout position (the balance point of its theme scores),
    /// blue for a male singer, pink for a female singer, grey otherwise; filled when it was
    /// classified from its lyrics, hollow when only from its title.
    ///
    /// Hover shows a card (title, artist, year, singer, text source, top three themes with bars
    /// and scores — never lyrics; the database holds none) and tethers to those themes. A click
    /// plays the song's excerpt natively (its own key and BPM, no morph) through the
    /// <see cref="ISongPlayer"/>; a second click stops it. 1–9/0 show one theme, L labels every
    /// song, G (or the button) goes back to the SongInfluenceGraph scene.
    ///
    /// DB path: -themesDb &lt;path&gt;, else the inspector's DbPath, else
    /// &lt;repo&gt;/data/graph/themes_graph.db, else StreamingAssets/themes_graph.db, else
    /// &lt;repo&gt;/data/graph/themes_demo.db. When the layout stage has not written positions yet, a
    /// deterministic fallback places each song at its score-weighted barycentre and spreads overlaps.
    /// </summary>
    [DefaultExecutionOrder(-50)]   // re-place theme labels before the label layer's LateUpdate
    public sealed class ThemesViewer : MonoBehaviour
    {
        public const string InfluenceSceneName = "SongInfluenceGraph";
        public const string InfluenceScenePath = "Assets/Scenes/SongInfluenceGraph.unity";

        [Header("Themes database")]
        [Tooltip("Blank: -themesDb <path>, else <repo>/data/graph/themes_graph.db, else StreamingAssets/themes_graph.db, else <repo>/data/graph/themes_demo.db.")]
        public string DbPath = "";
        [Tooltip("Build in Start (off for editor tools that build explicitly).")]
        public bool BuildOnStart = true;

        [Header("Songs")]
        [Min(.05f)] public float BubbleRadius = .48f;
        [Tooltip("Songs always labelled per theme: the ones that express it most clearly.")]
        [Range(0, 10)] public int ExemplarsPerTheme = 2;
        [Tooltip("Songs labelled for the theme shown with 1–9/0.")]
        [Range(0, 60)] public int FilterLabels = 14;

        [Header("Ring")]
        [Min(.5f)] public float PadRadius = 4.6f;

        [Header("Labels")]
        [Min(.001f)] public float LabelScreenSize = .075f;

        [Header("Camera")]
        [Tooltip("Camera driven by the viewer; blank = Camera.main.")]
        public Camera? ViewCameraOverride;
        [Range(10f, 89.5f)] public float OverviewPitch = 58f;
        public float OverviewYaw;
        [Tooltip("Screen area (normalized) the ring fills in the overview, leaving room for the theme labels and HUD.")]
        public Rect OverviewViewport = new(.15f, .09f, .70f, .80f);

        [Header("Picking")]
        [Tooltip("A bubble smaller than this on screen is still picked within this radius (pixels).")]
        [Min(1f)] public float MinPickPixels = 7f;

        public ThemesGraphData? Data { get; private set; }
        public string ResolvedDbPath { get; private set; } = "";
        public string DbPathReason { get; private set; } = "";
        /// <summary>"layout" (theme_song positions) or "viewer fallback" (NULL positions).</summary>
        public string PositionSource { get; private set; } = "";
        public double BuildMilliseconds { get; private set; }
        public IReadOnlyList<ThemeSongNode> Nodes => nodes;
        public IReadOnlyList<ThemeSongNode> PinnedLabelNodes => pinned;
        public ThemesRing Ring { get; private set; } = null!;
        public LabelLayer Labels { get; private set; } = null!;
        public ThemesHud Hud { get; private set; } = null!;
        public ThemeSongNode? Hovered { get; private set; }
        public ThemeSongNode? Playing { get; private set; }
        /// <summary>Theme shown alone (1..N), 0 = all themes.</summary>
        public int FilterAnchor { get; private set; }
        public bool AllLabels { get; private set; }
        public ISongPlayer? Player { get; private set; }
        public string PlayerDescription { get; private set; } = "";
        /// <summary>Last transient message shown in the status line (e.g. a missing MIDI file).</summary>
        public string? Message { get; private set; }
        public Camera? ViewCamera => ViewCameraOverride != null ? ViewCameraOverride : Camera.main;
        public IReadOnlyList<LineRenderer> Tethers => tethers;

        public event Action<ThemesViewer>? Built;

        readonly List<ThemeSongNode> nodes = new();
        readonly List<ThemeSongNode> pinned = new();
        readonly HashSet<ThemeSongNode> requested = new();
        readonly List<LineRenderer> tethers = new();
        readonly List<Material> tetherMaterials = new();
        readonly List<float> tetherScores = new();
        GameObject? graphRoot;
        SongClip? playingClip;
        float messageUntil;
        Vector2 pressPosition;
        bool pressed, pressedOnButton;
        ThemeSongNode? tetherNode;
        Vector3[] songPositions = Array.Empty<Vector3>();
        (ThemeSongNode?, int, string?, ISongPlayer?) statusKey;

        void Start()
        {
            if (!BuildOnStart) return;
            string path = ThemesGraphReader.ResolveDatabasePath(DbPath, out string reason);
            if (!File.Exists(path))
            {
                Debug.LogError($"MusicHistory: themes database not found: {path}. Run the themes stage and the themes layout, or pass {ThemesGraphReader.CommandLineFlag} <path>.");
                enabled = false;
                return;
            }
            DbPathReason = reason;
            Build(path);
        }

        /// <summary>Reads <paramref name="dbPath"/> and (re)builds the ring, the songs and the HUD.</summary>
        public void Build(string dbPath)
        {
            Stopwatch clock = Stopwatch.StartNew();
            Clear();
            ResolvedDbPath = Path.GetFullPath(dbPath);
            ThemesGraphData data = ThemesGraphReader.Read(ResolvedDbPath);
            Data = data;
            if (data.Problems.Count > 0)
                Debug.LogWarning($"MusicHistory: {data.Problems.Count} contract problems in {ResolvedDbPath}; first: " +
                                 string.Join(" | ", data.Problems.Take(5)));

            Vector3[] positions;
            if (data.HasPositions)
            {
                positions = data.Songs.Select(s => s.LayoutPosition!.Value).ToArray();
                PositionSource = "layout";
            }
            else
            {
                positions = ThemesGraphReader.FallbackPositions(data, BubbleRadius * 2.1f);
                PositionSource = "viewer fallback";
            }

            Labels = GetOrAdd<LabelLayer>();
            Labels.ScreenSize = LabelScreenSize;
            Labels.HideDimmed = false;

            graphRoot = new GameObject("Lyric Themes Graph");
            graphRoot.transform.SetParent(transform, false);
            Ring = ThemesRing.Create(data, PadRadius, Labels, graphRoot.transform);
            Transform songRoot = new GameObject("Songs").transform;
            songRoot.SetParent(graphRoot.transform, false);
            for (int i = 0; i < data.Songs.Count; i++)
                nodes.Add(ThemeSongNode.Create(data.Songs[i], positions[i], BubbleRadius, songRoot));
            Ring.SetReach(positions, BubbleRadius);
            songPositions = positions;

            for (int a = 1; a <= data.Anchors.Count; a++)
            {
                foreach (ThemeSongNode node in Exemplars(a, ExemplarsPerTheme))
                {
                    node.LabelPinned = true;
                    pinned.Add(node);
                    node.RefreshLabel(Labels, false);
                }
            }

            Transform tetherRoot = new GameObject("Tethers").transform;
            tetherRoot.SetParent(graphRoot.transform, false);
            for (int i = 0; i < 3; i++)
            {
                GameObject go = new($"Tether {i + 1}");
                go.transform.SetParent(tetherRoot, false);
                LineRenderer line = go.AddComponent<LineRenderer>();
                line.useWorldSpace = true;
                line.positionCount = 2;
                line.numCapVertices = 2;
                line.alignment = LineAlignment.View;
                line.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
                line.receiveShadows = false;
                line.lightProbeUsage = UnityEngine.Rendering.LightProbeUsage.Off;
                line.reflectionProbeUsage = UnityEngine.Rendering.ReflectionProbeUsage.Off;
                Material m = ThemesMaterials.Line($"Tether {i + 1}");
                line.sharedMaterial = m;
                line.enabled = false;
                tethers.Add(line);
                tetherMaterials.Add(m);
                tetherScores.Add(0f);
            }

            Camera? cam = ViewCamera;
            SceneLook.Apply(cam);
            Hud = GetOrAdd<ThemesHud>();
            Hud.Build();
            Hud.SetLegend(LegendBody());
            statusKey = default;
            ApplyStates();
            RefreshStatus(force: true);
            if (cam != null) FrameOverview(cam);
            if (Application.isPlaying) EnsurePlayer();

            clock.Stop();
            BuildMilliseconds = clock.Elapsed.TotalMilliseconds;
            Debug.Log($"MusicHistory: lyric themes: {nodes.Count} songs, {data.Anchors.Count} themes from {ResolvedDbPath} " +
                      $"[{DbPathReason}] in {BuildMilliseconds:0} ms; positions: {PositionSource}; player: {(Player != null ? PlayerDescription : "not yet")}");
            Built?.Invoke(this);
        }

        /// <summary>The songs whose top theme is <paramref name="anchorId"/>, clearest first.</summary>
        public IEnumerable<ThemeSongNode> Exemplars(int anchorId, int count) => nodes
            .Where(n => n.Song.TopAnchor == anchorId)
            .OrderByDescending(n => n.Song.Scores[anchorId - 1])
            .ThenBy(n => n.NodeId)
            .Take(count);

        T GetOrAdd<T>() where T : Component
        {
            T existing = GetComponent<T>();
            return existing != null ? existing : gameObject.AddComponent<T>();
        }

        string LegendBody()
        {
            ThemesGraphData data = Data!;
            string m = ThemesPalette.Muted;
            int n = data.Songs.Count, lyrics = data.LyricsCount;
            StringBuilder b = new();
            b.Append($"<size=128%><b>Lyric themes</b></size>  <color={m}>where each song sits among {data.Anchors.Count} themes</color>");
            if (data.IsSynthetic) b.Append("  <color=#ffcf4a>[synthetic data]</color>");
            b.Append($"\n{n} songs · {lyrics} classified from lyrics · {n - lyrics} from the title only");
            int male = data.CountGender(SingerGender.Male), female = data.CountGender(SingerGender.Female);
            b.Append($"\n<color={ThemesPalette.ToHex(ThemesPalette.Male)}>●</color> male singer {male}     " +
                     $"<color={ThemesPalette.ToHex(ThemesPalette.Female)}>●</color> female singer {female}" +
                     $"\n<color={ThemesPalette.ToHex(ThemesPalette.Neutral)}>●</color> mixed, nonbinary, unknown or instrumental {n - male - female}");
            b.Append($"\n<color={m}>Filled: classified from lyrics · hollow: from the title only.\nA song sits where the pull of its themes balances; " +
                     "on a theme when it is all that theme.</color>");
            List<string> facts = new();
            string? backend = data.MetaValue("backend");
            string? model = data.MetaValue("model");
            if (!string.IsNullOrEmpty(backend)) facts.Add(GraphHud.Esc(backend));
            if (!string.IsNullOrEmpty(model)) facts.Add(GraphHud.Esc(model!.Split(';')[0]));
            if (data.MetaDouble("validation_top1") is double t1 && data.MetaDouble("validation_top2") is double t2)
                facts.Add($"hand-labelled check: top-1 {Math.Round(t1 * 100)}%, top-2 {Math.Round(t2 * 100)}%" + (data.MetaInt("validation_n") is int vn ? $" (n={vn})" : ""));
            facts.Add(PositionSource == "layout" ? "layout positions" : "positions: viewer fallback (layout not run)");
            facts.Add(GraphHud.Esc(Path.GetFileName(ResolvedDbPath)));
            b.Append($"\n<size=80%><color={m}>{string.Join(" · ", facts)}</color></size>");
            return b.ToString();
        }

        // ------------------------------------------------------------------ camera

        /// <summary>Frames the whole ring (and the theme labels around it) from the overview angle.</summary>
        public void FrameOverview(Camera cam)
        {
            if (Data == null) return;
            Quaternion rotation = ThemesOrbitCamera.Rotation(OverviewYaw, OverviewPitch);
            List<(Vector3, float)> items = new(nodes.Count + 72);
            float outer = Ring.RingRadius + PadRadius;
            for (int i = 0; i < 72; i++)
            {
                float a = i * Mathf.PI * 2f / 72f;
                items.Add((new Vector3(Mathf.Cos(a) * outer, 0f, Mathf.Sin(a) * outer), 0f));
            }
            foreach (ThemeSongNode node in nodes) items.Add((node.transform.position, node.Radius));
            (Vector3 position, Quaternion look) = CameraFraming.Frame(cam, items, rotation * Vector3.forward, 1.0f, 5f, OverviewViewport);
            cam.transform.SetPositionAndRotation(position, look);
            cam.farClipPlane = Mathf.Max(cam.farClipPlane, position.magnitude * 4f + 400f);
            ThemesOrbitCamera? orbit = cam.GetComponent<ThemesOrbitCamera>();
            if (orbit != null) orbit.AdoptTransform(immediate: true);
        }

        // ------------------------------------------------------------------ picking and hover

        /// <summary>
        /// The bubble under <paramref name="screenPoint"/>: among the bubbles drawn there, the one
        /// whose sphere surface is nearest to the camera; when none is, the nearest bubble within
        /// <see cref="MinPickPixels"/> (tiny far-away dots stay easy to hover). Songs outside the
        /// active theme filter are not picked.
        /// </summary>
        public ThemeSongNode? Pick(Camera cam, Vector2 screenPoint)
        {
            float tanHalf = Mathf.Tan(cam.fieldOfView * .5f * Mathf.Deg2Rad);
            float pixelHeight = Mathf.Max(1, cam.pixelHeight);
            float grab2 = MinPickPixels * MinPickPixels;
            // One matrix product per call and plain arithmetic per song (no native call per bubble).
            Matrix4x4 view = cam.worldToCameraMatrix;
            Matrix4x4 vp = cam.projectionMatrix * view;
            Rect pixels = cam.pixelRect;
            float near = cam.nearClipPlane;
            ThemeSongNode? best = null, nearest = null;
            float bestDepth = float.MaxValue, nearestD2 = float.MaxValue;
            for (int i = 0; i < nodes.Count; i++)
            {
                ThemeSongNode node = nodes[i];
                if (FilterAnchor != 0 && node.Song.TopAnchor != FilterAnchor) continue;
                Vector3 p = songPositions[i];
                float depth = -(view.m20 * p.x + view.m21 * p.y + view.m22 * p.z + view.m23);
                if (depth <= near) continue;
                float w = vp.m30 * p.x + vp.m31 * p.y + vp.m32 * p.z + vp.m33;
                if (Mathf.Abs(w) < 1e-6f) continue;
                float sx = pixels.x + ((vp.m00 * p.x + vp.m01 * p.y + vp.m02 * p.z + vp.m03) / w * .5f + .5f) * pixels.width;
                float sy = pixels.y + ((vp.m10 * p.x + vp.m11 * p.y + vp.m12 * p.z + vp.m13) / w * .5f + .5f) * pixels.height;
                Vector3 sp = new(sx, sy, depth);
                float dx = sp.x - screenPoint.x, dy = sp.y - screenPoint.y;
                float d2 = dx * dx + dy * dy;
                float worldPerPixel = cam.orthographic ? cam.orthographicSize * 2f / pixelHeight : 2f * sp.z * tanHalf / pixelHeight;
                // As drawn: the SongBubble shader never draws a bubble smaller than 4 pixels across.
                float drawn = Mathf.Max(node.Radius / worldPerPixel, 2f);
                if (d2 <= drawn * drawn)
                {
                    // The sphere surface nearest the camera at this pixel is the one you see.
                    float surface = sp.z - node.Radius * Mathf.Sqrt(Mathf.Max(0f, 1f - d2 / (drawn * drawn)));
                    if (surface < bestDepth)
                    {
                        bestDepth = surface;
                        best = node;
                    }
                }
                else if (d2 <= grab2 && d2 < nearestD2)
                {
                    nearestD2 = d2;
                    nearest = node;
                }
            }
            return best != null ? best : nearest;
        }

        /// <summary>Hovers <paramref name="node"/> (null = nothing): glow, label, card and tethers.</summary>
        public void SetHover(ThemeSongNode? node)
        {
            if (node == Hovered) return;
            Hovered = node;
            ApplyStates();
        }

        /// <summary>Bubble states, requested labels, theme pads, card and tethers from hover, playback and filter.</summary>
        public void ApplyStates()
        {
            if (Data == null) return;
            foreach (ThemeSongNode n in nodes)
            {
                ThemeBubbleState state = n == Hovered ? ThemeBubbleState.Hover
                    : n == Playing ? ThemeBubbleState.Playing
                    : FilterAnchor != 0 && n.Song.TopAnchor != FilterAnchor ? ThemeBubbleState.Dimmed
                    : ThemeBubbleState.Normal;
                n.SetState(state);
            }

            HashSet<ThemeSongNode> want = new();
            if (Hovered != null) want.Add(Hovered);
            if (Playing != null) want.Add(Playing);
            if (FilterAnchor != 0)
                foreach (ThemeSongNode n in Exemplars(FilterAnchor, FilterLabels)) want.Add(n);
            foreach (ThemeSongNode n in requested)
                if (!want.Contains(n)) n.RequestLabel(Labels, false, AllLabels);
            foreach (ThemeSongNode n in want) n.RequestLabel(Labels, true, AllLabels);
            requested.Clear();
            requested.UnionWith(want);
            foreach (ThemeSongNode n in pinned) n.RefreshLabel(Labels, AllLabels);
            Labels.HideDimmed = FilterAnchor != 0;

            ThemeSongNode? focus = Hovered != null ? Hovered : Playing;
            HashSet<int> lit = new();
            if (focus != null)
                foreach ((int anchorId, double _) in focus.Song.TopThemes(3)) lit.Add(anchorId);
            foreach (ThemeAnchorNode a in Ring.Anchors)
            {
                int id = a.Anchor.AnchorId;
                a.SetLook(id == FilterAnchor || lit.Contains(id) ? ThemeAnchorNode.AnchorLook.Lit
                    : FilterAnchor != 0 ? ThemeAnchorNode.AnchorLook.Faded
                    : ThemeAnchorNode.AnchorLook.Normal);
            }
            SetTethers(focus);
            Hud.ShowCard(Hovered, Data);
        }

        void SetTethers(ThemeSongNode? node)
        {
            tetherNode = node;
            List<(int anchorId, double score)> top = node != null ? node.Song.TopThemes(tethers.Count) : new();
            for (int i = 0; i < tethers.Count; i++)
            {
                LineRenderer line = tethers[i];
                bool on = node != null && i < top.Count && top[i].score > 1e-3;
                line.enabled = on;
                if (!on) continue;
                float s = (float)top[i].score;
                tetherScores[i] = s;
                Vector3 from = node!.transform.position, to = Data!.Anchor(top[i].anchorId).Position;
                line.SetPosition(0, from);
                line.SetPosition(1, to);
                Color c = ThemesPalette.Theme * (.35f + .65f * s);
                c.a = 1f;
                ThemesMaterials.SetLineColor(tetherMaterials[i], c);
            }
        }

        /// <summary>Keeps tethers a few pixels wide (wider for a stronger theme) at any zoom.</summary>
        void RefreshTethers(Camera? cam)
        {
            if (cam == null || tetherNode == null) return;
            float depth = Mathf.Max(.5f, Vector3.Dot(tetherNode.transform.position - cam.transform.position, cam.transform.forward));
            float worldPerPixel = cam.orthographic
                ? cam.orthographicSize * 2f / Mathf.Max(1, cam.pixelHeight)
                : 2f * depth * Mathf.Tan(cam.fieldOfView * .5f * Mathf.Deg2Rad) / Mathf.Max(1, cam.pixelHeight);
            for (int i = 0; i < tethers.Count; i++)
                if (tethers[i].enabled) tethers[i].widthMultiplier = worldPerPixel * (1.6f + 4.4f * tetherScores[i]);
        }

        // ------------------------------------------------------------------ filter and labels

        /// <summary>Shows only the songs whose top theme is <paramref name="anchorId"/> (0 = all; the same id again toggles off).</summary>
        public void SetFilter(int anchorId)
        {
            if (Data == null) return;
            if (anchorId < 0 || anchorId > Data.Anchors.Count) anchorId = 0;
            FilterAnchor = anchorId == FilterAnchor ? 0 : anchorId;
            if (Hovered != null && FilterAnchor != 0 && Hovered.Song.TopAnchor != FilterAnchor) Hovered = null;
            ApplyStates();
        }

        public void SetAllLabels(bool all)
        {
            AllLabels = all;
            foreach (ThemeSongNode n in nodes)
                if (all || n.Label != null) n.RefreshLabel(Labels, all);
            ApplyStates();
        }

        // ------------------------------------------------------------------ playback

        /// <summary>The player: an ISongPlayer on this object, else MusicHistory.Audio.SongPlayer, else the silent clock.</summary>
        public ISongPlayer EnsurePlayer()
        {
            if (Player != null) return Player;
            UsePlayer(SongPlayerDiscovery.Discover(gameObject, out string description), description);
            return Player!;
        }

        /// <summary>Replaces the player (validation uses a recording probe).</summary>
        public void UsePlayer(ISongPlayer player, string description)
        {
            if (Player != null)
            {
                Player.Finished -= OnClipFinished;
                if (Player.IsPlaying) Player.Stop();
            }
            Player = player;
            PlayerDescription = description;
            Player.Finished += OnClipFinished;
            StartClicksAtOnce(player);
        }

        /// <summary>
        /// There are no tours here, so a click should start at once instead of waiting for the bar
        /// line of a clip that just ended: sets a player's public <c>Handoff</c> field (the synth's
        /// HandoffGrid) to <c>Immediate</c> when it has one. Reflection keeps this scene free of a
        /// compile-time dependency on the audio module, like <see cref="SongPlayerDiscovery"/>.
        /// </summary>
        static void StartClicksAtOnce(ISongPlayer player)
        {
            try
            {
                System.Reflection.FieldInfo? field = player.GetType().GetField("Handoff");
                if (field == null || !field.FieldType.IsEnum || !Enum.IsDefined(field.FieldType, "Immediate")) return;
                field.SetValue(player, Enum.Parse(field.FieldType, "Immediate"));
            }
            catch (Exception)
            {
                // Optional nicety only.
            }
        }

        /// <summary>
        /// Click on a song: plays its excerpt in its native key and BPM (no morph: no previous
        /// clip); clicking the playing song again stops it. Returns true when a clip started.
        /// </summary>
        public bool TogglePlay(ThemeSongNode node)
        {
            ISongPlayer player = EnsurePlayer();
            if (Playing == node && player.IsPlaying)
            {
                StopPlayback();
                return false;
            }
            SongClip clip = node.Song.ToClip();
            if (string.IsNullOrEmpty(clip.MidiPath) || !File.Exists(clip.MidiPath))
            {
                ShowMessage($"No MIDI file for {GraphHud.Esc(node.Song.Title)}");
                return false;
            }
            if (!(clip.ExcerptEndBeat > clip.ExcerptStartBeat))
            {
                ShowMessage($"No excerpt for {GraphHud.Esc(node.Song.Title)}");
                return false;
            }
            // Start at once rather than on the old clip's next bar line.
            if (player.IsPlaying) player.Stop();
            player.ApplesToApples = false;
            player.Paused = false;
            Playing = node;
            playingClip = clip;
            Message = null;
            try
            {
                player.Play(clip, null!);   // no previous clip: native key and tempo, no morph
            }
            catch (Exception e)
            {
                Playing = null;
                playingClip = null;
                ShowMessage($"Cannot play {GraphHud.Esc(node.Song.Title)} ({GraphHud.Esc(e.GetType().Name)})");
                ApplyStates();
                return false;
            }
            ApplyStates();
            return true;
        }

        public void StopPlayback()
        {
            if (Player != null && (Player.IsPlaying || Playing != null)) Player.Stop();
            bool had = Playing != null;
            Playing = null;
            playingClip = null;
            if (had) ApplyStates();
        }

        void OnClipFinished(SongClip clip)
        {
            if (!ReferenceEquals(clip, playingClip)) return;
            Playing = null;
            playingClip = null;
            ApplyStates();
        }

        void ShowMessage(string text)
        {
            Message = text;
            messageUntil = Time.realtimeSinceStartup + 4f;
        }

        /// <summary>The status line: what is playing (with progress), the theme filter, a transient message.</summary>
        public void RefreshStatus(bool force = false)
        {
            if (Data == null || Hud == null) return;
            if (Message != null && Application.isPlaying && Time.realtimeSinceStartup >= messageUntil) Message = null;
            (ThemeSongNode?, int, string?, ISongPlayer?) key = (Playing, FilterAnchor, Message, Player);
            bool playing = Playing != null && playingClip != null && Player != null;
            if (force || !key.Equals(statusKey))
            {
                statusKey = key;
                string m = ThemesPalette.Muted;
                List<string> lines = new();
                if (playing)
                {
                    ThemeSongRecord s = Playing!.Song;
                    string silent = Player is SilentSongPlayer ? $" · <color={m}>silent (no synth installed)</color>" : "";
                    lines.Add($"<color={ThemesPalette.ToHex(ThemesPalette.Theme)}>►</color> <b>{GraphHud.Esc(s.Title)}</b> — {GraphHud.Esc(s.Artist)} · {s.Year}" +
                              $"   <color={m}>{SongPalette.KeyName(s.TonicPc, s.Minor)} · {SongPalette.Invariant(s.NativeBpm, "0.#")} BPM · click it again or Space to stop</color>{silent}");
                }
                if (FilterAnchor != 0)
                {
                    ThemeAnchorRecord a = Data.Anchor(FilterAnchor);
                    lines.Add($"Showing <color={ThemesPalette.ToHex(ThemesPalette.Theme)}>{GraphHud.Esc(a.Label)}</color>: {a.TopCount} songs whose top theme it is" +
                              $"   <color={m}>Esc or {ThemeAnchorNode.KeyName(FilterAnchor)} shows all</color>");
                }
                if (Message != null) lines.Add(Message);
                Hud.SetStatus(lines.Count > 0 ? string.Join("\n", lines) : null, playing);
            }
            if (playing)
            {
                double length = playingClip!.ExcerptEndBeat - playingClip.ExcerptStartBeat;
                Hud.SetProgress(length > 0 ? (float)((Player!.CurrentBeat - playingClip.ExcerptStartBeat) / length) : 0f);
            }
        }

        // ------------------------------------------------------------------ scene switch

        /// <summary>True when the influence graph scene can be loaded (it is in the build settings).</summary>
        public static bool InfluenceSceneInBuild => SceneUtility.GetBuildIndexByScenePath(InfluenceScenePath) >= 0;

        public void GoToInfluenceGraph()
        {
            StopPlayback();
            if (!Application.isPlaying) return;
#if UNITY_EDITOR
            if (!InfluenceSceneInBuild)
            {
                EditorSceneManager.LoadSceneInPlayMode(InfluenceScenePath, new LoadSceneParameters(LoadSceneMode.Single));
                return;
            }
#endif
            SceneManager.LoadScene(InfluenceSceneName, LoadSceneMode.Single);
        }

        // ------------------------------------------------------------------ frame loop

        void Update()
        {
            if (Data == null) return;
            Camera? cam = ViewCamera;
            Keyboard? k = Keyboard.current;
            if (k != null) HandleKeys(k, cam);
            Mouse? mouse = Mouse.current;
            if (mouse != null && cam != null) HandlePointer(mouse, cam);
            if (Playing != null && Player != null && !Player.IsPlaying && playingClip != null && Player.CurrentBeat >= playingClip.ExcerptEndBeat)
                OnClipFinished(playingClip);
            RefreshStatus();
        }

        void HandleKeys(Keyboard k, Camera? cam)
        {
            Key[] digits = { Key.Digit1, Key.Digit2, Key.Digit3, Key.Digit4, Key.Digit5, Key.Digit6, Key.Digit7, Key.Digit8, Key.Digit9, Key.Digit0 };
            Key[] pad = { Key.Numpad1, Key.Numpad2, Key.Numpad3, Key.Numpad4, Key.Numpad5, Key.Numpad6, Key.Numpad7, Key.Numpad8, Key.Numpad9, Key.Numpad0 };
            for (int i = 0; i < digits.Length && i < Data!.Anchors.Count; i++)
                if (k[digits[i]].wasPressedThisFrame || k[pad[i]].wasPressedThisFrame) SetFilter(i + 1);
            if (k.escapeKey.wasPressedThisFrame && FilterAnchor != 0) SetFilter(0);
            if (k.lKey.wasPressedThisFrame) SetAllLabels(!AllLabels);
            if (k.hKey.wasPressedThisFrame) Hud.ToggleHelp();
            if (k.spaceKey.wasPressedThisFrame) StopPlayback();
            if ((k.rKey.wasPressedThisFrame || k.homeKey.wasPressedThisFrame) && cam != null) FrameOverview(cam);
            if (k.gKey.wasPressedThisFrame || k.backspaceKey.wasPressedThisFrame) GoToInfluenceGraph();
        }

        void HandlePointer(Mouse mouse, Camera cam)
        {
            Vector2 pointer = mouse.position.ReadValue();
            bool overButton = Hud.ButtonContains(pointer);
            Hud.SetButtonHot(overButton);
            ThemesOrbitCamera? orbit = cam.GetComponent<ThemesOrbitCamera>();
            bool dragging = (orbit != null && orbit.Dragging) || mouse.rightButton.isPressed || mouse.middleButton.isPressed;
            ThemeSongNode? hit = dragging || overButton ? null : Pick(cam, pointer);
            SetHover(hit);

            if (mouse.leftButton.wasPressedThisFrame)
            {
                pressed = true;
                pressedOnButton = overButton;
                pressPosition = pointer;
            }
            if (pressed && mouse.leftButton.wasReleasedThisFrame)
            {
                pressed = false;
                if ((pointer - pressPosition).sqrMagnitude < 36f)
                {
                    if (pressedOnButton && overButton) GoToInfluenceGraph();
                    else if (hit != null) TogglePlay(hit);
                }
            }
        }

        void LateUpdate()
        {
            Camera? cam = ViewCamera;
            if (Data == null || cam == null) return;
            Ring.PlaceLabels(cam);
            RefreshTethers(cam);
            Hud.PlaceCard(cam);
        }

        /// <summary>Theme labels, song labels, tethers and HUD for <paramref name="cam"/> now (edit-mode rendering).</summary>
        public void RefreshView(Camera cam)
        {
            for (int pass = 0; pass < 2; pass++)
            {
                Ring.PlaceLabels(cam);
                Labels.Refresh(cam);
            }
            RefreshTethers(cam);
            Labels.ForceMeshUpdate();
            RefreshStatus(force: true);
            Hud.PlaceCard(cam);
            Hud.ForceUpdate();
        }

        /// <summary>Destroys everything <see cref="Build"/> created.</summary>
        public void Clear()
        {
            StopPlayback();
            if (Labels != null) Labels.Clear();
            if (graphRoot != null) Discard(graphRoot);
            graphRoot = null;
            if (Hud != null && Hud.Canvas != null) Discard(Hud.Canvas.gameObject);
            nodes.Clear();
            pinned.Clear();
            requested.Clear();
            tethers.Clear();
            tetherMaterials.Clear();
            tetherScores.Clear();
            tetherNode = null;
            songPositions = Array.Empty<Vector3>();
            Hovered = null;
            FilterAnchor = 0;
            AllLabels = false;
            Message = null;
            ThemesMaterials.Clear();
            ThemeSongNode.ReleaseSharedMesh();
            ThemesRing.ReleaseSharedMesh();
            Data = null;
        }

        static void Discard(UnityEngine.Object o)
        {
            if (Application.isPlaying) Destroy(o);
            else DestroyImmediate(o);
        }

        void OnDestroy()
        {
            if (Player != null)
            {
                Player.Finished -= OnClipFinished;
                if (Player.IsPlaying) Player.Stop();
            }
            ThemesMaterials.Clear();
            ThemeSongNode.ReleaseSharedMesh();
            ThemesRing.ReleaseSharedMesh();
        }
    }
}
