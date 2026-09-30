using System.Globalization;
using System.Text;
using System.Text.Json;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Influence;

/// <summary>A graph edge as exported (one row of influence_edges).</summary>
internal sealed record GraphEdge(int Id, int Source, int Target, string Kind, string ChannelsCsv, string Primary, double S, double Z,
    double? Q, double Similarity, double Weight, Segment? Seg, string Evidence);

internal sealed class GraphSummary
{
    public int Nodes, Edges, TreeEdges, SecondaryEdges, Roots;
    public double MinTime, MaxTime;
    public List<GraphEdge> EdgeRows = [];
    public int[] InDegree = [], OutDegree = [];
    public (double Start, double End)[] Excerpts = [];
    public string ContentSha256 = "";
}

/// <summary>
/// Writes the graph database of DESIGN.md §10 from the pipeline tables (song/work + pair_score,
/// influence_edge, tree_node). The file is built beside the target and moved into place, in
/// journal_mode=DELETE, with nothing an SQLite 3.15 reader (Unity's sqlite3.dll) cannot open.
/// </summary>
internal static class GraphExport
{
    public static GraphSummary Export(SqliteConnection pipeline, string graphPath, string repoRoot, Params p, string generatedAt, TextWriter log)
    {
        var stats = new LoadStats();
        var songs = PipelineDb.LoadSongs(pipeline, stats);
        if (songs.Length == 0) throw new InvalidOperationException("no selected, analyzed songs with a year in the pipeline database");
        var byId = songs.ToDictionary(s => s.WorkId, s => s.Index, StringComparer.Ordinal);
        var nodes = PipelineDb.LoadTree(pipeline);
        var missing = songs.Where(s => !nodes.ContainsKey(s.WorkId)).Select(s => s.WorkId).Take(5).ToList();
        if (missing.Count > 0)
            throw new InvalidOperationException($"tree_node has no row for {string.Join(", ", missing)}: run the influence stage ('run') first");

        // Significant influence pairs (for edges, credit spans and root excerpts).
        var pairs = new Dictionary<(int, int), PairResult>();
        foreach (var sp in PipelineDb.LoadPairs(pipeline, significantOnly: true))
        {
            if (sp.Relation != "influence" || !byId.TryGetValue(sp.A, out int a) || !byId.TryGetValue(sp.B, out int b)) continue;
            pairs[(a, b)] = ToResult(sp, songs[a], songs[b], a, b, p);
        }
        int n = songs.Length;
        var tree = new TreeResult
        {
            Parent = new int[n], Root = new int[n], Depth = new int[n], RefCount = new int[n], Descendants = new int[n],
            RefNorm = new double?[n], Katz = new double[n], Edges = [], CreditedSpans = new List<CreditedSpan>[n],
        };
        for (int i = 0; i < n; i++)
        {
            var node = nodes[songs[i].WorkId];
            tree.Parent[i] = node.Parent != null && byId.TryGetValue(node.Parent, out int pi) ? pi : -1;
            tree.Root[i] = byId.TryGetValue(node.Root, out int ri) ? ri : i;
            tree.Depth[i] = node.Depth;
            tree.RefCount[i] = node.RefCount;
            tree.RefNorm[i] = node.RefNorm;
            tree.Katz[i] = node.Katz ?? 0;
            tree.Descendants[i] = node.Descendants;
            tree.CreditedSpans[i] = [];
        }
        foreach (var e in PipelineDb.LoadEdges(pipeline))
        {
            if (!byId.TryGetValue(e.A, out int a) || !byId.TryGetValue(e.B, out int b)) continue;
            if (!pairs.TryGetValue((a, b), out var pr))
                throw new InvalidOperationException($"influence_edge {e.A} -> {e.B} has no significant pair_score row");
            tree.Edges.Add((pr, e.Kind, e.Credited));
        }
        foreach (var group in pairs.Values.GroupBy(x => x.B).OrderBy(g => g.Key))
            foreach (var (a, spans) in Tree.Credit([.. group.OrderBy(x => x.A)], p))
                tree.CreditedSpans[a].AddRange(spans);

        CheckInvariants(songs, tree);
        var excerpts = Excerpts.Compute(songs, tree, p);
        var summary = new GraphSummary { Nodes = n, Excerpts = excerpts, InDegree = new int[n], OutDegree = new int[n] };

        // Edge rows: by target, the tree edge first, then secondary edges by S.
        var ordered = tree.Edges.OrderBy(e => e.Pair.B).ThenBy(e => e.Kind == "tree" ? 0 : 1)
            .ThenByDescending(e => e.Pair.S).ThenBy(e => e.Pair.A).ToList();
        int id = 0;
        foreach (var (pr, kind, _) in ordered)
        {
            double sim = 1 - Math.Pow(2, -pr.S / p.SimilarityHalfBits);
            var counting = Enumerable.Range(0, Channels.Count).Where(c => pr.Counts((Channel)c)).ToList();
            int primary = counting.Count > 0
                ? counting.OrderByDescending(c => pr.W[c] * pr.Z[c]).ThenBy(c => c).First()
                : Enumerable.Range(0, Channels.Count).OrderByDescending(c => pr.E[c]).ThenBy(c => c).First();
            string csv = counting.Count > 0 ? string.Join(",", counting.Select(c => Channels.Names[c])) : Channels.Names[primary];
            double weight = kind == "tree" ? 0.5 + 0.5 * sim : sim;
            summary.EdgeRows.Add(new GraphEdge(++id, pr.A + 1, pr.B + 1, kind, csv, Channels.Names[primary], pr.S, pr.Zc,
                double.IsNaN(pr.Q) ? null : pr.Q, sim, weight, Excerpts.Strongest(pr), EvidenceText(pr)));
            summary.InDegree[pr.B]++;
            summary.OutDegree[pr.A]++;
            if (kind == "tree") summary.TreeEdges++; else summary.SecondaryEdges++;
        }
        summary.Edges = summary.EdgeRows.Count;
        summary.Roots = tree.Parent.Count(x => x < 0);
        summary.MinTime = songs.Min(s => s.TimeValue);
        summary.MaxTime = songs.Max(s => s.TimeValue);

        string normalization = PipelineDb.Meta(pipeline, "normalization") ?? Environment.GetEnvironmentVariable("MUSICHISTORY_NORMALIZATION") ?? "relative";
        string targetBpm = Environment.GetEnvironmentVariable("MUSICHISTORY_TARGET_BPM") ?? "120";
        string? resonance = songs.Where(s => !string.IsNullOrEmpty(s.ResonanceCommit)).GroupBy(s => s.ResonanceCommit!)
            .OrderByDescending(g => g.Count()).ThenBy(g => g.Key, StringComparer.Ordinal).Select(g => g.Key).FirstOrDefault();
        var meta = new List<(string, string?)>
        {
            ("schema_version", Schema.GraphSchemaVersion.ToString(CultureInfo.InvariantCulture)),
            ("generated_at", generatedAt),
            ("normalization", normalization),
            ("target_key", normalization == "parallel" ? "C major / C minor" : "C major / A minor"),
            ("target_bpm", double.Parse(targetBpm, CultureInfo.InvariantCulture).ToString("R", CultureInfo.InvariantCulture)),
            ("min_time", summary.MinTime.ToString("R", CultureInfo.InvariantCulture)),
            ("max_time", summary.MaxTime.ToString("R", CultureInfo.InvariantCulture)),
            ("song_count", n.ToString(CultureInfo.InvariantCulture)),
            ("edge_count", summary.Edges.ToString(CultureInfo.InvariantCulture)),
            ("root_count", summary.Roots.ToString(CultureInfo.InvariantCulture)),
            ("resonance_commit", resonance),
            ("pipeline_commit", RepoRoot.GitHead(repoRoot)),
            ("midi_base", "relative to this file's folder"),
        };
        // Songs outside the ranked list that were added so known influence pairs can be checked
        // (work.selected = 2); the viewer marks them.
        var exported = new HashSet<string>(songs.Select(s => s.WorkId), StringComparer.Ordinal);
        var extras = new List<string>();
        using (var cmd = pipeline.CreateCommand())
        {
            cmd.CommandText = "SELECT work_id FROM work WHERE selected = 2 ORDER BY work_id";
            using var r = cmd.ExecuteReader();
            while (r.Read())
                if (exported.Contains(r.GetString(0))) extras.Add(r.GetString(0));
        }
        meta.Add(("validation_extras", string.Join(",", extras)));

        string graphDir = Path.GetDirectoryName(Path.GetFullPath(graphPath))!;
        Directory.CreateDirectory(graphDir);
        string tmp = Path.GetFullPath(graphPath) + ".tmp";
        foreach (var f in new[] { tmp, tmp + "-journal" })
            if (File.Exists(f)) File.Delete(f);
        using (var g = new SqliteConnection(new SqliteConnectionStringBuilder { DataSource = tmp, Mode = SqliteOpenMode.ReadWriteCreate, Pooling = false }.ToString()))
        {
            g.Open();
            PipelineDb.Exec(g, "PRAGMA journal_mode=DELETE");
            PipelineDb.Exec(g, "PRAGMA page_size=4096");
            PipelineDb.Exec(g, Schema.Graph);
            using var tx = g.BeginTransaction();
            Insert(g, tx, "INSERT INTO graph_meta(key, value) VALUES ($1, $2)",
                meta.Select(m => new object?[] { m.Item1, m.Item2 }));
            Insert(g, tx, "INSERT INTO nodes(id, position_x, position_y, position_z) VALUES ($1, NULL, NULL, NULL)",
                songs.Select(s => new object?[] { s.Index + 1 }));
            Insert(g, tx, """
                INSERT INTO song_node(node_id, work_id, title, artist, year, release_date, date_precision, time_value, canon_rank,
                  tonic_pc, mode, key_name, norm_shift, native_bpm, beats_per_bar, first_downbeat, midi_path, normalized_midi_path,
                  midi_source, excerpt_start_beat, excerpt_end_beat, tree_parent_node, tree_root_node, tree_depth, ref_count,
                  ref_norm, katz, descendants, in_degree, out_degree, key_confidence, melody_confidence, main_loop, summary)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22,
                  $23, $24, $25, $26, $27, $28, $29, $30, $31, $32, $33, $34)
                """,
                songs.Select(s => new object?[]
                {
                    s.Index + 1, s.WorkId, s.Title, s.Artist, s.Year, s.ReleaseDate, s.DatePrecision, s.TimeValue, s.CanonRank,
                    ((s.TonicPc % 12) + 12) % 12, s.Mode, Keys.Name(s.TonicPc, s.Mode), s.NormShift, s.NativeBpm, s.BeatsPerBar,
                    s.FirstDownbeat, RepoRoot.Relative(s.MidiPath, repoRoot, graphDir),
                    s.NormalizedMidiPath is { Length: > 0 } nm ? RepoRoot.Relative(nm, repoRoot, graphDir) : null,
                    s.MidiSource, excerpts[s.Index].Start, excerpts[s.Index].End,
                    tree.Parent[s.Index] >= 0 ? tree.Parent[s.Index] + 1 : null, tree.Root[s.Index] + 1, tree.Depth[s.Index],
                    tree.RefCount[s.Index], tree.RefNorm[s.Index], tree.Katz[s.Index], tree.Descendants[s.Index],
                    summary.InDegree[s.Index], summary.OutDegree[s.Index], s.KeyConfidence, s.MelodyConfidence, s.MainLoop,
                    SummaryText(s),
                }));
            Insert(g, tx, """
                INSERT INTO influence_edges(id, source_node, target_node, kind, channels, primary_channel, score_bits, z, q,
                  similarity, weight, src_start_beat, src_end_beat, dst_start_beat, dst_end_beat, evidence)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                """,
                summary.EdgeRows.Select(e => new object?[]
                {
                    e.Id, e.Source, e.Target, e.Kind, e.ChannelsCsv, e.Primary, e.S, e.Z, e.Q, e.Similarity, e.Weight,
                    e.Seg?.AStart, e.Seg?.AEnd, e.Seg?.BStart, e.Seg?.BEnd, e.Evidence,
                }));
            tx.Commit();
            summary.ContentSha256 = ContentHash(g);
        }
        SqliteConnection.ClearAllPools();
        File.Move(tmp, graphPath, overwrite: true);
        log.WriteLine($"graph: {n} nodes, {summary.Edges} edges ({summary.TreeEdges} tree, {summary.SecondaryEdges} secondary), " +
                      $"{summary.Roots} roots -> {graphPath}");
        return summary;
    }

    internal static PairResult ToResult(PipelineDb.StoredPair sp, Song a, Song b, int ai, int bi, Params p)
    {
        var r = new PairResult
        {
            A = ai, B = bi, Zc = sp.Zc ?? 0, Q = sp.Q ?? double.NaN, S = sp.S ?? 0, Pmi = sp.Pmi ?? double.NaN,
            ChordId = sp.ChordId ?? double.NaN, Significant = sp.Significant, Relation = sp.Relation, Tested = true,
        };
        double hMin = Math.Min(a.IntervalEntropy, b.IntervalEntropy);
        double[] w = [hMin < p.EntropyHalf ? p.WMelody / 2 : p.WMelody, p.WBass, p.WChord, p.WLoop];
        for (int c = 0; c < Channels.Count; c++)
        {
            r.Avail[c] = sp.E[c] != null;
            r.E[c] = sp.E[c] ?? 0;
            r.Z[c] = sp.Z[c] ?? double.NaN;
            r.W[c] = w[c];
        }
        r.Segments.AddRange(sp.Segments);
        return r;
    }

    /// <summary>DESIGN.md §10 invariants: one tree edge into every non-root, from its parent; sources earlier.</summary>
    private static void CheckInvariants(Song[] songs, TreeResult tree)
    {
        var treeIn = new int[songs.Length];
        foreach (var (pr, kind, _) in tree.Edges)
        {
            if (pr.A >= pr.B || !DateOrder.Earlier(songs[pr.A].Date, songs[pr.B].Date) || songs[pr.A].TimeValue >= songs[pr.B].TimeValue)
                throw new InvalidOperationException($"edge {songs[pr.A].WorkId} -> {songs[pr.B].WorkId} is not earlier -> later");
            if (kind == "tree")
            {
                treeIn[pr.B]++;
                if (tree.Parent[pr.B] != pr.A)
                    throw new InvalidOperationException($"tree edge into {songs[pr.B].WorkId} does not come from its tree parent");
            }
        }
        for (int i = 0; i < songs.Length; i++)
            if (treeIn[i] != (tree.Parent[i] >= 0 ? 1 : 0))
                throw new InvalidOperationException($"{songs[i].WorkId}: {treeIn[i]} tree edges in, parent {tree.Parent[i]}");
    }

    private static void Insert(SqliteConnection g, SqliteTransaction tx, string sql, IEnumerable<object?[]> rows)
    {
        using var cmd = g.CreateCommand();
        cmd.Transaction = tx;
        cmd.CommandText = sql;
        SqliteParameter[]? ps = null;
        foreach (var row in rows)
        {
            if (ps == null)
            {
                ps = new SqliteParameter[row.Length];
                for (int i = 0; i < row.Length; i++) ps[i] = cmd.Parameters.Add("$" + (i + 1).ToString(CultureInfo.InvariantCulture), SqliteType.Text);
            }
            for (int i = 0; i < row.Length; i++)
            {
                object? v = row[i];
                ps[i].SqliteType = v switch { int or long => SqliteType.Integer, double => SqliteType.Real, _ => SqliteType.Text };
                ps[i].Value = v is double d && (double.IsNaN(d) || double.IsInfinity(d)) ? DBNull.Value : v ?? DBNull.Value;
            }
            cmd.ExecuteNonQuery();
        }
    }

    /// <summary>"melody 24 notes, 31 bits; loop vi-IV-I-V (same phase), 9 bits" -- channel facts only, never text.</summary>
    public static string EvidenceText(PairResult pr)
    {
        var parts = new List<(double Bits, string Text)>();
        for (int c = 0; c < Channels.Count; c++)
        {
            var best = pr.Segments.Where(s => (int)s.Channel == c).OrderByDescending(s => s.Bits).FirstOrDefault();
            if (best == null) continue;
            double bits = pr.Segments.Where(s => (int)s.Channel == c).Sum(s => s.Bits);
            string what = (Channel)c switch
            {
                Channel.Melody => $"melody {best.N} notes",
                Channel.Bass => $"bass riff {best.N} notes",
                Channel.Chord => $"chords {best.N} changes",
                _ => $"loop {best.Loop ?? "?"} ({(best.SamePhase ? "same" : "cross")} phase)",
            };
            parts.Add((bits, $"{what}, {bits.ToString("0", CultureInfo.InvariantCulture)} bits"));
        }
        return string.Join("; ", parts.OrderByDescending(x => x.Bits).Select(x => x.Text));
    }

    /// <summary>Short display facts from summary_json (form, chord count, top chords, modulations). Never lyrics.</summary>
    public static string? SummaryText(Song s)
    {
        var parts = new List<string>();
        if (!string.IsNullOrEmpty(s.OriginalArtist) && !string.Equals(s.OriginalArtist, s.Artist, StringComparison.OrdinalIgnoreCase))
            parts.Add($"orig. {s.OriginalArtist}");
        if (!string.IsNullOrEmpty(s.SummaryJson))
        {
            try
            {
                using var doc = JsonDocument.Parse(s.SummaryJson);
                var root = doc.RootElement;
                if (root.TryGetProperty("form", out var form) && form.ValueKind == JsonValueKind.String && form.GetString() is { Length: > 0 } f)
                    parts.Add($"form {Ascii(f, 60)}");
                if (root.TryGetProperty("chord_changes", out var cc) && cc.ValueKind == JsonValueKind.Number)
                    parts.Add($"{cc.GetInt32()} chord changes");
                if (root.TryGetProperty("top_chords", out var tc) && tc.ValueKind == JsonValueKind.Array)
                {
                    var names = tc.EnumerateArray().Take(4).Where(x => x.ValueKind == JsonValueKind.Array && x.GetArrayLength() > 0)
                        .Select(x => x[0].GetString()).Where(x => !string.IsNullOrEmpty(x)).Select(x => Ascii(x!, 8)).ToList();
                    if (names.Count > 0) parts.Add("top " + string.Join(" ", names));
                }
                if (root.TryGetProperty("modulations", out var mods) && mods.ValueKind == JsonValueKind.Array && mods.GetArrayLength() > 0)
                {
                    var last = mods.EnumerateArray().Last();
                    if (last.ValueKind == JsonValueKind.Array && last.GetArrayLength() > 1 && last[1].ValueKind == JsonValueKind.String)
                        parts.Add($"modulates to {Ascii(last[1].GetString()!, 12)}");
                }
            }
            catch (JsonException)
            {
                // A malformed summary is just not shown.
            }
        }
        if (parts.Count == 0) return null;
        string text = string.Join("; ", parts);
        return text.Length > 200 ? text[..200] : text;
    }

    private static string Ascii(string s, int max)
    {
        var sb = new StringBuilder();
        foreach (char ch in s)
            if (ch >= 32 && ch < 127) sb.Append(ch);
        string t = sb.ToString();
        return t.Length > max ? t[..max] : t;
    }

    /// <summary>SHA-256 over a canonical dump of every table except graph_meta.generated_at (determinism checks).</summary>
    public static string ContentHash(SqliteConnection g)
    {
        using var sha = System.Security.Cryptography.IncrementalHash.CreateHash(System.Security.Cryptography.HashAlgorithmName.SHA256);
        foreach (string sql in new[]
                 {
                     "SELECT key, value FROM graph_meta WHERE key <> 'generated_at' ORDER BY key",
                     "SELECT * FROM nodes ORDER BY id", "SELECT * FROM song_node ORDER BY node_id", "SELECT * FROM influence_edges ORDER BY id",
                 })
        {
            using var cmd = g.CreateCommand();
            cmd.CommandText = sql;
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                var sb = new StringBuilder();
                for (int i = 0; i < r.FieldCount; i++)
                {
                    object v = r.GetValue(i);
                    sb.Append(v switch
                    {
                        DBNull => "␀",
                        double d => d.ToString("R", CultureInfo.InvariantCulture),
                        long l => l.ToString(CultureInfo.InvariantCulture),
                        _ => Convert.ToString(v, CultureInfo.InvariantCulture),
                    }).Append('\u001f');
                }
                sha.AppendData(Encoding.UTF8.GetBytes(sb.Append('\n').ToString()));
            }
        }
        return Convert.ToHexStringLower(sha.GetHashAndReset());
    }
}

/// <summary>Repository root, stored-path resolution and the pipeline commit.</summary>
internal static class RepoRoot
{
    /// <summary>
    /// Stored paths are relative to the repository root ("data/songs/...") or absolute. The root is
    /// --root, else MUSICHISTORY_ROOT, else the nearest ancestor of the pipeline DB holding
    /// musichistory/config.py, else the parent of the DB's folder (the default layout <root>/data/).
    /// </summary>
    public static string Find(string dbPath, string? explicitRoot)
    {
        if (!string.IsNullOrEmpty(explicitRoot)) return Path.GetFullPath(explicitRoot);
        string? env = Environment.GetEnvironmentVariable("MUSICHISTORY_ROOT");
        if (!string.IsNullOrEmpty(env)) return Path.GetFullPath(env);
        var dir = new DirectoryInfo(Path.GetDirectoryName(Path.GetFullPath(dbPath))!);
        for (var d = dir; d != null; d = d.Parent)
            if (File.Exists(Path.Combine(d.FullName, "musichistory", "config.py"))) return d.FullName;
        return dir.Parent?.FullName ?? dir.FullName;
    }

    /// <summary>Path of a stored MIDI path relative to <paramref name="baseDir"/>, with '/' separators.</summary>
    public static string Relative(string stored, string repoRoot, string baseDir)
    {
        string abs = Path.IsPathRooted(stored) ? Path.GetFullPath(stored) : Path.GetFullPath(Path.Combine(repoRoot, stored));
        return Path.GetRelativePath(baseDir, abs).Replace('\\', '/');
    }

    /// <summary>Commit of HEAD read from .git (no git process; null outside a repository).</summary>
    public static string? GitHead(string root)
    {
        try
        {
            string git = Path.Combine(root, ".git");
            string headFile = Path.Combine(git, "HEAD");
            if (!File.Exists(headFile)) return null;
            string head = File.ReadAllText(headFile).Trim();
            if (!head.StartsWith("ref: ", StringComparison.Ordinal)) return head;
            string refName = head[5..].Trim();
            string refFile = Path.Combine(git, refName.Replace('/', Path.DirectorySeparatorChar));
            if (File.Exists(refFile)) return File.ReadAllText(refFile).Trim();
            string packed = Path.Combine(git, "packed-refs");
            if (File.Exists(packed))
                foreach (string line in File.ReadLines(packed))
                    if (line.EndsWith(" " + refName, StringComparison.Ordinal)) return line[..line.IndexOf(' ')];
            return null;
        }
        catch (IOException)
        {
            return null;
        }
    }
}
