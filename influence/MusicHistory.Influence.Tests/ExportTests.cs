using System.Globalization;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Influence.Tests;

/// <summary>A song of a hand-built pipeline DB (a tree root: its excerpt is the first 16 bars from first_downbeat).</summary>
internal sealed record MiniSong(string Id, int Tonic, string Mode, double FirstDownbeat = 0, double EndBeat = 200,
    (double Start, double End, int Tonic, string Mode)[]? Regions = null, string? Normalization = null, double? TargetBpm = null);

/// <summary>Small pipeline DBs in the db.py schema with just enough rows for <c>export</c>: work, song, key_region, tree_node.</summary>
internal sealed class MiniPipeline : IDisposable
{
    public readonly string Dir = Path.Combine(Path.GetTempPath(), "mh-influence-export-" + Guid.NewGuid().ToString("N"));

    public MiniPipeline() => Directory.CreateDirectory(Dir);

    public string Build(string name, IReadOnlyList<MiniSong> songs, IReadOnlyDictionary<string, string>? meta = null, params string[] extraSql)
    {
        string db = Path.Combine(Dir, "data", name + ".sqlite");
        Directory.CreateDirectory(Path.GetDirectoryName(db)!);
        using var c = PipelineDb.Open(db, create: true);
        PipelineDb.Exec(c, Schema.Pipeline);
        void Ins(string sql, params object?[] values)
        {
            using var cmd = c.CreateCommand();
            cmd.CommandText = sql;
            for (int i = 0; i < values.Length; i++) cmd.Parameters.AddWithValue("$" + (i + 1).ToString(CultureInfo.InvariantCulture), values[i] ?? DBNull.Value);
            cmd.ExecuteNonQuery();
        }
        foreach (var (k, v) in meta ?? new Dictionary<string, string>()) Ins("INSERT INTO meta(key, value) VALUES ($1, $2)", k, v);
        for (int i = 0; i < songs.Count; i++)
        {
            var s = songs[i];
            int year = 1960 + i;
            Ins("""
                INSERT INTO work(work_id, title, canonical_artist, work_date, work_date_precision, work_year, canon_rank, in_pool, selected)
                VALUES ($1, $2, 'Test Artist', $3, 9, $4, $5, 1, 1)
                """, s.Id, "Test " + s.Id, year.ToString(CultureInfo.InvariantCulture), year, i + 1);
            int target = s.Mode == "minor" ? 9 : 0;
            var cols = new List<string> { "work_id", "midi_path", "normalized_midi_path", "analysis_ok", "end_beat", "tonic_pc", "mode",
                "norm_shift", "native_bpm", "beats_per_bar", "first_downbeat" };
            var vals = new List<object?> { s.Id, $"data/songs/{s.Id}/score.mid", $"data/normalized/{s.Id}.mid", 1, s.EndBeat, s.Tonic, s.Mode,
                ((target - s.Tonic + 5) % 12 + 12) % 12 - 5, 120.0, 4.0, s.FirstDownbeat };
            // Only when given, so these tests also run against a schema without the migrated columns.
            if (s.Normalization != null) { cols.Add("normalization"); vals.Add(s.Normalization); }
            if (s.TargetBpm != null) { cols.Add("target_bpm"); vals.Add(s.TargetBpm); }
            Ins($"INSERT INTO song({string.Join(", ", cols)}) VALUES ({string.Join(", ", cols.Select((_, k) => "$" + (k + 1)))})", [.. vals]);
            foreach (var r in s.Regions ?? [])
                Ins("INSERT INTO key_region(work_id, start_beat, end_beat, tonic_pc, mode, shift) VALUES ($1, $2, $3, $4, $5, 0)",
                    s.Id, r.Start, r.End, r.Tonic, r.Mode);
            Ins("INSERT INTO tree_node(work_id, parent_id, root_id, depth, ref_count, ref_norm, katz, descendants) VALUES ($1, NULL, $1, 0, 0, NULL, 0, 0)", s.Id);
        }
        foreach (string sql in extraSql) PipelineDb.Exec(c, sql);
        return db;
    }

    /// <summary>Exports <paramref name="db"/> with the default (process) environment; returns the graph path.</summary>
    public string Export(string db, TextWriter? log = null)
    {
        string graph = Path.Combine(Dir, "data", "graph", Path.GetFileNameWithoutExtension(db) + ".db");
        using var c = PipelineDb.Open(db);
        GraphExport.Export(c, graph, Dir, new Params(), "2026-01-01T00:00:00Z", log ?? TextWriter.Null);
        return graph;
    }

    public static List<object?[]> Rows(string db, string sql)
    {
        using var c = new SqliteConnection($"Data Source={db};Mode=ReadOnly;Pooling=False");
        c.Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = sql;
        using var r = cmd.ExecuteReader();
        var rows = new List<object?[]>();
        while (r.Read())
        {
            var row = new object?[r.FieldCount];
            for (int i = 0; i < r.FieldCount; i++) row[i] = r.IsDBNull(i) ? null : r.GetValue(i);
            rows.Add(row);
        }
        return rows;
    }

    public static Dictionary<string, string?> GraphMeta(string graph) =>
        Rows(graph, "SELECT key, value FROM graph_meta").ToDictionary(r => (string)r[0]!, r => (string?)r[1]);

    public void Dispose()
    {
        SqliteConnection.ClearAllPools();
        try
        {
            Directory.Delete(Dir, true);
        }
        catch (IOException)
        {
            // Best effort under %TEMP%.
        }
    }
}

/// <summary>
/// Review finding influence #1: song_node must carry the key heard where the excerpt starts and ends
/// (DESIGN.md §10 entry_* / exit_*), so a modulating excerpt hands off in the key actually heard.
/// </summary>
public class ExportKeyTests
{
    [Fact]
    public void EntryAndExitKeysComeFromTheKeyRegionsAroundTheExcerpt()
    {
        using var mini = new MiniPipeline();
        // Every song is a root without loops: excerpt = first 16 bars from first_downbeat ([0, 64] or [4, 68]).
        var songs = new List<MiniSong>
        {
            // Home C major; modulates to D major at beat 32, inside the excerpt: enters home, leaves in D major.
            new("RA0000000001", 0, "major", Regions: [(0, 32, 0, "major"), (32, 200, 2, "major")]),
            // Home G major; the first 16 bars are in E minor: enters and leaves in E minor.
            new("RB0000000002", 7, "major", Regions: [(0, 64, 4, "minor"), (64, 200, 7, "major")]),
            // Home A minor; C major starts exactly at the excerpt's end: the exit is still the home key.
            new("RC0000000003", 9, "minor", Regions: [(0, 64, 9, "minor"), (64, 200, 0, "major")]),
            // No key_region rows: home key throughout.
            new("RD0000000004", 5, "major"),
            // Home Eb major, first downbeat 4 (excerpt [4, 68]); starts in C minor, home from beat 40.
            new("RE0000000005", 3, "major", FirstDownbeat: 4, Regions: [(0, 40, 0, "minor"), (40, 200, 3, "major")]),
            // Home D major, whole song in D minor per its regions: same tonic, other mode is not the home key.
            new("RF0000000006", 2, "major", Regions: [(0, 200, 2, "minor")]),
        };
        string graph = mini.Export(mini.Build("keys", songs));
        var rows = MiniPipeline.Rows(graph, """
            SELECT work_id, excerpt_start_beat, excerpt_end_beat, entry_tonic_pc, entry_mode, exit_tonic_pc, exit_mode
            FROM song_node ORDER BY work_id
            """).ToDictionary(r => (string)r[0]!);
        void Check(string id, double start, double end, int? et, string? em, int? xt, string? xm)
        {
            var r = rows[id];
            Assert.Equal(start, Convert.ToDouble(r[1]));
            Assert.Equal(end, Convert.ToDouble(r[2]));
            Assert.Equal(et, r[3] == null ? null : (int?)Convert.ToInt32(r[3]));
            Assert.Equal(em, (string?)r[4]);
            Assert.Equal(xt, r[5] == null ? null : (int?)Convert.ToInt32(r[5]));
            Assert.Equal(xm, (string?)r[6]);
        }
        Check("RA0000000001", 0, 64, null, null, 2, "major");
        Check("RB0000000002", 0, 64, 4, "minor", 4, "minor");
        Check("RC0000000003", 0, 64, null, null, null, null);
        Check("RD0000000004", 0, 64, null, null, null, null);
        Check("RE0000000005", 4, 68, 0, "minor", null, null);
        Check("RF0000000006", 0, 64, 2, "minor", 2, "minor");
    }
}

/// <summary>
/// Review finding influence #0: graph_meta.normalization / target_key / target_bpm must come from the
/// analyze stage's records (pipeline meta analyze_normalization / analyze_target_bpm), not from the
/// environment of the exporting process.
/// </summary>
public class ExportMetaTests
{
    [Fact]
    public void GraphMetaSettingsComeFromTheAnalyzeMeta()
    {
        using var mini = new MiniPipeline();
        var meta = new Dictionary<string, string> { ["analyze_normalization"] = "parallel", ["analyze_target_bpm"] = "100" };
        string graph = mini.Export(mini.Build("meta", [new("RA0000000001", 0, "major"), new("RB0000000002", 9, "minor")], meta));
        var gm = MiniPipeline.GraphMeta(graph);
        Assert.Equal("parallel", gm["normalization"]);
        Assert.Equal("C major / C minor", gm["target_key"]);
        Assert.Equal("100", gm["target_bpm"]);
    }
}
