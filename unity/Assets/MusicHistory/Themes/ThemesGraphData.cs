#nullable enable
using System;
using System.Collections.Generic;
using System.Data;
using System.Globalization;
using System.IO;
using Mono.Data.Sqlite;
using MusicHistory.Playback;
using UnityEngine;

namespace MusicHistory.Themes
{
    /// <summary>Singer of the lead act (docs/DESIGN.md §12 "Singer").</summary>
    public enum SingerGender
    {
        Male,
        Female,
        Mixed,
        Nonbinary,
        Unknown,
        Instrumental
    }

    /// <summary>One row of theme_anchor: a theme pinned on the ring.</summary>
    public sealed class ThemeAnchorRecord
    {
        public int AnchorId;
        /// <summary>The theme text, e.g. "I love you/him/her".</summary>
        public string Label = "";
        public string Short = "";
        /// <summary>Angle on the ring in degrees, from +x toward +z.</summary>
        public double AngleDegrees;
        public Vector3 Position;
        /// <summary>Songs whose top theme this is.</summary>
        public int TopCount;
    }

    /// <summary>
    /// One row of theme_song plus its theme_score row (docs/DESIGN.md §12). Holds only title,
    /// artist, year, singer, text source, scores and playback facts: the database has no lyrics.
    /// </summary>
    public sealed class ThemeSongRecord
    {
        public int NodeId;
        public string WorkId = "";
        public string Title = "";
        public string Artist = "";
        public int Year;
        /// <summary>singer_gender as stored ('male', 'female', ...).</summary>
        public string SingerGenderRaw = "";
        public SingerGender Gender = SingerGender.Unknown;
        /// <summary>'lyrics' or 'title'.</summary>
        public string TextSource = "";
        public int TopAnchor;
        public double TopScore;
        /// <summary>Score per anchor, Scores[anchorId - 1]; rows sum to 1.</summary>
        public double[] Scores = Array.Empty<double>();
        /// <summary>Layout position (NULL until the layout `themes` stage runs).</summary>
        public Vector3? LayoutPosition;
        /// <summary>Absolute MIDI path (resolved against the DB folder), or null.</summary>
        public string? MidiPath;
        public double ExcerptStartBeat;
        public double ExcerptEndBeat;
        public int TonicPc;
        public bool Minor;
        public double NativeBpm;
        public double BeatsPerBar;
        public double FirstDownbeat;

        public bool FromLyrics => string.Equals(TextSource, "lyrics", StringComparison.OrdinalIgnoreCase);

        /// <summary>The <paramref name="count"/> highest-scoring themes, best first (ties by anchor id).</summary>
        public List<(int anchorId, double score)> TopThemes(int count)
        {
            List<(int anchorId, double score)> all = new(Scores.Length);
            for (int i = 0; i < Scores.Length; i++) all.Add((i + 1, Scores[i]));
            all.Sort((a, b) => b.score != a.score ? b.score.CompareTo(a.score) : a.anchorId.CompareTo(b.anchorId));
            if (all.Count > count) all.RemoveRange(count, all.Count - count);
            return all;
        }

        /// <summary>The song's excerpt in its native key and tempo (the themes viewer never morphs).</summary>
        public SongClip ToClip() => new()
        {
            NodeId = NodeId,
            Title = Title,
            Artist = Artist,
            MidiPath = MidiPath ?? "",
            NormalizedMidiPath = null,
            ExcerptStartBeat = ExcerptStartBeat,
            ExcerptEndBeat = ExcerptEndBeat,
            TonicPc = TonicPc,
            Minor = Minor,
            NativeBpm = NativeBpm,
            BeatsPerBar = BeatsPerBar,
            FirstDownbeat = FirstDownbeat,
            NormShift = 0
        };
    }

    /// <summary>The latest themes_layout_run row (absent until the layout stage has run).</summary>
    public sealed class ThemesLayoutRunInfo
    {
        public bool Present;
        public int RunId;
        public string? CreatedAt;
        public string? Device;
        public int Iterations;
        public double? Sharpen;
        public double? Repulsion;
        public double? FinalMeanMove;
    }

    public sealed class ThemesGraphData
    {
        public string DbPath = "";
        public string DbFolder = "";
        public readonly Dictionary<string, string?> Meta = new(StringComparer.Ordinal);
        /// <summary>Anchors by id: Anchors[anchorId - 1].</summary>
        public readonly List<ThemeAnchorRecord> Anchors = new();
        /// <summary>Songs by node id: Songs[nodeId - 1].</summary>
        public readonly List<ThemeSongRecord> Songs = new();
        public ThemesLayoutRunInfo LayoutRun = new();
        /// <summary>True when every song has a layout position.</summary>
        public bool HasPositions;
        /// <summary>Contract violations found while reading (empty = conformant).</summary>
        public readonly List<string> Problems = new();
        /// <summary>Ring radius: themes_meta ring_radius, else the mean anchor distance from the centre.</summary>
        public float RingRadius = 40f;
        public int ScoreRows;

        public ThemeSongRecord Song(int nodeId) => Songs[nodeId - 1];
        public ThemeAnchorRecord Anchor(int anchorId) => Anchors[anchorId - 1];

        public string? MetaValue(string key) => Meta.TryGetValue(key, out string? v) ? v : null;

        public int? MetaInt(string key) =>
            int.TryParse(MetaValue(key), NumberStyles.Integer, CultureInfo.InvariantCulture, out int v) ? v : null;

        public double? MetaDouble(string key) =>
            double.TryParse(MetaValue(key), NumberStyles.Float, CultureInfo.InvariantCulture, out double v) ? v : null;

        public bool IsSynthetic => MetaValue("synthetic") == "1";

        public int LyricsCount
        {
            get
            {
                int n = 0;
                foreach (ThemeSongRecord s in Songs)
                    if (s.FromLyrics) n++;
                return n;
            }
        }

        public int CountGender(SingerGender g)
        {
            int n = 0;
            foreach (ThemeSongRecord s in Songs)
                if (s.Gender == g) n++;
            return n;
        }
    }

    /// <summary>
    /// Reads the themes graph database (docs/DESIGN.md §12) with Unity's Mono.Data.Sqlite /
    /// SQLite 3.15, resolves MIDI paths against the DB folder and checks the invariants the viewer
    /// relies on. It selects only the columns below; none of them holds lyric text.
    /// </summary>
    public static class ThemesGraphReader
    {
        public const string CommandLineFlag = "-themesDb";
        public const string DefaultFileName = "themes_graph.db";
        public const string DemoFileName = "themes_demo.db";

        public static readonly string[] Genders = { "male", "female", "mixed", "nonbinary", "unknown", "instrumental" };
        public static readonly string[] TextSources = { "lyrics", "title" };

        /// <summary>The ten themes in anchor order (DESIGN.md §12); the database's labels must match.</summary>
        public static readonly string[] DesignThemes =
        {
            "I would be so good to you/him/her",
            "I'm sad you/she/he don't/doesn't love me",
            "I love you/him/her",
            "I wish you/he/she loved me",
            "I don't need/love you/him/her",
            "I hate that I love you/him/her",
            "I miss you/him/her",
            "Let's all love each other",
            "What is going on in the world?",
            "Other"
        };

        public const string MetaQuery = "SELECT key, value FROM themes_meta";
        public const string AnchorQuery =
            "SELECT anchor_id, label, short, angle, position_x, position_y, position_z FROM theme_anchor ORDER BY anchor_id";
        public const string SongQuery =
            "SELECT node_id, work_id, title, artist, year, singer_gender, text_source, top_anchor, top_score, " +
            "position_x, position_y, position_z, midi_path, excerpt_start_beat, excerpt_end_beat, tonic_pc, mode, " +
            "native_bpm, beats_per_bar, first_downbeat FROM theme_song ORDER BY node_id";
        public const string ScoreQuery = "SELECT node_id, anchor_id, score FROM theme_score";
        public const string LayoutRunQuery =
            "SELECT run_id, created_at, device, iterations, sharpen, repulsion, final_mean_move " +
            "FROM themes_layout_run ORDER BY run_id DESC LIMIT 1";

        public static ThemesGraphData Read(string dbPath)
        {
            string fullPath = Path.GetFullPath(dbPath);
            if (!File.Exists(fullPath)) throw new FileNotFoundException("themes graph database not found", fullPath);
            ThemesGraphData data = new()
            {
                DbPath = fullPath,
                DbFolder = Path.GetDirectoryName(fullPath) ?? ""
            };
            using SqliteConnection conn = new("URI=file:" + fullPath);
            conn.Open();
            ReadMeta(conn, data);
            ReadAnchors(conn, data);
            ReadSongs(conn, data);
            ReadScores(conn, data);
            ReadLayoutRun(conn, data);
            conn.Close();
            Validate(data);
            return data;
        }

        static void ReadMeta(SqliteConnection conn, ThemesGraphData data)
        {
            try
            {
                using IDbCommand cmd = conn.CreateCommand();
                cmd.CommandText = MetaQuery;
                using IDataReader r = cmd.ExecuteReader();
                while (r.Read())
                {
                    string? key = Str(r, 0);
                    if (key != null) data.Meta[key] = Str(r, 1);
                }
            }
            catch (SqliteException e)
            {
                data.Problems.Add($"themes_meta unreadable: {e.Message}");
            }
        }

        static void ReadAnchors(SqliteConnection conn, ThemesGraphData data)
        {
            bool radians = string.Equals(data.MetaValue("angle_unit"), "radians", StringComparison.OrdinalIgnoreCase);
            using IDbCommand cmd = conn.CreateCommand();
            cmd.CommandText = AnchorQuery;
            using IDataReader r = cmd.ExecuteReader();
            while (r.Read())
            {
                double angle = Dbl(r, 3) ?? 0;
                ThemeAnchorRecord a = new()
                {
                    AnchorId = Int(r, 0) ?? 0,
                    Label = Str(r, 1) ?? "",
                    Short = Str(r, 2) ?? "",
                    AngleDegrees = radians ? angle * 180.0 / Math.PI : angle,
                    Position = new Vector3((float)(Dbl(r, 4) ?? 0), (float)(Dbl(r, 5) ?? 0), (float)(Dbl(r, 6) ?? 0))
                };
                data.Anchors.Add(a);
            }
            double sum = 0;
            foreach (ThemeAnchorRecord a in data.Anchors) sum += new Vector2(a.Position.x, a.Position.z).magnitude;
            double? meta = data.MetaDouble("ring_radius");
            data.RingRadius = (float)(meta is double m && m > 0 ? m : data.Anchors.Count > 0 ? sum / data.Anchors.Count : 40);
        }

        static void ReadSongs(SqliteConnection conn, ThemesGraphData data)
        {
            using IDbCommand cmd = conn.CreateCommand();
            cmd.CommandText = SongQuery;
            using IDataReader r = cmd.ExecuteReader();
            int withPosition = 0;
            while (r.Read())
            {
                string gender = (Str(r, 5) ?? "").Trim().ToLowerInvariant();
                ThemeSongRecord s = new()
                {
                    NodeId = Int(r, 0) ?? 0,
                    WorkId = Str(r, 1) ?? "",
                    Title = Str(r, 2) ?? "",
                    Artist = Str(r, 3) ?? "",
                    Year = Int(r, 4) ?? 0,
                    SingerGenderRaw = gender,
                    Gender = ParseGender(gender),
                    TextSource = (Str(r, 6) ?? "").Trim().ToLowerInvariant(),
                    TopAnchor = Int(r, 7) ?? 0,
                    TopScore = Dbl(r, 8) ?? 0,
                    MidiPath = ResolveRelative(data.DbFolder, Str(r, 12)),
                    ExcerptStartBeat = Dbl(r, 13) ?? 0,
                    ExcerptEndBeat = Dbl(r, 14) ?? 0,
                    TonicPc = (((Int(r, 15) ?? 0) % 12) + 12) % 12,
                    Minor = string.Equals((Str(r, 16) ?? "").Trim(), "minor", StringComparison.OrdinalIgnoreCase),
                    NativeBpm = Dbl(r, 17) ?? 120,
                    BeatsPerBar = Dbl(r, 18) ?? 4,
                    FirstDownbeat = Dbl(r, 19) ?? 0,
                    Scores = new double[data.Anchors.Count]
                };
                double? x = Dbl(r, 9), y = Dbl(r, 10), z = Dbl(r, 11);
                if (x.HasValue && y.HasValue && z.HasValue && IsFinite(x.Value) && IsFinite(y.Value) && IsFinite(z.Value))
                {
                    s.LayoutPosition = new Vector3((float)x.Value, (float)y.Value, (float)z.Value);
                    withPosition++;
                }
                data.Songs.Add(s);
            }
            data.HasPositions = data.Songs.Count > 0 && withPosition == data.Songs.Count;
            if (withPosition > 0 && !data.HasPositions)
                data.Problems.Add($"only {withPosition} of {data.Songs.Count} songs have positions");
        }

        static void ReadScores(SqliteConnection conn, ThemesGraphData data)
        {
            int n = data.Songs.Count, k = data.Anchors.Count;
            using IDbCommand cmd = conn.CreateCommand();
            cmd.CommandText = ScoreQuery;
            using IDataReader r = cmd.ExecuteReader();
            int bad = 0;
            while (r.Read())
            {
                data.ScoreRows++;
                int node = Int(r, 0) ?? 0, anchor = Int(r, 1) ?? 0;
                double score = Dbl(r, 2) ?? double.NaN;
                if (node < 1 || node > n || anchor < 1 || anchor > k || data.Songs[node - 1].NodeId != node)
                {
                    bad++;
                    continue;
                }
                data.Songs[node - 1].Scores[anchor - 1] = score;
            }
            if (bad > 0) data.Problems.Add($"{bad} theme_score rows reference a missing song or anchor");
            foreach (ThemeAnchorRecord a in data.Anchors) a.TopCount = 0;
            foreach (ThemeSongRecord s in data.Songs)
                if (s.TopAnchor >= 1 && s.TopAnchor <= k) data.Anchors[s.TopAnchor - 1].TopCount++;
        }

        static void ReadLayoutRun(SqliteConnection conn, ThemesGraphData data)
        {
            try
            {
                using IDbCommand cmd = conn.CreateCommand();
                cmd.CommandText = LayoutRunQuery;
                using IDataReader r = cmd.ExecuteReader();
                if (!r.Read()) return;
                data.LayoutRun = new ThemesLayoutRunInfo
                {
                    Present = true,
                    RunId = Int(r, 0) ?? 0,
                    CreatedAt = Str(r, 1),
                    Device = Str(r, 2),
                    Iterations = Int(r, 3) ?? 0,
                    Sharpen = Dbl(r, 4),
                    Repulsion = Dbl(r, 5),
                    FinalMeanMove = Dbl(r, 6)
                };
            }
            catch (SqliteException)
            {
                // Older exports without the table: no layout run.
            }
        }

        /// <summary>The §12 invariants the viewer depends on. Problems are collected, not thrown.</summary>
        public static void Validate(ThemesGraphData data)
        {
            List<string> p = data.Problems;
            int k = data.Anchors.Count, n = data.Songs.Count;
            if (k == 0) p.Add("theme_anchor is empty");
            for (int i = 0; i < k; i++)
                if (data.Anchors[i].AnchorId != i + 1) { p.Add($"anchor ids are not 1..{k}"); break; }
            for (int i = 0; i < n; i++)
            {
                if (data.Songs[i].NodeId == i + 1) continue;
                p.Add($"node ids are not contiguous 1..N (row {i + 1} has id {data.Songs[i].NodeId})");
                return;
            }
            for (int i = 1; i < n; i++)
            {
                ThemeSongRecord a = data.Songs[i - 1], b = data.Songs[i];
                if (a.Year < b.Year || (a.Year == b.Year && string.CompareOrdinal(a.WorkId, b.WorkId) < 0)) continue;
                p.Add($"node {b.NodeId} is not ordered by (year, work_id)");
                break;
            }
            if (data.ScoreRows != n * k) p.Add($"theme_score has {data.ScoreRows} rows, expected {n * k} (songs x anchors)");

            int badSum = 0, badTop = 0, badGender = 0, badSource = 0, badRange = 0;
            foreach (ThemeSongRecord s in data.Songs)
            {
                double sum = 0, best = double.MinValue;
                foreach (double v in s.Scores)
                {
                    if (!(v >= -1e-9 && v <= 1 + 1e-9)) badRange++;
                    sum += v;
                    best = Math.Max(best, v);
                }
                if (Math.Abs(sum - 1) > 1e-3) badSum++;
                if (s.TopAnchor < 1 || s.TopAnchor > k || Math.Abs(s.Scores[s.TopAnchor - 1] - best) > 1e-6 || Math.Abs(s.TopScore - best) > 1e-6)
                    badTop++;
                if (Array.IndexOf(Genders, s.SingerGenderRaw) < 0) badGender++;
                if (Array.IndexOf(TextSources, s.TextSource) < 0) badSource++;
            }
            if (badRange > 0) p.Add($"{badRange} scores outside [0, 1]");
            if (badSum > 0) p.Add($"{badSum} songs whose scores do not sum to 1");
            if (badTop > 0) p.Add($"{badTop} songs whose top_anchor/top_score is not their highest score");
            if (badGender > 0) p.Add($"{badGender} songs with an unknown singer_gender");
            if (badSource > 0) p.Add($"{badSource} songs with a text_source other than 'lyrics'/'title'");

            CheckMetaCount(data, "song_count", n);
            CheckMetaCount(data, "lyrics_count", data.LyricsCount);
            CheckMetaCount(data, "title_count", n - data.LyricsCount);
        }

        static void CheckMetaCount(ThemesGraphData data, string key, int actual)
        {
            int? declared = data.MetaInt(key);
            if (declared.HasValue && declared.Value != actual)
                data.Problems.Add($"themes_meta {key}={declared.Value} but the tables hold {actual}");
        }

        public static SingerGender ParseGender(string? raw) => (raw ?? "").Trim().ToLowerInvariant() switch
        {
            "male" => SingerGender.Male,
            "female" => SingerGender.Female,
            "mixed" => SingerGender.Mixed,
            "nonbinary" => SingerGender.Nonbinary,
            "instrumental" => SingerGender.Instrumental,
            _ => SingerGender.Unknown
        };

        /// <summary>A path stored relative to the DB folder with '/' separators, made absolute.</summary>
        public static string? ResolveRelative(string dbFolder, string? stored)
        {
            if (string.IsNullOrWhiteSpace(stored)) return null;
            string normalized = stored!.Replace('/', Path.DirectorySeparatorChar);
            return Path.IsPathRooted(normalized)
                ? Path.GetFullPath(normalized)
                : Path.GetFullPath(Path.Combine(dbFolder, normalized));
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

        /// <summary>
        /// -themesDb &lt;path&gt;, else the inspector's path, else &lt;repo&gt;/data/graph/themes_graph.db,
        /// else StreamingAssets/themes_graph.db (player builds), else &lt;repo&gt;/data/graph/themes_demo.db.
        /// </summary>
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
                (Path.Combine(repo, "data", "graph", DefaultFileName), "pipeline themes graph"),
                (Path.Combine(Application.streamingAssetsPath, DefaultFileName), "StreamingAssets"),
                (Path.Combine(repo, "data", "graph", DemoFileName), "themes demo (themes_graph.db not found)")
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

        /// <summary>
        /// Positions for a database the layout stage has not reached yet: each song starts at the
        /// barycentre of the anchors weighted by score^<paramref name="sharpen"/> (the layout's
        /// repulsion-free equilibrium), then overlapping songs are pushed apart (position-based,
        /// deterministic) while a spring of stiffness Σ score^γ pulls each back, so peaked songs stay
        /// on their anchor and spread songs give way. Returns positions in node order (y = 0).
        /// </summary>
        public static Vector3[] FallbackPositions(ThemesGraphData data, float minDistance, float sharpen = 2f, int iterations = 300)
        {
            int n = data.Songs.Count, k = data.Anchors.Count;
            Vector2[] target = new Vector2[n];
            float[] stiffness = new float[n];
            for (int i = 0; i < n; i++)
            {
                double sw = 0, bx = 0, bz = 0;
                double[] scores = data.Songs[i].Scores;
                for (int a = 0; a < k && a < scores.Length; a++)
                {
                    double w = Math.Pow(Math.Max(0, scores[a]), sharpen);
                    sw += w;
                    bx += w * data.Anchors[a].Position.x;
                    bz += w * data.Anchors[a].Position.z;
                }
                target[i] = sw > 0 ? new Vector2((float)(bx / sw), (float)(bz / sw)) : Vector2.zero;
                stiffness[i] = (float)Math.Min(1, sw);
            }

            const float golden = 2.39996323f;
            Vector2[] p = new Vector2[n];
            for (int i = 0; i < n; i++)
            {
                float a = i * golden, r = minDistance * .05f * Mathf.Sqrt(1 + i % 23);
                p[i] = target[i] + new Vector2(Mathf.Cos(a), Mathf.Sin(a)) * r;
            }

            float cell = Mathf.Max(1e-3f, minDistance);
            Dictionary<long, int> heads = new(n * 2);
            int[] next = new int[n];
            float md2 = minDistance * minDistance;
            // Springs and collisions for the first two thirds, then collisions alone so every overlap resolves.
            int springIterations = Mathf.Max(1, iterations * 2 / 3);
            for (int it = 0; it < iterations; it++)
            {
                if (it < springIterations)
                {
                    float pull = Mathf.Lerp(.3f, .02f, it / (float)springIterations);
                    for (int i = 0; i < n; i++) p[i] += (target[i] - p[i]) * (pull * (.25f + .75f * stiffness[i]));
                }
                heads.Clear();
                for (int i = 0; i < n; i++)
                {
                    long key = CellKey(Mathf.FloorToInt(p[i].x / cell), Mathf.FloorToInt(p[i].y / cell));
                    next[i] = heads.TryGetValue(key, out int head) ? head : -1;
                    heads[key] = i;
                }
                for (int i = 0; i < n; i++)
                {
                    int cx = Mathf.FloorToInt(p[i].x / cell), cz = Mathf.FloorToInt(p[i].y / cell);
                    for (int dx = -1; dx <= 1; dx++)
                    for (int dz = -1; dz <= 1; dz++)
                    {
                        if (!heads.TryGetValue(CellKey(cx + dx, cz + dz), out int j)) continue;
                        for (; j >= 0; j = next[j])
                        {
                            if (j <= i) continue;
                            Vector2 d = p[j] - p[i];
                            float d2 = d.sqrMagnitude;
                            if (d2 >= md2) continue;
                            float dist = Mathf.Sqrt(d2);
                            Vector2 dir;
                            if (dist < 1e-5f)
                            {
                                float a = (i * 7919 + j * 104729) * golden;
                                dir = new Vector2(Mathf.Cos(a), Mathf.Sin(a));
                            }
                            else dir = d / dist;
                            float overlap = minDistance - dist;
                            float mi = 1f / (.2f + stiffness[i]), mj = 1f / (.2f + stiffness[j]);
                            float share = overlap / (mi + mj);
                            p[i] -= dir * (share * mi);
                            p[j] += dir * (share * mj);
                        }
                    }
                }
            }

            Vector3[] result = new Vector3[n];
            for (int i = 0; i < n; i++) result[i] = new Vector3(p[i].x, 0f, p[i].y);
            return result;
        }

        static long CellKey(int x, int z) => ((long)x << 32) ^ (uint)z;

        static bool IsFinite(double v) => !double.IsNaN(v) && !double.IsInfinity(v);

        static string? Str(IDataRecord r, int i) =>
            r.IsDBNull(i) ? null : Convert.ToString(r.GetValue(i), CultureInfo.InvariantCulture);

        static double? Dbl(IDataRecord r, int i)
        {
            if (r.IsDBNull(i)) return null;
            object v = r.GetValue(i);
            if (v is string text)
                return double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out double parsed) ? parsed : null;
            return Convert.ToDouble(v, CultureInfo.InvariantCulture);
        }

        static int? Int(IDataRecord r, int i)
        {
            double? d = Dbl(r, i);
            return d.HasValue ? (int)Math.Round(d.Value) : null;
        }
    }
}
