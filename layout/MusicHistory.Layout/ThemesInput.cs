using System.Globalization;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Layout;

/// <summary>The part of a §12 themes graph the layout needs, indexed by node id − 1.</summary>
internal sealed class ThemesGraph
{
    public required int Count { get; init; }
    public required string[] WorkId { get; init; }
    public required string[] Gender { get; init; }
    public required string[] TextSource { get; init; }
    /// <summary>Scores, <see cref="ThemesSchema.AnchorCount"/> per song (row-major), normalized to sum to 1.</summary>
    public required double[] Score { get; init; }
    /// <summary>themes_meta.ring_radius when present and valid.</summary>
    public double? MetaRadius { get; init; }
    public List<string> Warnings { get; } = [];

    public double ScoreOf(int song, int anchorIndex) => Score[song * ThemesSchema.AnchorCount + anchorIndex];
}

/// <summary>
/// Reads theme_anchor, theme_song and theme_score from a DESIGN.md §12 themes graph and refuses
/// files the layout cannot use (exit code 3, every problem listed, first 25):
/// <list type="bullet">
/// <item>the tables and columns exist; theme_anchor holds exactly the anchor ids 1..10;</item>
/// <item>theme_song.node_id is 1..N contiguous (N ≥ 1);</item>
/// <item>theme_score has exactly one row per (song, anchor), for known songs and anchors only;</item>
/// <item>every score is a finite number ≥ 0 and every song's scores have a positive sum.</item>
/// </list>
/// A song whose scores do not sum to 1 (beyond 1e-6) is normalized with a warning.
/// </summary>
internal static class ThemesInput
{
    private const int MaxListed = 25;

    public static ThemesGraph Load(string path)
    {
        using var c = GraphInput.OpenReadOnly(path);
        return Load(c);
    }

    public static ThemesGraph Load(SqliteConnection c)
    {
        var problems = new List<string>();
        void Require(string table, params string[] columns)
        {
            var have = GraphInput.Columns(c, table);
            if (have.Count == 0)
            {
                problems.Add($"table {table} is missing");
                return;
            }
            foreach (string col in columns)
                if (!have.Contains(col)) problems.Add($"{table}.{col} is missing");
        }
        Require("theme_anchor", "anchor_id", "angle", "position_x", "position_y", "position_z");
        Require("theme_song", "node_id", "work_id", "singer_gender", "text_source", "position_x", "position_y", "position_z");
        Require("theme_score", "node_id", "anchor_id", "score");
        Throw(problems, "the themes graph is missing tables or columns of DESIGN.md section 12");

        // Anchors: exactly 1..10.
        var anchors = new List<long>();
        using (var cmd = Cmd(c, "SELECT anchor_id FROM theme_anchor ORDER BY anchor_id"))
        using (var r = cmd.ExecuteReader())
            while (r.Read())
                anchors.Add(r.IsDBNull(0) ? -1 : r.GetInt64(0));
        if (!anchors.SequenceEqual(Enumerable.Range(1, ThemesSchema.AnchorCount).Select(i => (long)i)))
            problems.Add($"theme_anchor must hold anchor ids 1..{ThemesSchema.AnchorCount}, found [{string.Join(", ", anchors)}]");

        // Songs: 1..N contiguous.
        var workIds = new List<string>();
        var genders = new List<string>();
        var sources = new List<string>();
        using (var cmd = Cmd(c, "SELECT node_id, work_id, singer_gender, text_source FROM theme_song ORDER BY node_id"))
        using (var r = cmd.ExecuteReader())
        {
            int expect = 1;
            while (r.Read())
            {
                long id = r.GetInt64(0);
                if (id != expect && problems.Count < MaxListed)
                    problems.Add($"theme_song.node_id must be 1..N contiguous: expected {expect}, found {id}");
                expect++;
                workIds.Add(r.IsDBNull(1) ? "" : r.GetString(1));
                genders.Add(r.IsDBNull(2) ? "" : r.GetString(2));
                sources.Add(r.IsDBNull(3) ? "" : r.GetString(3));
            }
        }
        int n = workIds.Count;
        if (n == 0) throw new InvalidGraphException("the themes graph has no songs", ["theme_song is empty"]);
        Throw(problems, "the themes graph breaks DESIGN.md section 12");

        // Scores: one finite, non-negative value per (song, anchor).
        const int k = ThemesSchema.AnchorCount;
        var score = new double[n * k];
        var seen = new bool[n * k];
        using (var cmd = Cmd(c, "SELECT node_id, anchor_id, score FROM theme_score ORDER BY node_id, anchor_id"))
        using (var r = cmd.ExecuteReader())
            while (r.Read())
            {
                long node = r.GetValue(0) is long nv ? nv : -1, anchor = r.GetValue(1) is long av ? av : -1;
                if (node < 1 || node > n || anchor < 1 || anchor > k)
                {
                    Add(problems, $"theme_score row ({node}, {anchor}) names an unknown song or anchor");
                    continue;
                }
                int at = (int)(node - 1) * k + (int)(anchor - 1);
                object v = r.GetValue(2);
                double s = v is double d ? d : v is long l ? l : double.NaN;
                if (!double.IsFinite(s) || s < 0)
                    Add(problems, $"theme_score ({node}, {anchor}) = {Describe(v)} is not a finite number >= 0");
                seen[at] = true;
                score[at] = double.IsFinite(s) ? s : 0;
            }
        for (int i = 0; i < n; i++)
            for (int a = 0; a < k; a++)
                if (!seen[i * k + a])
                    Add(problems, $"song {i + 1} has no theme_score row for anchor {a + 1}");
        var warnings = new List<string>();
        int renormalized = 0;
        for (int i = 0; i < n; i++)
        {
            double sum = 0;
            for (int a = 0; a < k; a++) sum += score[i * k + a];
            if (!(sum > 0))
            {
                Add(problems, $"song {i + 1} ({workIds[i]}) has no positive score");
                continue;
            }
            if (Math.Abs(sum - 1) > 1e-6) renormalized++;
            for (int a = 0; a < k; a++) score[i * k + a] /= sum;
        }
        Throw(problems, "the themes graph breaks DESIGN.md section 12");
        if (renormalized > 0)
            warnings.Add($"{renormalized} song(s) had scores not summing to 1; normalized");

        double? metaRadius = null;
        if (GraphInput.Columns(c, "themes_meta").Contains("value"))
        {
            using var cmd = Cmd(c, "SELECT value FROM themes_meta WHERE key = 'ring_radius'");
            if (cmd.ExecuteScalar() is string sr && double.TryParse(sr, NumberStyles.Float, CultureInfo.InvariantCulture, out double rr)
                && double.IsFinite(rr) && rr > 0)
                metaRadius = rr;
        }

        var g = new ThemesGraph
        {
            Count = n, WorkId = [.. workIds], Gender = [.. genders], TextSource = [.. sources], Score = score, MetaRadius = metaRadius,
        };
        g.Warnings.AddRange(warnings);
        return g;
    }

    private static void Add(List<string> problems, string p)
    {
        if (problems.Count < MaxListed) problems.Add(p);
        else if (problems.Count == MaxListed) problems.Add("... (more problems not listed)");
    }

    private static void Throw(List<string> problems, string what)
    {
        if (problems.Count == 0) return;
        throw new InvalidGraphException($"{what}:\n  " + string.Join("\n  ", problems), problems);
    }

    private static SqliteCommand Cmd(SqliteConnection c, string sql)
    {
        var cmd = c.CreateCommand();
        cmd.CommandText = sql;
        return cmd;
    }

    private static string Describe(object v) => v switch
    {
        DBNull => "NULL",
        string s => $"'{s}'",
        IFormattable f => f.ToString(null, CultureInfo.InvariantCulture),
        _ => v.ToString() ?? "?",
    };
}
