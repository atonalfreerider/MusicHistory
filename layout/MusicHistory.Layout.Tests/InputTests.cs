using System.Text.RegularExpressions;

namespace MusicHistory.Layout.Tests;

/// <summary>Schema contract, demo generator and the §10 input validation (no GPU needed).</summary>
public class InputTests
{
    [Fact]
    public void SchemaMatchesDesignDoc()
    {
        string design = File.ReadAllText(Path.Combine(TestSupport.RepoRoot(), "docs", "DESIGN.md"));
        int sec = design.IndexOf("## 10.", StringComparison.Ordinal);
        Assert.True(sec >= 0, "DESIGN.md has no section 10");
        var block = Regex.Match(design[sec..], "```sql\\s*(.*?)```", RegexOptions.Singleline);
        Assert.True(block.Success, "no sql block in DESIGN.md §10");
        var fromDoc = Statements(block.Groups[1].Value).Where(s => s.StartsWith("create table", StringComparison.Ordinal)).ToList();
        var ours = Statements(GraphSchema.Graph + ";" + GraphSchema.LayoutTables).ToList();
        Assert.Equal(fromDoc, ours);
    }

    private static IEnumerable<string> Statements(string sql)
    {
        string noComments = Regex.Replace(sql, "--[^\n]*", "");
        foreach (string stmt in noComments.Split(';'))
        {
            string s = Regex.Replace(stmt, "\\s+", " ").Trim().ToLowerInvariant();
            s = Regex.Replace(s, "\\s*([(),])\\s*", "$1");
            if (s.Length > 0) yield return s;
        }
    }

    [Theory]
    [InlineData(0, "major", "C major")]
    [InlineData(1, "major", "Db major")]
    [InlineData(6, "major", "Gb major")]
    [InlineData(10, "major", "Bb major")]
    [InlineData(11, "major", "B major")]
    [InlineData(1, "minor", "C# minor")]
    [InlineData(3, "minor", "Eb minor")]
    [InlineData(6, "minor", "F# minor")]
    [InlineData(8, "minor", "G# minor")]
    [InlineData(9, "minor", "A minor")]
    public void KeyNamesFollowTheConvention(int tonic, string mode, string expected) =>
        Assert.Equal(expected, DemoGraph.KeyName(tonic, mode));

    [Fact]
    public void NormShiftLandsOnTheTargetKeyWithinRange()
    {
        for (int tonic = 0; tonic < 12; tonic++)
            foreach (bool minor in new[] { false, true })
            {
                int s = DemoGraph.NormShift(tonic, minor);
                Assert.InRange(s, -5, 6);
                Assert.Equal(minor ? 9 : 0, ((tonic + s) % 12 + 12) % 12);
            }
    }

    [Fact]
    public void DemoIsDeterministicAndPassesValidation()
    {
        using var dir = new TempDir();
        string a = TestSupport.Demo(dir, "a.db", 300);
        string b = TestSupport.Demo(dir, "b.db", 300);
        string[] tables = ["graph_meta", "nodes", "song_node", "influence_edges"];
        Assert.Equal(TestSupport.Dump(a, tables), TestSupport.Dump(b, tables));
        Assert.NotEqual(TestSupport.Dump(a, tables), TestSupport.Dump(TestSupport.Demo(dir, "c.db", 300, seed: 7), tables));

        var g = GraphInput.Load(a);
        Assert.Empty(g.Warnings);
        Assert.Equal(300, g.Count);
        Assert.True(g.Roots >= 1);
        Assert.Equal(g.Count - g.Roots, g.TreeEdges);
        foreach (var e in g.Edges)
        {
            Assert.True(e.Source < e.Target);
            Assert.True(g.Time[e.Source] < g.Time[e.Target]);
        }
        Assert.Equal("1", TestSupport.Scalar(a, "SELECT value FROM graph_meta WHERE key = 'synthetic'"));
        Assert.Equal("300", TestSupport.Scalar(a, "SELECT value FROM graph_meta WHERE key = 'song_count'"));
        Assert.Equal(0L, TestSupport.Scalar(a, "SELECT count(*) FROM nodes WHERE position_x IS NOT NULL"));
    }

    [Fact]
    public void DemoSongRowsArePlausible()
    {
        using var dir = new TempDir();
        string db = TestSupport.Demo(dir, "demo.db", 500);
        var rows = TestSupport.Rows(db, """
            SELECT node_id, title, artist, year, release_date, date_precision, time_value, tonic_pc, mode, key_name, norm_shift,
                   native_bpm, beats_per_bar, first_downbeat, midi_path, excerpt_start_beat, excerpt_end_beat, summary
            FROM song_node ORDER BY node_id
            """);
        foreach (var r in rows)
        {
            long id = (long)r[0]!;
            Assert.Equal($"Song {id:D4}", r[1]);
            Assert.StartsWith("Artist ", (string)r[2]!);
            long year = (long)r[3]!;
            Assert.InRange(year, 1940, 2025);
            string date = (string)r[4]!;
            long prec = (long)r[5]!;
            double t = (double)r[6]!;
            Assert.Equal(prec switch { 9 => 4, 10 => 7, _ => 10 }, date.Length);
            Assert.StartsWith(year.ToString("D4"), date);
            if (prec == 9) Assert.Equal(year + 0.5, t);
            if (prec == 10) Assert.Equal(year + (int.Parse(date[5..7]) - 0.5) / 12.0, t, 12);
            if (prec == 11)
                Assert.Equal(year + (DateTime.ParseExact(date, "yyyy-MM-dd", null).DayOfYear - 0.5) / 365.25, t, 12);
            Assert.Equal(DemoGraph.KeyName((int)(long)r[7]!, (string)r[8]!), r[9]);
            Assert.Equal(DemoGraph.NormShift((int)(long)r[7]!, (string)r[8]! == "minor"), (long)r[10]!);
            Assert.InRange((double)r[11]!, 60, 190);
            double bpb = (double)r[12]!, fd = (double)r[13]!, s = (double)r[15]!, e = (double)r[16]!;
            Assert.Contains(bpb, new[] { 3.0, 4.0, 6.0 });
            double bars = (e - s) / bpb;
            Assert.InRange(bars, 8, 24);
            Assert.Equal(Math.Round(bars), bars);
            Assert.Equal(0, ((s - fd) / bpb) % 1.0, 9);
            string midi = (string)r[14]!;
            Assert.StartsWith("../songs/R", midi);
            Assert.DoesNotContain('\\', midi);
            Assert.StartsWith("synthetic demo song", (string)r[17]!);
        }
    }

    [Fact]
    public void DemoSetsEntryAndExitKeysForSomeSongs()
    {
        // DESIGN.md §10 entry_* / exit_*: the key heard at the excerpt's start / end, NULL for the home key.
        using var dir = new TempDir();
        string db = TestSupport.Demo(dir, "keys.db", 500);
        var rows = TestSupport.Rows(db, """
            SELECT node_id, tonic_pc, mode, entry_tonic_pc, entry_mode, exit_tonic_pc, exit_mode FROM song_node ORDER BY node_id
            """);
        int entries = 0, exits = 0;
        foreach (var r in rows)
        {
            for (int k = 3; k <= 5; k += 2)
            {
                Assert.Equal(r[k] == null, r[k + 1] == null);   // tonic and mode are NULL together
                if (r[k] == null) continue;
                if (k == 3) entries++;
                else exits++;
                Assert.InRange((long)r[k]!, 0, 11);
                Assert.Contains((string)r[k + 1]!, new[] { "major", "minor" });
                Assert.False((long)r[k]! == (long)r[1]! && (string)r[k + 1]! == (string)r[2]!, $"node {r[0]}: a home key must be NULL");
            }
        }
        Assert.InRange(entries, 1, rows.Count / 4);
        Assert.InRange(exits, 1, rows.Count / 4);
        Assert.Contains(rows, r => r[3] == null && r[5] != null);   // leaves in another key only
        Assert.Contains(rows, r => r[3] != null && r[5] == null);   // enters in another key only
    }

    [Fact]
    public void DemoTreeIsUnchangedByTheKeyColumns()
    {
        // The entry/exit keys use their own RNG: seed 42 keeps the documented shape (1000 songs: 41 roots,
        // largest subtree 481, depth 10), so existing demo graphs and layouts stay comparable.
        using var dir = new TempDir();
        var s = DemoGraph.Write(dir.File("shape.db"), 1000, 42, TestSupport.FixedTime);
        Assert.Equal((41, 481, 10, 2114), (s.Roots, s.MaxDescendants, s.MaxDepth, s.Edges));
    }

    // ------------------------------------------------------------------ refusals

    private static string Mutated(TempDir dir, params string[] sql)
    {
        string db = TestSupport.Demo(dir, $"m{Guid.NewGuid():N}.db", 120);
        foreach (string s in sql) TestSupport.Exec(db, s);
        return db;
    }

    private static void Refused(string db, string expected, bool lenient = false)
    {
        var ex = Assert.Throws<InvalidGraphException>(() => GraphInput.Load(db, lenient));
        Assert.Contains(expected, ex.Message, StringComparison.OrdinalIgnoreCase);
        var (code, _, err) = TestSupport.Cli("validate", db);
        Assert.Equal(3, code);
        Assert.Contains(expected, err, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>A non-root node and its parent, from a demo graph.</summary>
    private static (long Node, long Parent) Child(string db, int skip = 0)
    {
        var r = TestSupport.Rows(db, $"SELECT node_id, tree_parent_node FROM song_node WHERE tree_parent_node IS NOT NULL ORDER BY node_id LIMIT 1 OFFSET {skip}")[0];
        return ((long)r[0]!, (long)r[1]!);
    }

    [Fact]
    public void RefusesNonContiguousIds()
    {
        using var dir = new TempDir();
        Refused(Mutated(dir, "DELETE FROM nodes WHERE id = 5"), "contiguous");
    }

    [Fact]
    public void RefusesMissingSongRow()
    {
        using var dir = new TempDir();
        Refused(Mutated(dir, "DELETE FROM song_node WHERE node_id = 7"), "no song_node row");
    }

    [Fact]
    public void RefusesIdsNotOrderedByTime()
    {
        using var dir = new TempDir();
        Refused(Mutated(dir, "UPDATE song_node SET time_value = 3000 WHERE node_id = 10"), "ordered by time_value");
    }

    [Fact]
    public void RefusesNonNumericTime()
    {
        using var dir = new TempDir();
        Refused(Mutated(dir, "UPDATE song_node SET time_value = 'soon' WHERE node_id = 3"), "not a finite number");
    }

    [Fact]
    public void RefusesMissingTreeEdge()
    {
        using var dir = new TempDir();
        string db = TestSupport.Demo(dir, "g.db", 120);
        var (node, _) = Child(db);
        TestSupport.Exec(db, $"UPDATE influence_edges SET kind = 'secondary' WHERE kind = 'tree' AND target_node = {node}");
        Refused(db, "exactly one tree edge");
    }

    [Fact]
    public void RefusesTreeEdgeFromWrongParent()
    {
        using var dir = new TempDir();
        string db = TestSupport.Demo(dir, "g.db", 120);
        // A child whose tree edge can be moved to another clearly earlier song without an edge to it.
        for (int k = 0; k < 50; k++)
        {
            var (node, parent) = Child(db, k);
            var other = TestSupport.Scalar(db, $"""
                SELECT s.node_id FROM song_node s, song_node t WHERE t.node_id = {node} AND s.time_value < t.time_value
                  AND s.node_id <> {parent}
                  AND NOT EXISTS (SELECT 1 FROM influence_edges e WHERE e.source_node = s.node_id AND e.target_node = {node})
                ORDER BY s.node_id LIMIT 1
                """);
            if (other is not long o) continue;
            TestSupport.Exec(db, $"UPDATE influence_edges SET source_node = {o} WHERE kind = 'tree' AND target_node = {node}");
            Refused(db, "not from tree_parent_node");
            return;
        }
        Assert.Fail("no suitable child in the demo graph");
    }

    [Fact]
    public void RefusesTreeEdgeIntoRoot()
    {
        using var dir = new TempDir();
        string db = TestSupport.Demo(dir, "g.db", 120);
        var (node, _) = Child(db, 3);
        TestSupport.Exec(db, $"UPDATE song_node SET tree_parent_node = NULL WHERE node_id = {node}");
        Refused(db, "is a root");
    }

    [Fact]
    public void RefusesEdgeBetweenContemporaries()
    {
        using var dir = new TempDir();
        string db = TestSupport.Demo(dir, "g.db", 300);
        // Two songs with the same time_value (year precision, same year): no order, so no edge.
        var pair = TestSupport.Rows(db, """
            SELECT a.node_id, b.node_id FROM song_node a JOIN song_node b ON a.time_value = b.time_value AND a.node_id < b.node_id
            WHERE NOT EXISTS (SELECT 1 FROM influence_edges e WHERE e.source_node = a.node_id AND e.target_node = b.node_id)
            LIMIT 1
            """)[0];
        TestSupport.Exec(db, $"""
            INSERT INTO influence_edges(source_node, target_node, kind, channels, primary_channel, score_bits, z, similarity, weight)
            VALUES ({pair[0]}, {pair[1]}, 'secondary', 'chord', 'chord', 20, 4, 0.3, 0.3)
            """);
        Refused(db, "source is not earlier");
    }

    [Fact]
    public void RefusesBadKindSimilarityAndWeight()
    {
        using var dir = new TempDir();
        Refused(Mutated(dir, "UPDATE influence_edges SET kind = 'cover' WHERE id = 3"), "is not 'tree' or 'secondary'");
        Refused(Mutated(dir, "UPDATE influence_edges SET similarity = 1.5 WHERE id = 3"), "is not in [0, 1]");
        Refused(Mutated(dir, "UPDATE influence_edges SET weight = -1 WHERE id = 3"), "weight");
    }

    [Fact]
    public void RefusesSelfParentAndMissingTables()
    {
        using var dir = new TempDir();
        string db = TestSupport.Demo(dir, "g.db", 120);
        var (node, _) = Child(db);
        TestSupport.Exec(db, $"UPDATE song_node SET tree_parent_node = {node} WHERE node_id = {node}");
        Refused(db, "its own tree parent");
        Refused(Mutated(dir, "DROP TABLE influence_edges"), "table influence_edges is missing");
        Refused(Mutated(dir, "DELETE FROM nodes", "DELETE FROM song_node", "DELETE FROM influence_edges"), "no nodes");
    }

    [Fact]
    public void DerivedTreeColumnsAreCheckedAndLenientRecomputes()
    {
        using var dir = new TempDir();
        string db = Mutated(dir, "UPDATE song_node SET descendants = descendants + 1 WHERE node_id = 1",
            "UPDATE song_node SET tree_depth = 9 WHERE node_id = 2");
        Refused(db, "descendants");
        var g = GraphInput.Load(db, lenient: true);
        Assert.Single(g.Warnings);
        Assert.Contains("2 derived tree value(s)", g.Warnings[0]);
        var clean = GraphInput.Load(TestSupport.Demo(dir, "clean.db", 120));
        Assert.Equal(clean.Descendants, g.Descendants);
        Assert.Equal(clean.Depth, g.Depth);
    }

    // ------------------------------------------------------------------ command line

    [Fact]
    public void CommandLineErrorsHaveExitCodes()
    {
        using var dir = new TempDir();
        Assert.Equal(2, TestSupport.Cli().Code);
        Assert.Equal(0, TestSupport.Cli("--help").Code);
        Assert.Equal(2, TestSupport.Cli(dir.File("missing.db")).Code);
        string db = TestSupport.Demo(dir, "g.db", 50);
        Assert.Equal(2, TestSupport.Cli(db, "--iterations", "many").Code);
        Assert.Equal(2, TestSupport.Cli(db, "--timeAxis=w").Code);
        Assert.Equal(2, TestSupport.Cli(db, "--damping", "1").Code);
        Assert.Equal(2, TestSupport.Cli(db, "--bogus", "1").Code);
        Assert.Equal(2, TestSupport.Cli(db, "--iterations").Code);
        Assert.Equal(2, TestSupport.Cli("demo", "--nodes", "10").Code);
        Assert.Equal(2, TestSupport.Cli(db, "--out", db).Code);
        Assert.Equal(0, TestSupport.Cli("validate", db).Code);
        // Nothing laid out yet: positions are NULL, so check fails with 6.
        Assert.Equal(6, TestSupport.Cli("check", db).Code);
    }

    [Fact]
    public void OptionsParseInBothSpellings()
    {
        var p = new LayoutParams();
        Assert.True(p.TryApply("--year-scale", () => "3.5"));
        Assert.True(p.TryApply("--TIMEAXIS", () => "z"));
        Assert.True(p.TryApply("--timeDirection", () => "down"));
        Assert.True(p.TryApply("--springWeight", () => "similarity"));
        Assert.True(p.TryApply("--pinLargestRoot", () => "false"));
        Assert.False(p.TryApply("--out", () => "x"));
        Assert.Equal(3.5, p.YearScale);
        Assert.Equal(TimeAxis.Z, p.Axis);
        Assert.Equal(TimeDirection.Down, p.Direction);
        Assert.Equal(SpringWeight.Similarity, p.SpringWeight);
        Assert.False(p.PinLargestRoot);
        var d = new LayoutParams();
        Assert.Equal(1500, d.Iterations);
        Assert.Equal(2.0, d.YearScale);
        Assert.Equal(TimeAxis.Y, d.Axis);
        Assert.Equal(TimeDirection.Up, d.Direction);
        Assert.Equal(0f, d.EdgeRepulsion);
        Assert.Equal(3.01f, d.Temperature(0), 5);
        Assert.Equal(0.01f, d.Temperature(d.Iterations), 6);
    }
}
