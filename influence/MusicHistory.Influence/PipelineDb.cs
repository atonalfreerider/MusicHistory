using System.Globalization;
using System.Text;
using System.Text.Json;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Influence;

internal sealed record KnownInfluence(string Src, string Dst, string Kind, string? Note);

internal sealed class LoadStats
{
    public int Rows, Loaded, SkippedNoYear, MissingKey, NoMelody, NoBass, NoChords, NoLoops;
}

/// <summary>
/// Reads the analyze stage's identities from the pipeline DB (musichistory/db.py) and writes the
/// influence stage's own tables (pair_score, influence_edge, tree_node), replacing earlier rows.
/// </summary>
internal static class PipelineDb
{
    public static SqliteConnection Open(string path, bool create = false)
    {
        if (!create && !File.Exists(path)) throw new FileNotFoundException($"pipeline database not found: {path}");
        var cs = new SqliteConnectionStringBuilder
        {
            DataSource = path,
            Mode = create ? SqliteOpenMode.ReadWriteCreate : SqliteOpenMode.ReadWrite,
            Pooling = false,
            DefaultTimeout = 120,
        }.ToString();
        var c = new SqliteConnection(cs);
        c.Open();
        Exec(c, "PRAGMA busy_timeout=120000");
        return c;
    }

    public static void Exec(SqliteConnection c, string sql, SqliteTransaction? tx = null)
    {
        using var cmd = c.CreateCommand();
        cmd.Transaction = tx;
        cmd.CommandText = sql;
        cmd.ExecuteNonQuery();
    }

    public static void EnsureInfluenceTables(SqliteConnection c) => Exec(c, Schema.InfluenceTables);

    public static string? Meta(SqliteConnection c, string key)
    {
        try
        {
            using var cmd = c.CreateCommand();
            cmd.CommandText = "SELECT value FROM meta WHERE key = $k";
            cmd.Parameters.AddWithValue("$k", key);
            return cmd.ExecuteScalar() as string;
        }
        catch (SqliteException)
        {
            return null;
        }
    }

    private static string? Str(SqliteDataReader r, int i) => r.IsDBNull(i) ? null : r.GetString(i);
    private static int? Int(SqliteDataReader r, int i) => r.IsDBNull(i) ? null : r.GetInt32(i);
    private static double? Dbl(SqliteDataReader r, int i) => r.IsDBNull(i) ? null : r.GetDouble(i);

    /// <summary>Selected, analyzed songs (song.analysis_ok = 1 JOIN work.selected >= 1; 2 = validation extra) in node order (time_value, work_id).</summary>
    public static Song[] LoadSongs(SqliteConnection c, LoadStats stats)
    {
        var songs = new List<Song>();
        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText = """
                SELECT s.work_id, w.title, w.canonical_artist, w.original_artist, w.work_date, w.work_date_precision,
                       w.work_year, w.effective_year, w.first_chart_week, w.canon_rank,
                       s.midi_path, s.normalized_midi_path, s.duration_s, s.end_beat, s.tonic_pc, s.mode,
                       s.key_confidence, s.key_ambiguous_fifth, s.norm_shift, s.native_bpm, s.beats_per_bar,
                       s.first_downbeat, s.melody_confidence, s.interval_entropy, s.main_loop, s.summary_json,
                       s.resonance_commit, c.source
                FROM song s JOIN work w ON w.work_id = s.work_id
                LEFT JOIN candidate c ON c.candidate_id = s.candidate_id
                WHERE s.analysis_ok = 1 AND w.selected >= 1
                ORDER BY s.work_id
                """;
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                stats.Rows++;
                string? date = Str(r, 4);
                int? year = Int(r, 6);
                if (year == null && date != null && date.Length >= 4 && int.TryParse(date[..4], NumberStyles.Integer, CultureInfo.InvariantCulture, out int dy)) year = dy;
                year ??= Int(r, 7);
                if (year == null)
                {
                    stats.SkippedNoYear++;
                    continue;
                }
                if (r.IsDBNull(14) || r.IsDBNull(15)) stats.MissingKey++;
                var s = new Song
                {
                    WorkId = r.GetString(0),
                    Title = Str(r, 1) ?? "",
                    Artist = Str(r, 2) ?? "",
                    OriginalArtist = Str(r, 3),
                    ReleaseDate = date,
                    DatePrecision = Int(r, 5),
                    Year = year.Value,
                    FirstChartWeek = Str(r, 8),
                    CanonRank = Int(r, 9),
                    MidiPath = Str(r, 10) ?? "",
                    NormalizedMidiPath = Str(r, 11),
                    DurationS = Dbl(r, 12) ?? 0,
                    EndBeat = Dbl(r, 13) ?? 0,
                    TonicPc = Int(r, 14) ?? 0,
                    Mode = Str(r, 15) == "minor" ? "minor" : "major",
                    KeyConfidence = Dbl(r, 16),
                    Fifth = (Int(r, 17) ?? 0) != 0,
                    NormShift = Int(r, 18) ?? 0,
                    NativeBpm = Dbl(r, 19) is > 0 and var bpm ? bpm : 120.0,
                    BeatsPerBar = Dbl(r, 20) is > 0 and var bpb ? bpb : 4.0,
                    FirstDownbeat = Dbl(r, 21) ?? 0,
                    MelodyConfidence = Dbl(r, 22),
                    IntervalEntropy = Dbl(r, 23) ?? double.NaN,
                    MainLoop = Str(r, 24),
                    SummaryJson = Str(r, 25),
                    ResonanceCommit = Str(r, 26),
                    MidiSource = Str(r, 27),
                };
                s.Date = DateOrder.Make(s.Year, s.ReleaseDate, s.DatePrecision, s.FirstChartWeek);
                songs.Add(s);
            }
        }
        var byId = songs.ToDictionary(s => s.WorkId, StringComparer.Ordinal);

        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText = "SELECT work_id, tokens, starts, durs, downbeat FROM chord_seq WHERE kind = 'chg' AND level = 'L1'";
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                if (!byId.TryGetValue(r.GetString(0), out var s)) continue;
                var tokens = Ints(r.GetString(1));
                var starts = Doubles(r.GetString(2));
                var durs = Doubles(r.GetString(3));
                var down = r.IsDBNull(4) ? new int[tokens.Length] : Ints(r.GetString(4));
                int n = new[] { tokens.Length, starts.Length, durs.Length }.Min();
                var keep = Enumerable.Range(0, n).Where(i => tokens[i] is >= 0 and < 36).ToArray();
                s.Chords = new ChordLine
                {
                    Tokens = keep.Select(i => tokens[i]).ToArray(),
                    Starts = keep.Select(i => starts[i]).ToArray(),
                    Durs = keep.Select(i => durs[i]).ToArray(),
                    Downbeat = keep.Select(i => i < down.Length ? down[i] : 0).ToArray(),
                };
            }
        }
        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText = "SELECT work_id, role, onsets, durs, pitches, met FROM melody_line";
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                if (!byId.TryGetValue(r.GetString(0), out var s)) continue;
                var on = Doubles(r.GetString(2));
                var du = Doubles(r.GetString(3));
                var pi = Ints(r.GetString(4));
                var me = Ints(r.GetString(5));
                int n = new[] { on.Length, du.Length, pi.Length }.Min();
                var line = new NoteLine
                {
                    Onsets = on[..n], Durs = du[..n], Pitches = pi[..n],
                    Met = Enumerable.Range(0, n).Select(i => i < me.Length ? me[i] : 3).ToArray(),
                };
                if (r.GetString(1) == "melody") s.Melody = line;
                else if (r.GetString(1) == "bass") s.Bass = line;
            }
        }
        var loops = new Dictionary<string, List<LoopRow>>(StringComparer.Ordinal);
        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText = """
                SELECT work_id, family, cycle_id, phase, rhythm_sig, cycle_tokens, roman, loop_beats, passes, visits,
                       coverage_beats, visit_starts FROM loop ORDER BY work_id, family
                """;
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                if (!byId.ContainsKey(r.GetString(0))) continue;
                var cyc = DotInts(r.GetString(2));
                int phase = r.GetInt32(3);
                var rhy = DotInts(r.GetString(4));
                var own = r.IsDBNull(5) ? [] : Ints(r.GetString(5));
                int n = cyc.Length;
                if (n < 2) continue;
                if (own.Length != n) own = Enumerable.Range(0, n).Select(j => cyc[(j + phase) % n]).ToArray();
                var ownR = rhy.Length == n ? Enumerable.Range(0, n).Select(j => rhy[(j + phase) % n]).ToArray() : new int[n];
                var row = new LoopRow
                {
                    Family = r.GetInt32(1), Own = own, OwnRhythm = ownR, Roman = Str(r, 6),
                    LoopBeats = Dbl(r, 7) ?? 0, Passes = Int(r, 8) ?? 0, Visits = Int(r, 9) ?? 0,
                    Coverage = Dbl(r, 10) ?? 0, VisitStarts = r.IsDBNull(11) ? [] : Doubles(r.GetString(11)),
                };
                if (!loops.TryGetValue(r.GetString(0), out var list)) loops[r.GetString(0)] = list = [];
                list.Add(row);
            }
        }
        foreach (var s in songs)
        {
            if (loops.TryGetValue(s.WorkId, out var l)) s.Loops = [.. l];
            if (double.IsNaN(s.IntervalEntropy)) s.IntervalEntropy = s.Melody != null ? IntervalEntropy(s.Melody.Pitches) : 0;
            if (s.Melody == null) stats.NoMelody++;
            if (s.Bass == null) stats.NoBass++;
            if (s.Chords == null) stats.NoChords++;
            if (s.Loops.Length == 0) stats.NoLoops++;
        }
        songs.Sort((x, y) => x.TimeValue != y.TimeValue ? x.TimeValue.CompareTo(y.TimeValue) : string.CompareOrdinal(x.WorkId, y.WorkId));
        for (int i = 0; i < songs.Count; i++) songs[i].Index = i;
        stats.Loaded = songs.Count;
        return [.. songs];
    }

    /// <summary>Shannon entropy (bits) of intervals clipped to +-12, repeats included (identity/melody.py).</summary>
    public static double IntervalEntropy(int[] pitches)
    {
        if (pitches.Length < 2) return 0;
        var cnt = new Dictionary<int, int>();
        for (int i = 1; i < pitches.Length; i++)
        {
            int iv = Math.Clamp(pitches[i] - pitches[i - 1], -12, 12);
            cnt[iv] = cnt.GetValueOrDefault(iv) + 1;
        }
        double n = pitches.Length - 1, h = 0;
        foreach (int k in cnt.Keys.Order()) h -= cnt[k] / n * Math.Log2(cnt[k] / n);
        return Math.Round(h, 4);
    }

    public static List<KnownInfluence> LoadKnown(SqliteConnection c)
    {
        var list = new List<KnownInfluence>();
        try
        {
            using var cmd = c.CreateCommand();
            cmd.CommandText = "SELECT src_work_id, dst_work_id, kind, note FROM known_influence ORDER BY kind, src_work_id, dst_work_id";
            using var r = cmd.ExecuteReader();
            while (r.Read()) list.Add(new KnownInfluence(r.GetString(0), r.GetString(1), r.GetString(2), Str(r, 3)));
        }
        catch (SqliteException)
        {
            // Older databases without the canon tables: no validation rows.
        }
        return list;
    }

    // ------------------------------------------------------------------------------ writing
    public static void WriteResults(SqliteConnection c, Song[] songs, IReadOnlyList<PairResult>? pairs, TreeResult tree)
    {
        EnsureInfluenceTables(c);
        using var tx = c.BeginTransaction();
        if (pairs != null) Exec(c, "DELETE FROM pair_score", tx);
        Exec(c, "DELETE FROM influence_edge", tx);
        Exec(c, "DELETE FROM tree_node", tx);
        using (var cmd = c.CreateCommand())
        {
            cmd.Transaction = tx;
            cmd.CommandText = """
                INSERT INTO pair_score(a_id, b_id, e_melody, e_bass, e_chord, e_loop, z_melody, z_bass, z_chord, z_loop,
                  z_combined, q, s_bits, pmi, chord_identity, significant, relation, segments_json)
                VALUES ($a, $b, $e0, $e1, $e2, $e3, $z0, $z1, $z2, $z3, $zc, $q, $s, $pmi, $cid, $sig, $rel, $seg)
                """;
            var names = new[] { "$a", "$b", "$e0", "$e1", "$e2", "$e3", "$z0", "$z1", "$z2", "$z3", "$zc", "$q", "$s", "$pmi", "$cid", "$sig", "$rel", "$seg" };
            var ps = names.Select(n => cmd.Parameters.Add(n, SqliteType.Text)).ToArray();
            foreach (var r in pairs ?? [])
            {
                ps[0].Value = songs[r.A].WorkId;
                ps[1].Value = songs[r.B].WorkId;
                for (int ch = 0; ch < 4; ch++)
                {
                    ps[2 + ch].SqliteType = SqliteType.Real;
                    ps[2 + ch].Value = !r.Contemporaneous && r.Avail[ch] ? r.E[ch] : DBNull.Value;
                    ps[6 + ch].SqliteType = SqliteType.Real;
                    ps[6 + ch].Value = !r.Contemporaneous && r.Avail[ch] && !double.IsNaN(r.Z[ch]) ? r.Z[ch] : DBNull.Value;
                }
                SetReal(ps[10], r.Contemporaneous || !r.Tested ? double.NaN : r.Zc);
                SetReal(ps[11], r.Q);
                SetReal(ps[12], r.Contemporaneous || !r.Tested ? double.NaN : r.S);
                SetReal(ps[13], r.Pmi);
                SetReal(ps[14], r.ChordId);
                ps[15].SqliteType = SqliteType.Integer;
                ps[15].Value = r.Significant ? 1 : 0;
                ps[16].Value = r.Relation;
                ps[17].Value = r.Segments.Count > 0 ? SegmentsJson(r.Segments) : DBNull.Value;
                cmd.ExecuteNonQuery();
            }
        }
        using (var cmd = c.CreateCommand())
        {
            cmd.Transaction = tx;
            cmd.CommandText = "INSERT INTO influence_edge(a_id, b_id, kind, s_bits, credited) VALUES ($a, $b, $k, $s, $c)";
            var pa = cmd.Parameters.Add("$a", SqliteType.Text);
            var pb = cmd.Parameters.Add("$b", SqliteType.Text);
            var pk = cmd.Parameters.Add("$k", SqliteType.Text);
            var psb = cmd.Parameters.Add("$s", SqliteType.Real);
            var pc = cmd.Parameters.Add("$c", SqliteType.Integer);
            foreach (var (pair, kind, credited) in tree.Edges)
            {
                pa.Value = songs[pair.A].WorkId;
                pb.Value = songs[pair.B].WorkId;
                pk.Value = kind;
                psb.Value = pair.S;
                pc.Value = credited ? 1 : 0;
                cmd.ExecuteNonQuery();
            }
        }
        using (var cmd = c.CreateCommand())
        {
            cmd.Transaction = tx;
            cmd.CommandText = """
                INSERT INTO tree_node(work_id, parent_id, root_id, depth, ref_count, ref_norm, katz, descendants)
                VALUES ($w, $p, $r, $d, $rc, $rn, $k, $ds)
                """;
            var pw = cmd.Parameters.Add("$w", SqliteType.Text);
            var pp = cmd.Parameters.Add("$p", SqliteType.Text);
            var pr = cmd.Parameters.Add("$r", SqliteType.Text);
            var pd = cmd.Parameters.Add("$d", SqliteType.Integer);
            var prc = cmd.Parameters.Add("$rc", SqliteType.Integer);
            var prn = cmd.Parameters.Add("$rn", SqliteType.Real);
            var pk = cmd.Parameters.Add("$k", SqliteType.Real);
            var pds = cmd.Parameters.Add("$ds", SqliteType.Integer);
            for (int i = 0; i < songs.Length; i++)
            {
                pw.Value = songs[i].WorkId;
                pp.Value = tree.Parent[i] >= 0 ? songs[tree.Parent[i]].WorkId : DBNull.Value;
                pr.Value = songs[tree.Root[i]].WorkId;
                pd.Value = tree.Depth[i];
                prc.Value = tree.RefCount[i];
                prn.Value = tree.RefNorm[i] is double rn ? rn : DBNull.Value;
                pk.Value = tree.Katz[i];
                pds.Value = tree.Descendants[i];
                cmd.ExecuteNonQuery();
            }
        }
        tx.Commit();
    }

    private static void SetReal(SqliteParameter p, double v)
    {
        p.SqliteType = SqliteType.Real;
        p.Value = double.IsNaN(v) || double.IsInfinity(v) ? DBNull.Value : v;
    }

    public static string SegmentsJson(IEnumerable<Segment> segs)
    {
        var sb = new StringBuilder("[");
        bool first = true;
        foreach (var s in segs)
        {
            if (!first) sb.Append(',');
            first = false;
            sb.Append("{\"channel\":\"").Append(Channels.Name(s.Channel)).Append('"');
            sb.Append(",\"a_start\":").Append(Num(s.AStart)).Append(",\"a_end\":").Append(Num(s.AEnd));
            sb.Append(",\"b_start\":").Append(Num(s.BStart)).Append(",\"b_end\":").Append(Num(s.BEnd));
            sb.Append(",\"bits\":").Append(Num(s.Bits)).Append(",\"n\":").Append(s.N.ToString(CultureInfo.InvariantCulture));
            if (s.Channel == Channel.Loop)
            {
                sb.Append(",\"phase\":\"").Append(s.SamePhase ? "same" : "cross").Append('"');
                if (s.Loop != null) sb.Append(",\"loop\":").Append(JsonSerializer.Serialize(s.Loop));
            }
            sb.Append('}');
        }
        return sb.Append(']').ToString();
    }

    public static List<Segment> ParseSegments(string? json)
    {
        var list = new List<Segment>();
        if (string.IsNullOrEmpty(json)) return list;
        using var doc = JsonDocument.Parse(json);
        foreach (var e in doc.RootElement.EnumerateArray())
        {
            var ch = Array.IndexOf(Channels.Names, e.GetProperty("channel").GetString());
            list.Add(new Segment
            {
                Channel = (Channel)Math.Max(0, ch),
                AStart = e.GetProperty("a_start").GetDouble(), AEnd = e.GetProperty("a_end").GetDouble(),
                BStart = e.GetProperty("b_start").GetDouble(), BEnd = e.GetProperty("b_end").GetDouble(),
                Bits = e.GetProperty("bits").GetDouble(),
                N = e.TryGetProperty("n", out var n) ? n.GetInt32() : 0,
                SamePhase = e.TryGetProperty("phase", out var ph) && ph.GetString() == "same",
                Loop = e.TryGetProperty("loop", out var lp) ? lp.GetString() : null,
            });
        }
        return list;
    }

    public static string Num(double v) => Math.Round(v, 4).ToString("R", CultureInfo.InvariantCulture);

    // ------------------------------------------------------------------------------ reading back (export)
    public sealed record StoredPair(string A, string B, double?[] E, double?[] Z, double? Zc, double? Q, double? S, double? Pmi,
        double? ChordId, bool Significant, string Relation, List<Segment> Segments);

    public static List<StoredPair> LoadPairs(SqliteConnection c, bool significantOnly)
    {
        var list = new List<StoredPair>();
        using var cmd = c.CreateCommand();
        cmd.CommandText = """
            SELECT a_id, b_id, e_melody, e_bass, e_chord, e_loop, z_melody, z_bass, z_chord, z_loop, z_combined, q, s_bits,
                   pmi, chord_identity, significant, relation, segments_json FROM pair_score
            """ + (significantOnly ? " WHERE significant = 1" : "") + " ORDER BY b_id, a_id";
        using var r = cmd.ExecuteReader();
        while (r.Read())
            list.Add(new StoredPair(r.GetString(0), r.GetString(1),
                [Dbl(r, 2), Dbl(r, 3), Dbl(r, 4), Dbl(r, 5)], [Dbl(r, 6), Dbl(r, 7), Dbl(r, 8), Dbl(r, 9)],
                Dbl(r, 10), Dbl(r, 11), Dbl(r, 12), Dbl(r, 13), Dbl(r, 14), (Int(r, 15) ?? 0) != 0, Str(r, 16) ?? "none",
                ParseSegments(Str(r, 17))));
        return list;
    }

    public sealed record StoredEdge(string A, string B, string Kind, double S, bool Credited);

    public static List<StoredEdge> LoadEdges(SqliteConnection c)
    {
        var list = new List<StoredEdge>();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT a_id, b_id, kind, s_bits, credited FROM influence_edge ORDER BY b_id, a_id";
        using var r = cmd.ExecuteReader();
        while (r.Read()) list.Add(new StoredEdge(r.GetString(0), r.GetString(1), r.GetString(2), r.GetDouble(3), r.GetInt32(4) != 0));
        return list;
    }

    public sealed record StoredNode(string WorkId, string? Parent, string Root, int Depth, int RefCount, double? RefNorm, double? Katz, int Descendants);

    public static Dictionary<string, StoredNode> LoadTree(SqliteConnection c)
    {
        var d = new Dictionary<string, StoredNode>(StringComparer.Ordinal);
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT work_id, parent_id, root_id, depth, ref_count, ref_norm, katz, descendants FROM tree_node";
        using var r = cmd.ExecuteReader();
        while (r.Read())
            d[r.GetString(0)] = new StoredNode(r.GetString(0), Str(r, 1), r.GetString(2), r.GetInt32(3), r.GetInt32(4), Dbl(r, 5), Dbl(r, 6), r.GetInt32(7));
        return d;
    }

    // ------------------------------------------------------------------------------ JSON helpers
    public static int[] Ints(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var a = new int[doc.RootElement.GetArrayLength()];
        int i = 0;
        foreach (var e in doc.RootElement.EnumerateArray()) a[i++] = e.ValueKind == JsonValueKind.Number ? (int)Math.Round(e.GetDouble()) : 0;
        return a;
    }

    public static double[] Doubles(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var a = new double[doc.RootElement.GetArrayLength()];
        int i = 0;
        foreach (var e in doc.RootElement.EnumerateArray()) a[i++] = e.ValueKind == JsonValueKind.Number ? e.GetDouble() : 0;
        return a;
    }

    private static int[] DotInts(string s) =>
        s.Split('.', StringSplitOptions.RemoveEmptyEntries).Select(x => int.Parse(x, CultureInfo.InvariantCulture)).ToArray();
}
