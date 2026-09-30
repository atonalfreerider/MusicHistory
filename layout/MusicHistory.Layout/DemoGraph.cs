using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Layout;

/// <summary>
/// Writes a synthetic graph database in the exact DESIGN.md §10 schema, so the viewer and the
/// layout can be built and tested before the real pipeline has produced a graph. Everything is a
/// deterministic function of (N, seed): songs "Song 0001".."Song N" by "Artist 001".., original
/// dates 1940–2025 (year, month or day precision, time_value as in §8.1), keys and the relative
/// normalization shift, BPM, meter, bar-aligned excerpts (about one in eight entering or leaving
/// outside the home key: entry_* / exit_* set), placeholder MIDI paths
/// (<c>../songs/&lt;work_id&gt;/score.mid</c>, which do not exist), and an influence forest grown by
/// recency-weighted preferential attachment among clearly earlier songs, with 0–3 secondary
/// edges per song (seed 42, 1000 songs: 41 roots, largest subtree 481, depth 10). graph_meta
/// carries <c>synthetic = 1</c>. No text of any real song is involved.
/// </summary>
internal static class DemoGraph
{
    public sealed record Summary(int Nodes, int Edges, int TreeEdges, int SecondaryEdges, int Roots, int MaxDescendants,
        int MaxDepth, double MinTime, double MaxTime);

    private sealed class Song
    {
        public int Gen;
        public string WorkId = "";
        public int Year, Precision, Month, Day;
        public double Time;
        public string ReleaseDate = "";
        public int Artist;
        public int Tonic;
        public bool Minor;
        public double Bpm, BeatsPerBar, FirstDownbeat;
        public int Bars;
        public double ExcerptStart, ExcerptEnd;
        public int? EntryTonic, ExitTonic;       // key heard at the excerpt's start / end when not the home key
        public string? EntryMode, ExitMode;
        public int Parent = -1;
        public int CanonRank;
        public double KeyConfidence, MelodyConfidence;
        public string? MainLoop;
        public string Summary = "";
    }

    private sealed record Edge(int Source, int Target, bool Tree, string Channels, string Primary, double Bits, double Z, double Q,
        double Similarity, double Weight, double SrcStart, double SrcEnd, double DstStart, double DstEnd, string Evidence);

    private static readonly string[] ChannelNames = ["melody", "bass", "chord", "loop"];
    private static readonly string[] MajorLoops = ["I-V-vi-IV", "I-vi-IV-V", "I-IV-V-IV", "vi-IV-I-V", "I-IV-I-V", "I-bVII-IV-I", "ii-V-I-vi"];
    private static readonly string[] MinorLoops = ["i-VI-III-VII", "i-iv-v-i", "i-VII-VI-VII", "i-VI-VII-i", "i-iv-VII-III"];
    private static readonly string[] Forms = ["In (V C)x2 Br C Out", "(V V C)x2 Solo C", "In V PC C V PC C Br C Out", "A A B A", "12-bar blues x6", "In (V C)x3 Out"];
    private static readonly double[] MajorTonicWeights = [12, 3, 10, 5, 7, 8, 2, 11, 4, 9, 6, 3];   // C..B
    private static readonly double[] MinorTonicWeights = [6, 4, 8, 2, 9, 4, 5, 6, 2, 10, 3, 6];

    public static Summary Write(string path, int n, int seed, string generatedAt, double rootFraction = 0.04)
    {
        if (n < 1) throw new UsageException("--nodes must be >= 1");
        if (!(rootFraction >= 0 && rootFraction <= 1)) throw new UsageException("--roots must be a fraction in [0, 1]");
        var rng = new Random(seed);
        var songs = new List<Song>(n);
        int artistCount = Math.Max(8, n / 6);
        var careerStart = new int[artistCount];
        var careerLen = new int[artistCount];
        for (int a = 0; a < artistCount; a++)
        {
            careerStart[a] = 1932 + rng.Next(0, 90);
            careerLen[a] = 4 + rng.Next(0, 22);
        }

        for (int k = 0; k < n; k++)
        {
            var s = new Song { Gen = k };
            s.Year = SampleYear(rng);
            double pr = rng.NextDouble();
            s.Precision = pr < 0.3 ? 9 : pr < 0.55 ? 10 : 11;
            s.Month = 1 + rng.Next(12);
            s.Day = 1 + rng.Next(DateTime.DaysInMonth(s.Year, s.Month));
            switch (s.Precision)
            {
                case 9:
                    s.Time = s.Year + 0.5;
                    s.ReleaseDate = s.Year.ToString("D4", CultureInfo.InvariantCulture);
                    break;
                case 10:
                    s.Time = s.Year + (s.Month - 0.5) / 12.0;
                    s.ReleaseDate = string.Create(CultureInfo.InvariantCulture, $"{s.Year:D4}-{s.Month:D2}");
                    break;
                default:
                    int doy = new DateTime(s.Year, s.Month, s.Day).DayOfYear;
                    s.Time = s.Year + (doy - 0.5) / 365.25;
                    s.ReleaseDate = string.Create(CultureInfo.InvariantCulture, $"{s.Year:D4}-{s.Month:D2}-{s.Day:D2}");
                    break;
            }
            s.WorkId = "R" + Sha1Hex($"demo|{seed}|{k}")[..12];
            s.Artist = PickArtist(rng, s.Year, careerStart, careerLen);
            s.Minor = rng.NextDouble() < 0.32;
            s.Tonic = Weighted(rng, s.Minor ? MinorTonicWeights : MajorTonicWeights);
            s.Bpm = Math.Round(SampleBpm(rng, s.Year), 1);
            double m = rng.NextDouble();
            s.BeatsPerBar = m < 0.86 ? 4 : m < 0.97 ? 3 : 6;
            double fd = rng.NextDouble();
            s.FirstDownbeat = fd < 0.65 ? 0 : fd < 0.75 ? 1 : fd < 0.80 ? 2 : fd < 0.90 ? s.BeatsPerBar : 2 * s.BeatsPerBar;
            s.Bars = 60 + rng.Next(0, 61);
            s.KeyConfidence = Math.Round(0.5 + 0.5 * rng.NextDouble(), 3);
            s.MelodyConfidence = Math.Round(0.3 + 0.7 * rng.NextDouble(), 3);
            s.MainLoop = rng.NextDouble() < 0.75 ? Pick(rng, s.Minor ? MinorLoops : MajorLoops) : null;
            s.Summary = $"synthetic demo song; form {Pick(rng, Forms)}; {20 + rng.Next(0, 80)} chord changes";
            songs.Add(s);
        }

        // Node ids follow (time_value, work_id), as §10 requires.
        songs.Sort((x, y) => x.Time != y.Time ? x.Time.CompareTo(y.Time) : string.CompareOrdinal(x.WorkId, y.WorkId));

        var ranks = Enumerable.Range(1, n).ToArray();
        rng.Shuffle(ranks);
        for (int i = 0; i < n; i++) songs[i].CanonRank = ranks[i];

        // Influence forest: parent = recency-weighted preferential attachment among clearly
        // earlier songs (different year, or both dated to the month and different): weight
        // (1 + 2·children)·exp(−Δt / 10 years). A song is a root with probability rootFraction
        // (default 4 %), or when nothing is clearly earlier.
        var children = new int[n];
        var edges = new List<Edge>();
        var weights = new double[n];
        for (int i = 0; i < n; i++)
        {
            var s = songs[i];
            double total = 0;
            for (int j = 0; j < i; j++)
            {
                weights[j] = Earlier(songs[j], s) ? (1 + 2.0 * children[j]) * Math.Exp(-(s.Time - songs[j].Time) / 10.0) : 0;
                total += weights[j];
            }
            if (total <= 0 || rng.NextDouble() < rootFraction)
            {
                s.Parent = -1;
                s.ExcerptStart = s.FirstDownbeat + rng.Next(0, Math.Max(1, s.Bars / 3)) * s.BeatsPerBar;
                s.ExcerptEnd = s.ExcerptStart + (8 + rng.Next(0, 17)) * s.BeatsPerBar;
                continue;
            }
            int parent = Sample(rng, weights, i, total);
            s.Parent = parent;
            children[parent]++;
            int exBars = 8 + rng.Next(0, 17);
            s.ExcerptStart = s.FirstDownbeat + rng.Next(0, Math.Max(1, s.Bars - exBars)) * s.BeatsPerBar;
            s.ExcerptEnd = s.ExcerptStart + exBars * s.BeatsPerBar;
            edges.Add(MakeEdge(rng, songs, parent, i, tree: true, s.ExcerptStart, s.ExcerptEnd));

            double c = rng.NextDouble();
            int secondaries = c < 0.30 ? 0 : c < 0.60 ? 1 : c < 0.85 ? 2 : 3;
            var used = new HashSet<int> { parent };
            for (int tries = 0; used.Count - 1 < secondaries && tries < 12; tries++)
            {
                int q = Sample(rng, weights, i, total);
                if (!used.Add(q)) continue;
                double segBars = 4 + rng.Next(0, 13);
                double dStart = s.FirstDownbeat + rng.Next(0, Math.Max(1, s.Bars - (int)segBars)) * s.BeatsPerBar;
                edges.Add(MakeEdge(rng, songs, q, i, tree: false, dStart, dStart + segBars * s.BeatsPerBar));
            }
        }

        // Edge ids: by target, the tree edge first, then secondary edges by score (as influence exports them).
        edges = [.. edges.OrderBy(e => e.Target).ThenBy(e => e.Tree ? 0 : 1).ThenByDescending(e => e.Bits).ThenBy(e => e.Source)];

        var desc = new int[n];
        var depth = new int[n];
        var root = new int[n];
        var inDeg = new int[n];
        var outDeg = new int[n];
        var katz = new double[n];
        for (int i = 0; i < n; i++)
        {
            int p = songs[i].Parent;
            depth[i] = p < 0 ? 0 : depth[p] + 1;
            root[i] = p < 0 ? i : root[p];
        }
        for (int i = n - 1; i >= 0; i--)
            if (songs[i].Parent >= 0) desc[songs[i].Parent] += 1 + desc[i];
        foreach (var e in edges)
        {
            inDeg[e.Target]++;
            outDeg[e.Source]++;
        }
        var outEdges = edges.GroupBy(e => e.Source).ToDictionary(g => g.Key, g => g.Select(e => e.Target).ToArray());
        for (int i = n - 1; i >= 0; i--)
            if (outEdges.TryGetValue(i, out var ts))
                katz[i] = ts.Sum(t => 0.2 * (1 + katz[t]));
        var later = new int[n];   // songs strictly later than song i (for ref_norm)
        for (int i = n - 1, lastEqual = n - 1; i >= 0; i--)
        {
            if (i < n - 1 && songs[i].Time != songs[i + 1].Time) lastEqual = i;
            later[i] = n - 1 - lastEqual;
        }

        // Excerpts that start or end outside the home key (DESIGN.md §10 entry_* / exit_*; NULL = home key),
        // so the viewer's key handoff can be tried: about one song in eight. A separate RNG keeps the rest
        // of the graph exactly as before for the same (N, seed).
        var keyRng = new Random(unchecked(seed * 7919 + 104729));
        foreach (var s in songs)
        {
            double u = keyRng.NextDouble();
            if (u >= 0.12) continue;
            string mode = s.Minor ? "minor" : "major";
            if (u < 0.06)
            {
                // A final lift: the excerpt ends a semitone or a whole step up.
                s.ExitTonic = (s.Tonic + 1 + keyRng.Next(2)) % 12;
                s.ExitMode = mode;
            }
            else if (u < 0.09)
            {
                // The excerpt opens in the relative key and ends at home.
                s.EntryTonic = (s.Tonic + (s.Minor ? 3 : 9)) % 12;
                s.EntryMode = s.Minor ? "major" : "minor";
            }
            else
            {
                // The whole excerpt sits in a bridge in the dominant.
                s.EntryTonic = s.ExitTonic = (s.Tonic + 7) % 12;
                s.EntryMode = s.ExitMode = mode;
            }
        }

        int roots = songs.Count(s => s.Parent < 0);
        double minTime = songs[0].Time, maxTime = songs[^1].Time;
        string full = Path.GetFullPath(path);
        Directory.CreateDirectory(Path.GetDirectoryName(full)!);
        string tmp = full + ".tmp";
        foreach (string f in new[] { tmp, tmp + "-journal" })
            if (File.Exists(f)) File.Delete(f);
        using (var c = new SqliteConnection(new SqliteConnectionStringBuilder
               { DataSource = tmp, Mode = SqliteOpenMode.ReadWriteCreate, Pooling = false }.ToString()))
        {
            c.Open();
            Exec(c, "PRAGMA journal_mode=DELETE");
            Exec(c, "PRAGMA page_size=4096");
            Exec(c, GraphSchema.Graph);
            using var tx = c.BeginTransaction();
            string R(double x) => x.ToString("R", CultureInfo.InvariantCulture);
            string I(int x) => x.ToString(CultureInfo.InvariantCulture);
            Insert(c, tx, "INSERT INTO graph_meta(key, value) VALUES ($1, $2)", new (string, string?)[]
            {
                ("schema_version", I(GraphSchema.SchemaVersion)), ("generated_at", generatedAt), ("normalization", "relative"),
                ("target_key", "C major / A minor"), ("target_bpm", "120"), ("min_time", R(minTime)), ("max_time", R(maxTime)),
                ("song_count", I(n)), ("edge_count", I(edges.Count)), ("root_count", I(roots)), ("resonance_commit", null),
                ("pipeline_commit", null), ("midi_base", "relative to this file's folder"), ("synthetic", "1"),
                ("demo_seed", I(seed)), ("demo_generator", "MusicHistory.Layout demo"),
            }.Select(kv => new object?[] { kv.Item1, kv.Item2 }));
            Insert(c, tx, "INSERT INTO nodes(id, position_x, position_y, position_z) VALUES ($1, NULL, NULL, NULL)",
                Enumerable.Range(1, n).Select(id => new object?[] { id }));
            Insert(c, tx, """
                INSERT INTO song_node(node_id, work_id, title, artist, year, release_date, date_precision, time_value, canon_rank,
                  tonic_pc, mode, key_name, norm_shift, native_bpm, beats_per_bar, first_downbeat, midi_path, normalized_midi_path,
                  midi_source, excerpt_start_beat, excerpt_end_beat, entry_tonic_pc, entry_mode, exit_tonic_pc, exit_mode,
                  tree_parent_node, tree_root_node, tree_depth, ref_count,
                  ref_norm, katz, descendants, in_degree, out_degree, key_confidence, melody_confidence, main_loop, summary)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22,
                  $23, $24, $25, $26, $27, $28, $29, $30, $31, $32, $33, $34, $35, $36, $37, $38)
                """,
                Enumerable.Range(0, n).Select(i =>
                {
                    var s = songs[i];
                    string mode = s.Minor ? "minor" : "major";
                    return new object?[]
                    {
                        i + 1, s.WorkId, string.Create(CultureInfo.InvariantCulture, $"Song {i + 1:D4}"),
                        string.Create(CultureInfo.InvariantCulture, $"Artist {s.Artist + 1:D3}"), s.Year, s.ReleaseDate, s.Precision, s.Time,
                        s.CanonRank, s.Tonic, mode, KeyName(s.Tonic, mode), NormShift(s.Tonic, s.Minor), s.Bpm, s.BeatsPerBar,
                        s.FirstDownbeat, $"../songs/{s.WorkId}/score.mid", $"../normalized/{s.WorkId}.mid", "demo", s.ExcerptStart,
                        s.ExcerptEnd, s.EntryTonic, s.EntryMode, s.ExitTonic, s.ExitMode, s.Parent >= 0 ? s.Parent + 1 : null, root[i] + 1, depth[i], outDeg[i],
                        later[i] > 0 ? Math.Round(outDeg[i] / (double)later[i], 6) : null, Math.Round(katz[i], 6), desc[i], inDeg[i],
                        outDeg[i], s.KeyConfidence, s.MelodyConfidence, s.MainLoop, s.Summary,
                    };
                }));
            Insert(c, tx, """
                INSERT INTO influence_edges(id, source_node, target_node, kind, channels, primary_channel, score_bits, z, q,
                  similarity, weight, src_start_beat, src_end_beat, dst_start_beat, dst_end_beat, evidence)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                """,
                edges.Select((e, k) => new object?[]
                {
                    k + 1, e.Source + 1, e.Target + 1, e.Tree ? "tree" : "secondary", e.Channels, e.Primary, e.Bits, e.Z, e.Q,
                    e.Similarity, e.Weight, e.SrcStart, e.SrcEnd, e.DstStart, e.DstEnd, e.Evidence,
                }));
            tx.Commit();
        }
        SqliteConnection.ClearAllPools();
        File.Move(tmp, full, overwrite: true);
        return new Summary(n, edges.Count, edges.Count(e => e.Tree), edges.Count(e => !e.Tree), roots, desc.Max(), depth.Max(),
            minTime, maxTime);
    }

    /// <summary>§8.1 order: different years, or both dated to the month or better and different.</summary>
    private static bool Earlier(Song a, Song b) =>
        a.Year < b.Year || (a.Year == b.Year && a.Precision >= 10 && b.Precision >= 10 && a.Time < b.Time);

    private static Edge MakeEdge(Random rng, List<Song> songs, int src, int dst, bool tree, double dStart, double dEnd)
    {
        var a = songs[src];
        double bits = Math.Round(tree ? 24 + Exp(rng, 20) : 12 + Exp(rng, 10), 2);
        double sim = 1 - Math.Pow(2, -bits / 32.0);
        double weight = tree ? 0.5 + 0.5 * sim : sim;
        int primary = Weighted(rng, [0.35, 0.15, 0.35, 0.15]);
        var chans = new SortedSet<int> { primary };
        int extra = rng.Next(0, 3);
        for (int k = 0; k < extra; k++) chans.Add(rng.Next(0, 4));
        var parts = new List<string>();
        foreach (int ch in chans.OrderBy(ch => ch == primary ? 0 : 1).ThenBy(ch => ch))
        {
            // The primary channel carries 60 % of the bits when others contribute, the rest split evenly.
            double b = chans.Count == 1 ? bits : ch == primary ? 0.6 * bits : 0.4 * bits / (chans.Count - 1);
            string what = ch switch
            {
                0 => $"melody {8 + rng.Next(0, 20)} notes",
                1 => $"bass riff {6 + rng.Next(0, 14)} notes",
                2 => $"chords {4 + rng.Next(0, 10)} changes",
                _ => $"loop {(a.MainLoop ?? (a.Minor ? MinorLoops[0] : MajorLoops[0]))} ({(rng.NextDouble() < 0.7 ? "same" : "cross")} phase)",
            };
            parts.Add(string.Create(CultureInfo.InvariantCulture, $"{what}, {b:0} bits"));
        }
        int segBars = 8 + rng.Next(0, 9);
        double sStart = a.FirstDownbeat + rng.Next(0, Math.Max(1, a.Bars - segBars)) * a.BeatsPerBar;
        return new Edge(src, dst, tree, string.Join(",", chans.Select(ch => ChannelNames[ch])), ChannelNames[primary], bits,
            Math.Round(3 + bits / 12 + rng.NextDouble(), 3), Math.Max(1e-12, Math.Round(Math.Pow(10, -2 - bits / 30), 12)),
            Math.Round(sim, 6), Math.Round(weight, 6), sStart, sStart + segBars * a.BeatsPerBar, dStart, dEnd, string.Join("; ", parts));
    }

    /// <summary>Years 1940–2025: sparse before the mid-1950s, as all-time lists are.</summary>
    private static int SampleYear(Random rng)
    {
        while (true)
        {
            int y = 1940 + rng.Next(0, 86);
            double w = y < 1955 ? 0.35 + 0.65 * (y - 1940) / 15.0 : 1.0;
            if (rng.NextDouble() < w) return y;
        }
    }

    private static double SampleBpm(Random rng, int year)
    {
        double mean = year switch { < 1950 => 105, < 1960 => 116, < 1970 => 118, < 1980 => 115, < 1990 => 118, < 2000 => 104, _ => 110 };
        if (rng.NextDouble() < 0.1) mean = 72;
        double g = Math.Sqrt(-2 * Math.Log(1 - rng.NextDouble())) * Math.Cos(2 * Math.PI * rng.NextDouble());
        return Math.Clamp(mean + 20 * g, 60, 190);
    }

    private static int PickArtist(Random rng, int year, int[] start, int[] len)
    {
        for (int tries = 0; tries < 40; tries++)
        {
            int a = rng.Next(start.Length);
            if (year >= start[a] && year <= start[a] + len[a]) return a;
        }
        return rng.Next(start.Length);
    }

    private static int Sample(Random rng, double[] w, int count, double total)
    {
        double x = rng.NextDouble() * total;
        for (int j = 0; j < count; j++)
        {
            x -= w[j];
            if (x < 0 && w[j] > 0) return j;
        }
        for (int j = count - 1; j >= 0; j--)
            if (w[j] > 0) return j;
        throw new InvalidOperationException("no candidate");
    }

    private static int Weighted(Random rng, double[] w)
    {
        double x = rng.NextDouble() * w.Sum();
        for (int i = 0; i < w.Length; i++)
        {
            x -= w[i];
            if (x < 0) return i;
        }
        return w.Length - 1;
    }

    private static T Pick<T>(Random rng, T[] items) => items[rng.Next(items.Length)];

    private static double Exp(Random rng, double mean) => -mean * Math.Log(1 - rng.NextDouble());

    private static string Sha1Hex(string s) => Convert.ToHexStringLower(SHA1.HashData(Encoding.UTF8.GetBytes(s)));

    private static readonly string[] Sharp = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
    private static readonly string[] Flat = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"];
    private static readonly int[] FlatMajor = [5, 10, 3, 8, 1, 6];
    private static readonly int[] FlatMinor = [2, 7, 0, 5, 10, 3];

    /// <summary>DESIGN.md §3 key names: flats for F Bb Eb Ab Db Gb major and D G C F Bb Eb minor.</summary>
    public static string KeyName(int tonic, string mode)
    {
        int t = ((tonic % 12) + 12) % 12;
        bool minor = mode == "minor";
        bool flat = Array.IndexOf(minor ? FlatMinor : FlatMajor, t) >= 0;
        return $"{(flat ? Flat : Sharp)[t]} {(minor ? "minor" : "major")}";
    }

    /// <summary>Relative normalization shift (§7): major to C, minor to A, in [−5, 6].</summary>
    public static int NormShift(int tonic, bool minor)
    {
        int target = minor ? 9 : 0;
        return ((target - tonic + 5) % 12 + 12) % 12 - 5;
    }

    private static void Exec(SqliteConnection c, string sql)
    {
        using var cmd = c.CreateCommand();
        cmd.CommandText = sql;
        cmd.ExecuteNonQuery();
    }

    private static void Insert(SqliteConnection c, SqliteTransaction tx, string sql, IEnumerable<object?[]> rows)
    {
        using var cmd = c.CreateCommand();
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
                ps[i].Value = v ?? DBNull.Value;
            }
            cmd.ExecuteNonQuery();
        }
    }
}
