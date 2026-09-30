using System.Globalization;
using System.Text;
using System.Text.Json;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Influence;

/// <summary>
/// The lineage mode's private pipeline tables (DESIGN.md §2: a stage creates its extra tables itself) and the shared
/// influence tables it fills: <c>influence_edge</c> (the lineage edges, <c>s_bits</c> = score) and <c>tree_node</c>
/// (the lineage tree); <c>pair_score</c> keeps the strict evidence (the strong matches). <c>export</c> and
/// <c>retree</c> rebuild everything from these tables; the pipeline meta key <c>influence_edge_semantics</c>
/// (= <c>identity_lineage</c>) tells them which graph the tables hold.
/// </summary>
internal static class LineageStore
{
    public const string MetaSemantics = "influence_edge_semantics";
    public const string Lineage = "identity_lineage";
    public const string Strict = "strict_evidence";

    public const string Tables = """
        CREATE TABLE IF NOT EXISTS lineage_family(
          family_id INTEGER PRIMARY KEY, fkey TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, label TEXT NOT NULL, roman TEXT,
          channel TEXT NOT NULL, channels TEXT NOT NULL, size INTEGER NOT NULL, specificity REAL NOT NULL, z REAL, q REAL);
        CREATE TABLE IF NOT EXISTS lineage_member(
          family_id INTEGER NOT NULL, work_id TEXT NOT NULL, strength REAL NOT NULL, first_beat REAL NOT NULL, first_end REAL NOT NULL,
          variant TEXT, loops_json TEXT, PRIMARY KEY(family_id, work_id));
        CREATE TABLE IF NOT EXISTS lineage_edge(
          a_id TEXT NOT NULL, b_id TEXT NOT NULL, kind TEXT NOT NULL, family_id INTEGER NOT NULL, score REAL NOT NULL,
          closeness REAL NOT NULL, credited INTEGER NOT NULL, a_start REAL NOT NULL, a_end REAL NOT NULL, b_start REAL NOT NULL,
          b_end REAL NOT NULL, channels TEXT NOT NULL, families_json TEXT NOT NULL, PRIMARY KEY(a_id, b_id));
        CREATE TABLE IF NOT EXISTS lineage_excerpt(
          work_id TEXT PRIMARY KEY, start_beat REAL NOT NULL, end_beat REAL NOT NULL, source TEXT NOT NULL);
        """;

    /// <summary>A TreeResult view of the lineage tree for <see cref="PipelineDb.WriteResults"/> (influence_edge rows: A, B, kind, score, credited).</summary>
    public static TreeResult AsTree(LineageResult r) => new()
    {
        Parent = r.Parent, Root = r.Root, Depth = r.Depth, RefCount = r.RefCount, Descendants = r.Descendants, RefNorm = r.RefNorm, Katz = r.Katz,
        Edges = r.Edges.Select(e => (new PairResult { A = e.Pair.A, B = e.Pair.B, S = e.Pair.Score }, e.Kind, e.Credited)).ToList(),
        CreditedSpans = Enumerable.Range(0, r.Songs.Length).Select(_ => new List<CreditedSpan>()).ToArray(),
    };

    /// <summary>Replace the lineage tables' rows (inside the transaction of <see cref="PipelineDb.WriteResults"/>).</summary>
    public static void Write(SqliteConnection c, SqliteTransaction tx, LineageResult r)
    {
        var songs = r.Songs;
        foreach (string t in new[] { "lineage_family", "lineage_member", "lineage_edge", "lineage_excerpt" }) PipelineDb.Exec(c, $"DELETE FROM {t}", tx);
        Rows(c, tx, "INSERT INTO lineage_family VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)", r.Families.Select(f => new object?[]
        {
            f.Id, f.Key, Family.KindName(f.Kind), f.Label, f.Roman, f.Channel, f.Channels, f.Size, f.Specificity,
            f.Kind == FamilyKind.Strong ? f.Z : null, f.Kind == FamilyKind.Strong && !double.IsNaN(f.Q) ? f.Q : null,
        }));
        Rows(c, tx, "INSERT INTO lineage_member VALUES ($1, $2, $3, $4, $5, $6, $7)", r.Families.SelectMany(f => f.Members.Select(m => new object?[]
        {
            f.Id, songs[m.Song].WorkId, m.Strength, m.First, m.FirstEnd, m.Variant, m.Vars.Count > 0 ? LoopsJson(m.Vars) : null,
        })));
        Rows(c, tx, "INSERT INTO lineage_edge VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)", r.Edges.Select(e => new object?[]
        {
            songs[e.Pair.A].WorkId, songs[e.Pair.B].WorkId, e.Kind, e.Family.Id, e.Pair.Score, e.Pair.Close, e.Credited ? 1 : 0,
            e.AStart, e.AEnd, e.BStart, e.BEnd, e.ChannelsCsv,
            "[" + string.Join(",", e.Pair.Parts.Select(x => $"[{x.F.Id},{Num(x.C)}]")) + "]",
        }));
        Rows(c, tx, "INSERT INTO lineage_excerpt VALUES ($1, $2, $3, $4)", songs.Select(s => new object?[]
        {
            s.WorkId, r.Excerpt[s.Index].Start, r.Excerpt[s.Index].End, r.ExcerptSource[s.Index],
        }));
    }

    public static void Ensure(SqliteConnection c)
    {
        using var cmd = c.CreateCommand();
        cmd.CommandText = Tables;
        cmd.ExecuteNonQuery();
    }

    private static string Num(double v) => Math.Round(v, 6).ToString("R", CultureInfo.InvariantCulture);

    public static string LoopsJson(IEnumerable<LoopVar> vars)
    {
        var sb = new StringBuilder("[");
        bool first = true;
        foreach (var v in vars)
        {
            if (!first) sb.Append(',');
            first = false;
            sb.Append('[').Append(v.Phase).Append(",[").Append(string.Join(",", v.Tokens)).Append("],[").Append(string.Join(",", v.Rhythm)).Append("],")
                .Append(double.IsNaN(v.Coverage) ? "null" : Num(v.Coverage)).Append(',').Append(double.IsNaN(v.First) ? "null" : Num(v.First)).Append(',')
                .Append(Num(v.VisitBeats)).Append(',').Append(Num(v.LoopBeats)).Append(']');
        }
        return sb.Append(']').ToString();
    }

    public static List<LoopVar> ParseLoops(string? json)
    {
        var l = new List<LoopVar>();
        if (string.IsNullOrEmpty(json)) return l;
        using var doc = JsonDocument.Parse(json);
        foreach (var e in doc.RootElement.EnumerateArray())
        {
            double D(int i) => e[i].ValueKind == JsonValueKind.Number ? e[i].GetDouble() : double.NaN;
            l.Add(new LoopVar
            {
                Phase = e[0].GetInt32(), Tokens = e[1].EnumerateArray().Select(x => x.GetInt32()).ToArray(),
                Rhythm = e[2].EnumerateArray().Select(x => x.GetInt32()).ToArray(), Coverage = D(3), First = D(4), VisitBeats = D(5), LoopBeats = D(6),
            });
        }
        return l;
    }

    private static void Rows(SqliteConnection c, SqliteTransaction tx, string sql, IEnumerable<object?[]> rows)
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
                ps[i].Value = v is double d && (double.IsNaN(d) || double.IsInfinity(d)) ? DBNull.Value : v ?? DBNull.Value;
            }
            cmd.ExecuteNonQuery();
        }
    }

    // ------------------------------------------------------------------------------ reading back
    /// <summary>The stored families (members in song order) for the loaded songs; members of songs no longer loaded are dropped.</summary>
    public static List<Family> LoadFamilies(SqliteConnection c, Song[] songs)
    {
        var idx = songs.ToDictionary(s => s.WorkId, s => s.Index, StringComparer.Ordinal);
        var fams = new Dictionary<int, Family>();
        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText = "SELECT family_id, fkey, kind, label, roman, channel, channels, size, specificity, z, q FROM lineage_family ORDER BY family_id";
            using var r = cmd.ExecuteReader();
            while (r.Read())
                fams[r.GetInt32(0)] = new Family
                {
                    Id = r.GetInt32(0), Key = r.GetString(1), Kind = Family.ParseKind(r.GetString(2)), Label = r.GetString(3),
                    Roman = r.IsDBNull(4) ? null : r.GetString(4), Channel = r.GetString(5), Channels = r.GetString(6), Specificity = r.GetDouble(8),
                    Z = r.IsDBNull(9) ? 0 : r.GetDouble(9), Q = r.IsDBNull(10) ? double.NaN : r.GetDouble(10),
                };
        }
        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText = "SELECT family_id, work_id, strength, first_beat, first_end, variant, loops_json FROM lineage_member ORDER BY family_id, work_id";
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                if (!fams.TryGetValue(r.GetInt32(0), out var f) || !idx.TryGetValue(r.GetString(1), out int s)) continue;
                var m = new Member
                {
                    Song = s, Strength = r.GetDouble(2), First = r.GetDouble(3), FirstEnd = r.GetDouble(4), Variant = r.IsDBNull(5) ? null : r.GetString(5),
                };
                m.Vars.AddRange(ParseLoops(r.IsDBNull(6) ? null : r.GetString(6)));
                f.Members.Add(m);
            }
        }
        foreach (var f in fams.Values)
        {
            f.Members.Sort((x, y) => x.Song.CompareTo(y.Song));
            f.Reindex();
        }
        return [.. fams.Values.OrderBy(f => f.Id)];
    }

    public sealed record StoredEdge(string A, string B, string Kind, int FamilyId, double Score, double Close, bool Credited,
        double AStart, double AEnd, double BStart, double BEnd, string Channels);

    public static List<StoredEdge> LoadEdges(SqliteConnection c)
    {
        var l = new List<StoredEdge>();
        using var cmd = c.CreateCommand();
        cmd.CommandText = """
            SELECT a_id, b_id, kind, family_id, score, closeness, credited, a_start, a_end, b_start, b_end, channels
            FROM lineage_edge ORDER BY b_id, a_id
            """;
        using var r = cmd.ExecuteReader();
        while (r.Read())
            l.Add(new StoredEdge(r.GetString(0), r.GetString(1), r.GetString(2), r.GetInt32(3), r.GetDouble(4), r.GetDouble(5), r.GetInt32(6) != 0,
                r.GetDouble(7), r.GetDouble(8), r.GetDouble(9), r.GetDouble(10), r.GetString(11)));
        return l;
    }

    public static Dictionary<string, (double Start, double End, string Source)> LoadExcerpts(SqliteConnection c)
    {
        var d = new Dictionary<string, (double, double, string)>(StringComparer.Ordinal);
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT work_id, start_beat, end_beat, source FROM lineage_excerpt";
        using var r = cmd.ExecuteReader();
        while (r.Read()) d[r.GetString(0)] = (r.GetDouble(1), r.GetDouble(2), r.GetString(3));
        return d;
    }
}
