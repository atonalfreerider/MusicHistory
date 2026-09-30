#nullable enable
using System;
using System.Collections.Generic;
using System.Data;
using System.Globalization;
using System.IO;
using Mono.Data.Sqlite;
using MusicHistory.Playback;
using UnityEngine;

namespace MusicHistory.Viewer
{
    /// <summary>One row of song_node (+ its nodes position), docs/DESIGN.md §10.</summary>
    public sealed class SongRecord
    {
        public int NodeId;
        public string WorkId = "";
        public string Title = "";
        public string Artist = "";
        public int Year;
        public string? ReleaseDate;
        public int? DatePrecision;
        public double TimeValue;
        public int? CanonRank;
        public int TonicPc;
        public bool Minor;
        public string KeyName = "";
        public int NormShift;
        public double NativeBpm;
        public double BeatsPerBar;
        public double FirstDownbeat;
        /// <summary>Absolute path (resolved against the DB folder).</summary>
        public string MidiPath = "";
        public string? NormalizedMidiPath;
        public string? MidiSource;
        public double ExcerptStartBeat;
        public double ExcerptEndBeat;
        public int? TreeParent;
        public int TreeRoot;
        public int TreeDepth;
        public int RefCount;
        public double? RefNorm;
        public double? Katz;
        public int Descendants;
        public int InDegree;
        public int OutDegree;
        public double? KeyConfidence;
        public double? MelodyConfidence;
        public string? MainLoop;
        public string? Summary;
        /// <summary>Layout position (NULL until layout runs).</summary>
        public Vector3? LayoutPosition;
        public double? LayoutMass;
        public double? LayoutDisplayRadius;

        public bool IsRoot => TreeParent == null;

        public SongClip ToClip() => new()
        {
            NodeId = NodeId,
            Title = Title,
            Artist = Artist,
            MidiPath = MidiPath,
            NormalizedMidiPath = NormalizedMidiPath,
            ExcerptStartBeat = ExcerptStartBeat,
            ExcerptEndBeat = ExcerptEndBeat,
            TonicPc = TonicPc,
            Minor = Minor,
            NativeBpm = NativeBpm,
            BeatsPerBar = BeatsPerBar,
            FirstDownbeat = FirstDownbeat,
            NormShift = NormShift
        };
    }

    /// <summary>One row of influence_edges. Source is always the earlier song.</summary>
    public sealed class EdgeRecord
    {
        public int Id;
        public int Source;
        public int Target;
        public bool IsTree;
        public string Channels = "";
        public string PrimaryChannel = "";
        public double ScoreBits;
        public double Z;
        public double? Q;
        public double Similarity;
        public double Weight;
        public double? SrcStartBeat;
        public double? SrcEndBeat;
        public double? DstStartBeat;
        public double? DstEndBeat;
        public string? Evidence;
    }

    /// <summary>The latest layout_run row, or defaults matching the layout stage when there is none.</summary>
    public sealed class LayoutRunInfo
    {
        public bool Present;
        public string TimeAxis = "y";
        public string TimeDirection = "up";
        public double YearScale = 2;
        public double? MinTime;
        public string? Device;
        public int Iterations;
        public double? RadiusScale;
        public string? CreatedAt;
    }

    public sealed class SongGraphData
    {
        public string DbPath = "";
        public string DbFolder = "";
        public readonly Dictionary<string, string?> Meta = new(StringComparer.Ordinal);
        /// <summary>Songs by node id: Songs[nodeId - 1].</summary>
        public readonly List<SongRecord> Songs = new();
        public readonly List<EdgeRecord> Edges = new();
        public LayoutRunInfo Layout = new();
        /// <summary>True when every node has a layout position.</summary>
        public bool HasPositions;
        /// <summary>Contract violations found while reading (empty = conformant).</summary>
        public readonly List<string> Problems = new();
        public int RootCount;
        public int TreeEdgeCount;
        public double MinTime;
        public double MaxTime;

        public SongRecord Song(int nodeId) => Songs[nodeId - 1];

        public string? MetaValue(string key) => Meta.TryGetValue(key, out string? v) ? v : null;

        public int? MetaInt(string key) =>
            int.TryParse(MetaValue(key), NumberStyles.Integer, CultureInfo.InvariantCulture, out int v) ? v : null;

        public double? MetaDouble(string key) =>
            double.TryParse(MetaValue(key), NumberStyles.Float, CultureInfo.InvariantCulture, out double v) ? v : null;

        public bool IsSynthetic => MetaValue("synthetic") == "1";
    }

    /// <summary>
    /// Reads the graph database (docs/DESIGN.md §10) with Unity's Mono.Data.Sqlite / SQLite 3.15,
    /// resolves MIDI paths against the DB folder and checks the invariants the viewer relies on.
    /// Tables written by later stages (node_layout_metadata, layout_run) are optional.
    /// </summary>
    public static class SongGraphReader
    {
        const string SongQuery =
            "SELECT s.node_id, s.work_id, s.title, s.artist, s.year, s.release_date, s.date_precision, " +
            "s.time_value, s.canon_rank, s.tonic_pc, s.mode, s.key_name, s.norm_shift, s.native_bpm, " +
            "s.beats_per_bar, s.first_downbeat, s.midi_path, s.normalized_midi_path, s.midi_source, " +
            "s.excerpt_start_beat, s.excerpt_end_beat, s.tree_parent_node, s.tree_root_node, s.tree_depth, " +
            "s.ref_count, s.ref_norm, s.katz, s.descendants, s.in_degree, s.out_degree, s.key_confidence, " +
            "s.melody_confidence, s.main_loop, s.summary, n.position_x, n.position_y, n.position_z " +
            "FROM song_node s LEFT JOIN nodes n ON n.id = s.node_id ORDER BY s.node_id";

        const string EdgeQuery =
            "SELECT id, source_node, target_node, kind, channels, primary_channel, score_bits, z, q, " +
            "similarity, weight, src_start_beat, src_end_beat, dst_start_beat, dst_end_beat, evidence " +
            "FROM influence_edges ORDER BY id";

        public static SongGraphData Read(string dbPath)
        {
            string fullPath = Path.GetFullPath(dbPath);
            if (!File.Exists(fullPath)) throw new FileNotFoundException("graph database not found", fullPath);
            SongGraphData data = new()
            {
                DbPath = fullPath,
                DbFolder = Path.GetDirectoryName(fullPath) ?? ""
            };

            using SqliteConnection conn = new("URI=file:" + fullPath);
            conn.Open();
            ReadMeta(conn, data);
            ReadSongs(conn, data);
            ReadLayoutMetadata(conn, data);
            ReadLayoutRun(conn, data);
            ReadEdges(conn, data);
            conn.Close();
            Validate(data);
            return data;
        }

        static void ReadMeta(SqliteConnection conn, SongGraphData data)
        {
            using IDbCommand cmd = conn.CreateCommand();
            cmd.CommandText = "SELECT key, value FROM graph_meta";
            try
            {
                using IDataReader r = cmd.ExecuteReader();
                while (r.Read())
                {
                    string? key = Str(r, 0);
                    if (key != null) data.Meta[key] = Str(r, 1);
                }
            }
            catch (SqliteException e)
            {
                data.Problems.Add($"graph_meta unreadable: {e.Message}");
            }
        }

        static void ReadSongs(SqliteConnection conn, SongGraphData data)
        {
            using IDbCommand cmd = conn.CreateCommand();
            cmd.CommandText = SongQuery;
            using IDataReader r = cmd.ExecuteReader();
            int withPosition = 0;
            while (r.Read())
            {
                SongRecord s = new()
                {
                    NodeId = Int(r, 0) ?? 0,
                    WorkId = Str(r, 1) ?? "",
                    Title = Str(r, 2) ?? "",
                    Artist = Str(r, 3) ?? "",
                    Year = Int(r, 4) ?? 0,
                    ReleaseDate = Str(r, 5),
                    DatePrecision = Int(r, 6),
                    TimeValue = Dbl(r, 7) ?? 0,
                    CanonRank = Int(r, 8),
                    TonicPc = SongPalette.Wrap12(Int(r, 9) ?? 0),
                    Minor = string.Equals(Str(r, 10), "minor", StringComparison.OrdinalIgnoreCase),
                    KeyName = Str(r, 11) ?? "",
                    NormShift = Int(r, 12) ?? 0,
                    NativeBpm = Dbl(r, 13) ?? 120,
                    BeatsPerBar = Dbl(r, 14) ?? 4,
                    FirstDownbeat = Dbl(r, 15) ?? 0,
                    MidiPath = ResolveRelative(data.DbFolder, Str(r, 16)) ?? "",
                    NormalizedMidiPath = ResolveRelative(data.DbFolder, Str(r, 17)),
                    MidiSource = Str(r, 18),
                    ExcerptStartBeat = Dbl(r, 19) ?? 0,
                    ExcerptEndBeat = Dbl(r, 20) ?? 0,
                    TreeParent = Int(r, 21),
                    TreeRoot = Int(r, 22) ?? 0,
                    TreeDepth = Int(r, 23) ?? 0,
                    RefCount = Int(r, 24) ?? 0,
                    RefNorm = Dbl(r, 25),
                    Katz = Dbl(r, 26),
                    Descendants = Int(r, 27) ?? 0,
                    InDegree = Int(r, 28) ?? 0,
                    OutDegree = Int(r, 29) ?? 0,
                    KeyConfidence = Dbl(r, 30),
                    MelodyConfidence = Dbl(r, 31),
                    MainLoop = Str(r, 32),
                    Summary = Str(r, 33)
                };
                if (string.IsNullOrEmpty(s.KeyName)) s.KeyName = SongPalette.KeyName(s.TonicPc, s.Minor);
                double? x = Dbl(r, 34), y = Dbl(r, 35), z = Dbl(r, 36);
                if (x.HasValue && y.HasValue && z.HasValue && IsFinite(x.Value) && IsFinite(y.Value) && IsFinite(z.Value))
                {
                    s.LayoutPosition = new Vector3((float)x.Value, (float)y.Value, (float)z.Value);
                    withPosition++;
                }
                data.Songs.Add(s);
            }
            data.HasPositions = data.Songs.Count > 0 && withPosition == data.Songs.Count;
            if (data.Songs.Count > 0)
            {
                data.MinTime = double.MaxValue;
                data.MaxTime = double.MinValue;
                foreach (SongRecord s in data.Songs)
                {
                    data.MinTime = Math.Min(data.MinTime, s.TimeValue);
                    data.MaxTime = Math.Max(data.MaxTime, s.TimeValue);
                }
            }
        }

        static void ReadLayoutMetadata(SqliteConnection conn, SongGraphData data)
        {
            using IDbCommand cmd = conn.CreateCommand();
            cmd.CommandText = "SELECT node_id, mass, display_radius FROM node_layout_metadata";
            try
            {
                using IDataReader r = cmd.ExecuteReader();
                while (r.Read())
                {
                    int id = Int(r, 0) ?? 0;
                    if (id < 1 || id > data.Songs.Count) continue;
                    SongRecord s = data.Songs[id - 1];
                    if (s.NodeId != id) continue;
                    s.LayoutMass = Dbl(r, 1);
                    s.LayoutDisplayRadius = Dbl(r, 2);
                }
            }
            catch (SqliteException)
            {
                // Layout has not run yet: the table is optional.
            }
        }

        static void ReadLayoutRun(SqliteConnection conn, SongGraphData data)
        {
            using IDbCommand cmd = conn.CreateCommand();
            cmd.CommandText =
                "SELECT time_axis, time_direction, year_scale, min_time, device, iterations, params_json, created_at " +
                "FROM layout_run ORDER BY run_id DESC LIMIT 1";
            try
            {
                using IDataReader r = cmd.ExecuteReader();
                if (!r.Read()) return;
                LayoutRunInfo info = new()
                {
                    Present = true,
                    TimeAxis = (Str(r, 0) ?? "y").Trim().ToLowerInvariant(),
                    TimeDirection = (Str(r, 1) ?? "up").Trim().ToLowerInvariant(),
                    YearScale = Dbl(r, 2) ?? 2,
                    MinTime = Dbl(r, 3),
                    Device = Str(r, 4),
                    Iterations = Int(r, 5) ?? 0,
                    RadiusScale = JsonNumber(Str(r, 6), "radiusScale"),
                    CreatedAt = Str(r, 7)
                };
                data.Layout = info;
            }
            catch (SqliteException)
            {
                // No layout_run table: defaults stand.
            }
        }

        static void ReadEdges(SqliteConnection conn, SongGraphData data)
        {
            using IDbCommand cmd = conn.CreateCommand();
            cmd.CommandText = EdgeQuery;
            using IDataReader r = cmd.ExecuteReader();
            while (r.Read())
            {
                EdgeRecord e = new()
                {
                    Id = Int(r, 0) ?? 0,
                    Source = Int(r, 1) ?? 0,
                    Target = Int(r, 2) ?? 0,
                    IsTree = string.Equals(Str(r, 3), "tree", StringComparison.OrdinalIgnoreCase),
                    Channels = Str(r, 4) ?? "",
                    PrimaryChannel = Str(r, 5) ?? "",
                    ScoreBits = Dbl(r, 6) ?? 0,
                    Z = Dbl(r, 7) ?? 0,
                    Q = Dbl(r, 8),
                    Similarity = Dbl(r, 9) ?? 0,
                    Weight = Dbl(r, 10) ?? 0,
                    SrcStartBeat = Dbl(r, 11),
                    SrcEndBeat = Dbl(r, 12),
                    DstStartBeat = Dbl(r, 13),
                    DstEndBeat = Dbl(r, 14),
                    Evidence = Str(r, 15)
                };
                data.Edges.Add(e);
            }
        }

        /// <summary>The §10 invariants the viewer depends on. Problems are collected, not thrown.</summary>
        public static void Validate(SongGraphData data)
        {
            List<string> p = data.Problems;
            int n = data.Songs.Count;
            for (int i = 0; i < n; i++)
            {
                if (data.Songs[i].NodeId != i + 1)
                {
                    p.Add($"node ids are not contiguous 1..N (row {i + 1} has id {data.Songs[i].NodeId})");
                    return; // everything below indexes by id
                }
            }

            for (int i = 1; i < n; i++)
            {
                SongRecord a = data.Songs[i - 1], b = data.Songs[i];
                if (a.TimeValue > b.TimeValue ||
                    (a.TimeValue == b.TimeValue && string.CompareOrdinal(a.WorkId, b.WorkId) > 0))
                {
                    p.Add($"node {b.NodeId} is not ordered by (time_value, work_id)");
                    break;
                }
            }

            int[] treeIn = new int[n + 1];
            int[] treeSource = new int[n + 1];
            int[] inDeg = new int[n + 1];
            int[] outDeg = new int[n + 1];
            data.TreeEdgeCount = 0;
            foreach (EdgeRecord e in data.Edges)
            {
                if (e.Source < 1 || e.Source > n || e.Target < 1 || e.Target > n)
                {
                    p.Add($"edge {e.Id} references a missing node ({e.Source}->{e.Target})");
                    continue;
                }
                inDeg[e.Target]++;
                outDeg[e.Source]++;
                if (e.Source >= e.Target) p.Add($"edge {e.Id}: source_node >= target_node");
                if (!(data.Song(e.Source).TimeValue < data.Song(e.Target).TimeValue))
                    p.Add($"edge {e.Id}: source is not earlier than target");
                if (e.IsTree)
                {
                    data.TreeEdgeCount++;
                    treeIn[e.Target]++;
                    treeSource[e.Target] = e.Source;
                }
            }

            int[] descendants = new int[n + 1];
            data.RootCount = 0;
            // Node ids are ordered by time and parents are earlier, so a reverse sweep sums subtrees.
            for (int id = n; id >= 1; id--)
            {
                SongRecord s = data.Song(id);
                if (s.TreeParent is int parent)
                {
                    if (parent < 1 || parent >= id) { p.Add($"node {id}: tree parent {parent} is not an earlier node"); continue; }
                    if (treeIn[id] != 1 || treeSource[id] != parent)
                        p.Add($"node {id}: expected exactly one tree edge from {parent} (found {treeIn[id]})");
                    descendants[parent] += 1 + descendants[id];
                }
                else if (treeIn[id] != 0)
                {
                    p.Add($"root {id} has an incoming tree edge");
                }
            }

            for (int id = 1; id <= n; id++)
            {
                SongRecord s = data.Song(id);
                if (s.TreeParent == null)
                {
                    data.RootCount++;
                    if (s.TreeRoot != id || s.TreeDepth != 0) p.Add($"root {id}: tree_root_node/tree_depth inconsistent");
                }
                else if (s.TreeParent is int parent && parent >= 1 && parent < id)
                {
                    SongRecord ps = data.Song(parent);
                    if (s.TreeRoot != ps.TreeRoot || s.TreeDepth != ps.TreeDepth + 1)
                        p.Add($"node {id}: tree_root_node/tree_depth disagree with its parent");
                }
                if (s.Descendants != descendants[id]) p.Add($"node {id}: descendants {s.Descendants} != {descendants[id]}");
                if (s.InDegree != inDeg[id] || s.OutDegree != outDeg[id])
                    p.Add($"node {id}: in/out degree {s.InDegree}/{s.OutDegree} != edges {inDeg[id]}/{outDeg[id]}");
                if (!(s.ExcerptEndBeat > s.ExcerptStartBeat)) p.Add($"node {id}: empty excerpt");
                if (string.IsNullOrEmpty(s.MidiPath)) p.Add($"node {id}: midi_path is empty");
            }

            CheckMetaCount(data, "song_count", n);
            CheckMetaCount(data, "edge_count", data.Edges.Count);
            CheckMetaCount(data, "root_count", data.RootCount);
        }

        static void CheckMetaCount(SongGraphData data, string key, int actual)
        {
            int? declared = data.MetaInt(key);
            if (declared.HasValue && declared.Value != actual)
                data.Problems.Add($"graph_meta {key}={declared.Value} but the tables hold {actual}");
        }

        /// <summary>A path stored relative to the DB folder with '/' separators, made absolute.</summary>
        public static string? ResolveRelative(string dbFolder, string? stored)
        {
            if (string.IsNullOrWhiteSpace(stored)) return null;
            string normalized = stored!.Replace('/', Path.DirectorySeparatorChar);
            return Path.IsPathRooted(normalized)
                ? Path.GetFullPath(normalized)
                : Path.GetFullPath(Path.Combine(dbFolder, normalized));
        }

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

        /// <summary>Reads one numeric field from a flat JSON object without a JSON dependency.</summary>
        static double? JsonNumber(string? json, string field)
        {
            if (string.IsNullOrEmpty(json)) return null;
            string key = "\"" + field + "\"";
            int at = json!.IndexOf(key, StringComparison.Ordinal);
            if (at < 0) return null;
            int colon = json.IndexOf(':', at + key.Length);
            if (colon < 0) return null;
            int start = colon + 1;
            while (start < json.Length && char.IsWhiteSpace(json[start])) start++;
            int end = start;
            while (end < json.Length && "+-.eE0123456789".IndexOf(json[end]) >= 0) end++;
            return double.TryParse(json.Substring(start, end - start), NumberStyles.Float, CultureInfo.InvariantCulture,
                out double v) ? v : null;
        }
    }
}
