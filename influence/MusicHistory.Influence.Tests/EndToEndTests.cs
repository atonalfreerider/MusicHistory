using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Influence.Tests;

/// <summary>make-fixture -> run (twice) -> graph DB invariants, relative paths, determinism, report.</summary>
public class EndToEndTests
{
    private static long Scalar(SqliteConnection c, string sql)
    {
        using var cmd = c.CreateCommand();
        cmd.CommandText = sql;
        return Convert.ToInt64(cmd.ExecuteScalar() ?? 0L);
    }

    [Fact]
    public void FixtureRunProducesAValidDeterministicGraph()
    {
        string root = Path.Combine(Path.GetTempPath(), "mh-influence-e2e-" + Environment.ProcessId);
        if (Directory.Exists(root)) Directory.Delete(root, true);
        string db = Path.Combine(root, "data", "musichistory.sqlite");
        string graph = Path.Combine(root, "data", "graph", "music_graph.db");
        string graph2 = Path.Combine(root, "data", "graph2", "music_graph.db");
        var log = new StringWriter();
        Fixture.Make(db, 150, 3, log);

        var p = new Params { Threads = 4 };
        var o1 = new RunOptions { Db = db, Graph = graph, Root = root, GeneratedAt = "2026-01-01T00:00:00Z" };
        Assert.Equal(0, Runner.Run(o1, p, log));
        var o2 = new RunOptions { Db = db, Graph = graph2, Root = root, GeneratedAt = "2026-01-01T00:00:00Z", Report = Path.Combine(root, "r2.json") };
        Assert.Equal(0, Runner.Run(o2, new Params { Threads = 2 }, log));

        // Determinism: same bytes (fixed generated_at), whatever the thread count.
        Assert.Equal(SHA256.HashData(File.ReadAllBytes(graph)), SHA256.HashData(File.ReadAllBytes(graph2)));
        Assert.False(File.Exists(graph + "-journal"));
        Assert.False(File.Exists(graph + "-wal"));

        using var g = new SqliteConnection($"Data Source={graph};Mode=ReadOnly;Pooling=False");
        g.Open();
        long n = Scalar(g, "SELECT COUNT(*) FROM song_node");
        Assert.Equal(150, n);
        Assert.Equal(n, Scalar(g, "SELECT COUNT(*) FROM nodes"));
        Assert.Equal(1, Scalar(g, "SELECT MIN(id) FROM nodes"));
        Assert.Equal(n, Scalar(g, "SELECT MAX(id) FROM nodes"));
        Assert.Equal(0, Scalar(g, "SELECT COUNT(*) FROM nodes WHERE position_x IS NOT NULL OR position_y IS NOT NULL OR position_z IS NOT NULL"));
        // Node ids ordered by (time_value, work_id).
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM song_node a JOIN song_node b ON b.node_id = a.node_id + 1
            WHERE b.time_value < a.time_value OR (b.time_value = a.time_value AND b.work_id < a.work_id)
            """));
        // Exactly one tree edge into every non-root, from its parent; sources earlier.
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM song_node s WHERE s.tree_parent_node IS NOT NULL AND
              (SELECT COUNT(*) FROM influence_edges e WHERE e.target_node = s.node_id AND e.kind = 'tree') <> 1
            """));
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM influence_edges e JOIN song_node t ON t.node_id = e.target_node
            WHERE e.kind = 'tree' AND e.source_node <> t.tree_parent_node
            """));
        Assert.Equal(0, Scalar(g, "SELECT COUNT(*) FROM song_node s WHERE s.tree_parent_node IS NULL AND EXISTS (SELECT 1 FROM influence_edges e WHERE e.target_node = s.node_id AND e.kind = 'tree')"));
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM influence_edges e JOIN song_node a ON a.node_id = e.source_node JOIN song_node b ON b.node_id = e.target_node
            WHERE NOT (a.time_value < b.time_value) OR e.source_node >= e.target_node
            """));
        Assert.Equal(0, Scalar(g, "SELECT COUNT(*) FROM influence_edges WHERE similarity < 0 OR similarity > 1 OR weight <= 0 OR kind NOT IN ('tree','secondary')"));
        Assert.Equal(0, Scalar(g, "SELECT COUNT(*) FROM (SELECT target_node, COUNT(*) c FROM influence_edges WHERE kind = 'secondary' GROUP BY target_node) WHERE c > 8"));
        Assert.Equal(Scalar(g, "SELECT COUNT(*) FROM song_node WHERE tree_parent_node IS NULL"), Scalar(g, "SELECT CAST(value AS INTEGER) FROM graph_meta WHERE key = 'root_count'"));
        Assert.Equal(Scalar(g, "SELECT COUNT(*) FROM influence_edges"), Scalar(g, "SELECT CAST(value AS INTEGER) FROM graph_meta WHERE key = 'edge_count'"));
        // Roots are their own roots at depth 0; children one deeper than their parent.
        Assert.Equal(0, Scalar(g, "SELECT COUNT(*) FROM song_node WHERE tree_parent_node IS NULL AND (tree_root_node <> node_id OR tree_depth <> 0)"));
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM song_node c JOIN song_node p ON p.node_id = c.tree_parent_node
            WHERE c.tree_depth <> p.tree_depth + 1 OR c.tree_root_node <> p.tree_root_node
            """));
        // Excerpts: bar aligned, 8..24 bars (fixture songs are longer than 8 bars).
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM song_node WHERE excerpt_end_beat <= excerpt_start_beat
              OR (excerpt_end_beat - excerpt_start_beat) / beats_per_bar < 7.999 OR (excerpt_end_beat - excerpt_start_beat) / beats_per_bar > 24.001
              OR abs((excerpt_start_beat - first_downbeat) / beats_per_bar - round((excerpt_start_beat - first_downbeat) / beats_per_bar)) > 1e-6
            """));
        // MIDI paths are relative to the graph folder with '/' separators.
        using (var cmd = g.CreateCommand())
        {
            cmd.CommandText = "SELECT work_id, midi_path, normalized_midi_path, key_name FROM song_node";
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                string rel = r.GetString(1);
                Assert.DoesNotContain('\\', rel);
                Assert.False(Path.IsPathRooted(rel));
                Assert.Equal("../songs/" + r.GetString(0) + "/score.mid", rel);
                Assert.Equal(Path.GetFullPath(Path.Combine(root, "data", "songs", r.GetString(0), "score.mid")),
                    Path.GetFullPath(Path.Combine(Path.GetDirectoryName(graph)!, rel)));
                Assert.Equal("../normalized/" + r.GetString(0) + ".mid", r.GetString(2));
                Assert.Matches("^[A-G][b#]? (major|minor)$", r.GetString(3));
            }
        }
        Assert.Equal("1", g.CreateCommand() is var c1 && (c1.CommandText = "SELECT value FROM graph_meta WHERE key='schema_version'") != null ? c1.ExecuteScalar() as string : null);

        // Entry/exit keys (DESIGN.md §10) follow the fixture's key_region rows: the region at the excerpt
        // start and just before its end, NULL when that region is the home key. Some fixture songs modulate.
        using (var pk = new SqliteConnection($"Data Source={db};Mode=ReadOnly;Pooling=False"))
        {
            pk.Open();
            var regions = new Dictionary<string, List<(double Start, int Tonic, string Mode)>>();
            using (var cmd = pk.CreateCommand())
            {
                cmd.CommandText = "SELECT work_id, start_beat, tonic_pc, mode FROM key_region ORDER BY work_id, start_beat";
                using var r = cmd.ExecuteReader();
                while (r.Read())
                {
                    if (!regions.TryGetValue(r.GetString(0), out var l)) regions[r.GetString(0)] = l = [];
                    l.Add((r.GetDouble(1), r.GetInt32(2), r.GetString(3)));
                }
            }
            Assert.Contains(regions.Values, l => l.Count > 1);
            int nonHome = 0;
            using var cmd2 = g.CreateCommand();
            cmd2.CommandText = """
                SELECT work_id, tonic_pc, mode, excerpt_start_beat, excerpt_end_beat, entry_tonic_pc, entry_mode, exit_tonic_pc, exit_mode
                FROM song_node
                """;
            using var rr = cmd2.ExecuteReader();
            while (rr.Read())
            {
                var regs = regions[rr.GetString(0)];
                (int?, string?) Expect(double beat)
                {
                    var k = regs.Last(x => x.Start <= beat + 1e-6 || x == regs[0]);
                    return k.Tonic == rr.GetInt32(1) && k.Mode == rr.GetString(2) ? (null, null) : (k.Tonic, k.Mode);
                }
                (int?, string?) Got(int i) => (rr.IsDBNull(i) ? null : rr.GetInt32(i), rr.IsDBNull(i + 1) ? null : rr.GetString(i + 1));
                Assert.Equal(Expect(rr.GetDouble(3)), Got(5));
                Assert.Equal(Expect(rr.GetDouble(4) - 1e-3), Got(7));
                if (!rr.IsDBNull(5) || !rr.IsDBNull(7)) nonHome++;
            }
            Assert.True(nonHome > 0, "no excerpt enters or leaves outside its home key; the fixture should exercise that");
        }
        // graph_meta settings come from the fixture's analyze meta, not from the environment.
        using (var cmd = g.CreateCommand())
        {
            cmd.CommandText = "SELECT value FROM graph_meta WHERE key = 'normalization'";
            Assert.Equal("relative", cmd.ExecuteScalar() as string);
        }

        // Pipeline tables replaced, one tree_node per song.
        using var pc = new SqliteConnection($"Data Source={db};Mode=ReadOnly;Pooling=False");
        pc.Open();
        Assert.Equal(150, Scalar(pc, "SELECT COUNT(*) FROM tree_node"));
        Assert.Equal(Scalar(g, "SELECT COUNT(*) FROM influence_edges"), Scalar(pc, "SELECT COUNT(*) FROM influence_edge"));
        Assert.True(Scalar(pc, "SELECT COUNT(*) FROM pair_score") > 100);
        Assert.Equal(0, Scalar(pc, "SELECT COUNT(*) FROM pair_score WHERE significant = 1 AND relation <> 'influence'"));

        // Report: counts and validation against the planted truth.
        using var rep = JsonDocument.Parse(File.ReadAllText(Path.Combine(root, "data", "graph", "influence_report.json")));
        var v = rep.RootElement.GetProperty("validation");
        Assert.True(v.GetProperty("summary").GetProperty("positive").GetProperty("present").GetInt32() > 0);
        var neg = v.GetProperty("summary").GetProperty("negative");
        int withEdge = neg.TryGetProperty("with_edge", out var we) ? we.GetInt32() : 0;
        Assert.True(withEdge <= 0.02 * neg.GetProperty("present").GetInt32(), $"{withEdge} commonplace pairs with an edge");
        Assert.Equal(n, rep.RootElement.GetProperty("graph").GetProperty("nodes").GetInt32());
        Assert.Equal(0, rep.RootElement.GetProperty("warnings").GetArrayLength());
        Assert.Contains("analyze_normalization", rep.RootElement.GetProperty("graph_meta_settings").GetProperty("normalization_source").GetString());
        Assert.Contains("analyze_target_bpm", rep.RootElement.GetProperty("graph_meta_settings").GetProperty("target_bpm_source").GetString());
        Assert.True(rep.RootElement.GetProperty("graph").GetProperty("excerpts_outside_home_key").GetInt32() > 0);
        g.Close();
        pc.Close();
        SqliteConnection.ClearAllPools();
        try
        {
            Directory.Delete(root, true);
        }
        catch (IOException)
        {
            // Best effort: a scanner may still hold a file; the folder is under %TEMP%.
        }
    }
}
