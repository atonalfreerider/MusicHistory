using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Influence.Tests;

/// <summary>
/// make-fixture -> run (the default lineage mode) twice, export and retree: identical bytes, the identity tables of
/// DESIGN.md §8b, labels that name a family both songs belong to, the §10 invariants; and --mode evidence: the unchanged
/// strict graph (no identity tables, no edge_semantics), also after a lineage run in the same pipeline DB.
/// </summary>
public class LineageEndToEndTests
{
    private const string Stamp = "2026-01-01T00:00:00Z";

    private static string NewRoot(string tag)
    {
        string root = Path.Combine(Path.GetTempPath(), $"mh-lineage-{tag}-{Environment.ProcessId}");
        if (Directory.Exists(root)) Directory.Delete(root, true);
        return root;
    }

    private static byte[] Hash(string path) => SHA256.HashData(File.ReadAllBytes(path));

    private static long Scalar(SqliteConnection c, string sql)
    {
        using var cmd = c.CreateCommand();
        cmd.CommandText = sql;
        return Convert.ToInt64(cmd.ExecuteScalar() ?? 0L);
    }

    private static int Cli(params string[] args) => Program.Main(args);

    [Fact]
    public void LineageRunExportAndRetreeAgreeAndLabelSharedFamilies()
    {
        string root = NewRoot("run");
        string db = Path.Combine(root, "data", "musichistory.sqlite");
        string G(string name) => Path.Combine(root, "data", name, "music_graph.db");
        var log = new StringWriter();
        Fixture.Make(db, 120, 5, log);
        var lp = new LineageParams();
        Assert.Equal(0, LineageRunner.Run(new RunOptions { Db = db, Graph = G("g1"), Root = root, GeneratedAt = Stamp }, new Params { Threads = 4, TargetFpr = 1e-3 }, lp, log));
        Assert.Equal(0, LineageRunner.Run(new RunOptions { Db = db, Graph = G("g2"), Root = root, GeneratedAt = Stamp, Report = Path.Combine(root, "r2.json") },
            new Params { Threads = 2, TargetFpr = 1e-3 }, lp, log));
        Assert.Equal(Hash(G("g1")), Hash(G("g2")));
        // export (the stored semantics decide) and retree (no rescoring) reproduce the run.
        Assert.Equal(0, Cli("export", "--db", db, "--graph", G("g3"), "--root", root, "--generated-at", Stamp));
        Assert.Equal(Hash(G("g1")), Hash(G("g3")));
        Assert.Equal(0, Cli("retree", "--db", db, "--graph", G("g4"), "--root", root, "--generated-at", Stamp));
        Assert.Equal(Hash(G("g1")), Hash(G("g4")));

        using var g = new SqliteConnection($"Data Source={G("g1")};Mode=ReadOnly;Pooling=False");
        g.Open();
        var meta = MiniPipeline.GraphMeta(G("g1"));
        Assert.Equal("identity_lineage", meta["edge_semantics"]);
        Assert.Equal("identity_lineage", meta["influence_edge_semantics"]);
        long fams = Scalar(g, "SELECT COUNT(*) FROM identity_family");
        Assert.True(fams > 0);
        Assert.Equal(fams.ToString(System.Globalization.CultureInfo.InvariantCulture), meta["family_count"]);
        Assert.Equal(0, Scalar(g, "SELECT COUNT(*) FROM identity_family WHERE kind NOT IN ('schema','loop','progression','strong')"));
        Assert.Equal(0, Scalar(g, "SELECT COUNT(*) FROM identity_family f WHERE size <> (SELECT COUNT(*) FROM song_family s WHERE s.family_id = f.family_id)"));
        Assert.Equal(0, Scalar(g, "SELECT COUNT(*) FROM song_family s WHERE s.node_id NOT IN (SELECT id FROM nodes) OR s.strength <= 0 OR s.strength > 1"));
        // Every edge names a family both songs belong to, strictly earlier -> later; z is the strong match's or 0.
        Assert.True(Scalar(g, "SELECT COUNT(*) FROM influence_edges") > 0);
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM influence_edges e WHERE NOT EXISTS (
              SELECT 1 FROM identity_family f JOIN song_family a ON a.family_id = f.family_id AND a.node_id = e.source_node
              JOIN song_family b ON b.family_id = f.family_id AND b.node_id = e.target_node WHERE f.label = e.evidence)
            """));
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM influence_edges e JOIN song_node s ON s.node_id = e.source_node JOIN song_node t ON t.node_id = e.target_node
            WHERE e.source_node >= e.target_node OR s.time_value >= t.time_value
            """));
        // primary_channel is one of channels (the viewer's contract).
        Assert.Equal(0, Scalar(g, "SELECT COUNT(*) FROM influence_edges WHERE instr(',' || channels || ',', ',' || primary_channel || ',') = 0"));
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM influence_edges e WHERE (e.z <> 0) <> EXISTS (SELECT 1 FROM identity_family f JOIN song_family a ON
              a.family_id = f.family_id AND a.node_id = e.source_node JOIN song_family b ON b.family_id = f.family_id AND b.node_id = e.target_node
              WHERE f.kind = 'strong' AND f.label = e.evidence)
            """));
        // The §10 tree invariants and the excerpt bounds (8..24 bars).
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM song_node s WHERE (s.tree_parent_node IS NULL) <> ((SELECT COUNT(*) FROM influence_edges e
              WHERE e.target_node = s.node_id AND e.kind = 'tree') = 0)
            """));
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM influence_edges e JOIN song_node s ON s.node_id = e.target_node
            WHERE e.kind = 'tree' AND e.source_node <> s.tree_parent_node
            """));
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM influence_edges e JOIN song_node s ON s.node_id = e.source_node JOIN song_node t ON t.node_id = e.target_node
            WHERE e.src_end_beat - e.src_start_beat < 8 * s.beats_per_bar - 1e-6 OR e.src_end_beat - e.src_start_beat > 24 * s.beats_per_bar + 1e-6
               OR e.dst_end_beat - e.dst_start_beat < 8 * t.beats_per_bar - 1e-6 OR e.dst_end_beat - e.dst_start_beat > 24 * t.beats_per_bar + 1e-6
            """));
        // A child's excerpt is where its tree edge's identity sounds.
        Assert.Equal(0, Scalar(g, """
            SELECT COUNT(*) FROM influence_edges e JOIN song_node t ON t.node_id = e.target_node
            WHERE e.kind = 'tree' AND (t.excerpt_start_beat <> e.dst_start_beat OR t.excerpt_end_beat <> e.dst_end_beat)
            """));
        Assert.True(Scalar(g, "SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM influence_edges WHERE kind = 'secondary' GROUP BY target_node)") <= 8);

        // Pipeline tables and the report.
        using var c = PipelineDb.Open(db);
        Assert.Equal(120, Scalar(c, "SELECT COUNT(*) FROM tree_node"));
        Assert.Equal(Scalar(g, "SELECT COUNT(*) FROM influence_edges"), Scalar(c, "SELECT COUNT(*) FROM influence_edge"));
        Assert.Equal(Scalar(g, "SELECT COUNT(*) FROM influence_edges"), Scalar(c, "SELECT COUNT(*) FROM lineage_edge"));
        using var rep = JsonDocument.Parse(File.ReadAllText(Path.Combine(root, "data", "g1", "influence_report.json")));
        var r = rep.RootElement;
        Assert.Equal("identity_lineage", r.GetProperty("edge_semantics").GetString());
        Assert.Equal(fams, r.GetProperty("families").GetProperty("total").GetInt64());
        Assert.Equal(4, r.GetProperty("tree").GetProperty("guard").GetArrayLength());
        Assert.True(r.GetProperty("tree").GetProperty("longest_chains").GetArrayLength() > 0);
        Assert.True(r.GetProperty("strict_evidence_graph").GetProperty("nodes").GetInt32() == 120);
        Assert.Equal(Scalar(g, "SELECT COUNT(*) FROM influence_edges WHERE kind = 'tree'"), r.GetProperty("graph").GetProperty("tree_edges").GetInt64());
        Assert.True(r.GetProperty("lineage_params").GetProperty("StrongWeight").GetDouble() > 0);
    }

    [Fact]
    public void EvidenceModeIsTheUnchangedStrictGraph()
    {
        string root = NewRoot("evidence");
        string db = Path.Combine(root, "data", "musichistory.sqlite");
        string G(string name) => Path.Combine(root, "data", name, "music_graph.db");
        var log = new StringWriter();
        Fixture.Make(db, 100, 9, log);
        // The evidence path as it always was (Runner.Run) and the CLI's --mode evidence write the same bytes.
        Assert.Equal(0, Runner.Run(new RunOptions { Db = db, Graph = G("e1"), Root = root, GeneratedAt = Stamp }, new Params { Threads = 2, TargetFpr = 1e-3 }, log));
        Assert.Equal(0, Cli("run", "--mode", "evidence", "--db", db, "--graph", G("e2"), "--root", root, "--generated-at", Stamp, "--threads", "3",
            "--param", "TargetFpr=1e-3"));
        Assert.Equal(Hash(G("e1")), Hash(G("e2")));
        var meta = MiniPipeline.GraphMeta(G("e1"));
        Assert.False(meta.ContainsKey("edge_semantics"));
        Assert.False(meta.ContainsKey("influence_edge_semantics"));
        using (var g = new SqliteConnection($"Data Source={G("e1")};Mode=ReadOnly;Pooling=False"))
        {
            g.Open();
            Assert.Equal(0, Scalar(g, "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('identity_family', 'song_family')"));
        }
        // A lineage run in between leaves no trace in a later evidence run, and export follows the last run's semantics.
        Assert.Equal(0, Cli("run", "--db", db, "--graph", G("l1"), "--root", root, "--generated-at", Stamp, "--param", "TargetFpr=1e-3"));
        Assert.Equal("identity_lineage", MiniPipeline.GraphMeta(G("l1"))["edge_semantics"]);
        Assert.Equal(0, Cli("run", "--mode", "evidence", "--db", db, "--graph", G("e3"), "--root", root, "--generated-at", Stamp, "--param", "TargetFpr=1e-3"));
        Assert.Equal(Hash(G("e1")), Hash(G("e3")));
        Assert.Equal(0, Cli("export", "--db", db, "--graph", G("e4"), "--root", root, "--generated-at", Stamp));
        Assert.Equal(Hash(G("e1")), Hash(G("e4")));
        // An unknown mode is refused.
        Assert.Equal(1, Cli("run", "--mode", "loose", "--db", db, "--graph", G("x")));
    }
}
