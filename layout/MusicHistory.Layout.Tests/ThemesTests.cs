using System.Globalization;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Data.Sqlite;
using Xunit.Abstractions;

namespace MusicHistory.Layout.Tests;

/// <summary>Small hand-made §12 themes graphs (placeholder titles; no text of any real song).</summary>
internal static class ThemesFixture
{
    public const string FixedTime = "2026-01-01T00:00:00Z";

    public static string Demo(TempDir dir, string name, int songs, int seed = 42)
    {
        string path = dir.File(name);
        ThemesDemo.Write(path, songs, seed, FixedTime);
        return path;
    }

    /// <summary>A §12 file with the given score vectors (one per song, ten scores each), anchors at <paramref name="radius"/>.</summary>
    public static string Write(string path, IReadOnlyList<double[]> scores, double radius = 40, string gender = "female")
    {
        using (var c = new SqliteConnection($"Data Source={path};Pooling=False"))
        {
            c.Open();
            Exec(c, "PRAGMA journal_mode=DELETE");
            Exec(c, ThemesSchema.Graph);
            using var tx = c.BeginTransaction();
            using (var cmd = c.CreateCommand())
            {
                cmd.Transaction = tx;
                cmd.CommandText = "INSERT INTO themes_meta(key, value) VALUES ('ring_radius', $r)";
                cmd.Parameters.AddWithValue("$r", radius.ToString("R", CultureInfo.InvariantCulture));
                cmd.ExecuteNonQuery();
            }
            foreach (var t in ThemesSchema.Themes)
            {
                using var cmd = c.CreateCommand();
                cmd.Transaction = tx;
                var (x, y, z) = ThemesSchema.AnchorPosition(t.Id, radius);
                cmd.CommandText = "INSERT INTO theme_anchor VALUES ($id, $label, $short, $angle, $x, $y, $z)";
                cmd.Parameters.AddWithValue("$id", t.Id);
                cmd.Parameters.AddWithValue("$label", t.Label);
                cmd.Parameters.AddWithValue("$short", t.Short);
                cmd.Parameters.AddWithValue("$angle", ThemesSchema.AngleDegrees(t.Id));
                cmd.Parameters.AddWithValue("$x", x);
                cmd.Parameters.AddWithValue("$y", y);
                cmd.Parameters.AddWithValue("$z", z);
                cmd.ExecuteNonQuery();
            }
            for (int i = 0; i < scores.Count; i++)
            {
                int top = Array.IndexOf(scores[i], scores[i].Max());
                using (var cmd = c.CreateCommand())
                {
                    cmd.Transaction = tx;
                    cmd.CommandText = """
                        INSERT INTO theme_song(node_id, work_id, title, artist, year, singer_gender, text_source, top_anchor, top_score)
                        VALUES ($id, $w, $t, 'Test Artist', 2000, $g, 'lyrics', $top, $ts)
                        """;
                    cmd.Parameters.AddWithValue("$id", i + 1);
                    cmd.Parameters.AddWithValue("$w", $"RTEST{i + 1:D7}");
                    cmd.Parameters.AddWithValue("$t", $"Test Song {i + 1}");
                    cmd.Parameters.AddWithValue("$g", gender);
                    cmd.Parameters.AddWithValue("$top", top + 1);
                    cmd.Parameters.AddWithValue("$ts", scores[i][top]);
                    cmd.ExecuteNonQuery();
                }
                for (int a = 0; a < ThemesSchema.AnchorCount; a++)
                {
                    using var cmd = c.CreateCommand();
                    cmd.Transaction = tx;
                    cmd.CommandText = "INSERT INTO theme_score(node_id, anchor_id, score) VALUES ($n, $a, $s)";
                    cmd.Parameters.AddWithValue("$n", i + 1);
                    cmd.Parameters.AddWithValue("$a", a + 1);
                    cmd.Parameters.AddWithValue("$s", scores[i][a]);
                    cmd.ExecuteNonQuery();
                }
            }
            tx.Commit();
        }
        SqliteConnection.ClearAllPools();
        return path;
    }

    /// <summary>A score vector with <paramref name="top"/> (1-based anchor) at <paramref name="value"/> and the rest spread evenly.</summary>
    public static double[] Peaked(int top, double value)
    {
        var s = Enumerable.Repeat((1 - value) / (ThemesSchema.AnchorCount - 1), ThemesSchema.AnchorCount).ToArray();
        s[top - 1] = value;
        return s;
    }

    /// <summary>The weighted barycentre recomputed independently of ThemesLayout: Σ_k s_k^γ A_k / Σ_k s_k^γ.</summary>
    public static (double X, double Z) Barycentre(double[] scores, double sharpen, double radius)
    {
        double sx = 0, sz = 0, sw = 0;
        for (int k = 0; k < scores.Length; k++)
        {
            double w = Math.Pow(scores[k], sharpen);
            double a = 36.0 * k * Math.PI / 180.0;
            sx += w * radius * Math.Cos(a);
            sz += w * radius * Math.Sin(a);
            sw += w;
        }
        return (sx / sw, sz / sw);
    }

    public static double[][] Scores(string db)
    {
        int n = Convert.ToInt32(TestSupport.Scalar(db, "SELECT count(*) FROM theme_song"), CultureInfo.InvariantCulture);
        var s = new double[n][];
        for (int i = 0; i < n; i++) s[i] = new double[ThemesSchema.AnchorCount];
        foreach (var r in TestSupport.Rows(db, "SELECT node_id, anchor_id, score FROM theme_score"))
            s[(long)r[0]! - 1][(long)r[1]! - 1] = (double)r[2]!;
        return s;
    }

    public static (double X, double Y, double Z)[] Positions(string db) =>
        [.. TestSupport.Rows(db, "SELECT position_x, position_y, position_z FROM theme_song ORDER BY node_id")
            .Select(r => ((double)r[0]!, (double)r[1]!, (double)r[2]!))];

    public static JsonObject LastRun(string db) =>
        JsonNode.Parse((string)TestSupport.Scalar(db, "SELECT params_json FROM themes_layout_run ORDER BY run_id DESC LIMIT 1")!)!.AsObject();

    private static void Exec(SqliteConnection c, string sql)
    {
        using var cmd = c.CreateCommand();
        cmd.CommandText = sql;
        cmd.ExecuteNonQuery();
    }
}

/// <summary>Themes schema contract, anchors, demo generator and input validation (no GPU needed).</summary>
public class ThemesInputTests
{
    [Fact]
    public void SchemaMatchesDesignDoc()
    {
        string design = File.ReadAllText(Path.Combine(TestSupport.RepoRoot(), "docs", "DESIGN.md"));
        int sec = design.IndexOf("## 12.", StringComparison.Ordinal);
        Assert.True(sec >= 0, "DESIGN.md has no section 12");
        var block = Regex.Match(design[sec..], "```sql\\s*(.*?)```", RegexOptions.Singleline);
        Assert.True(block.Success, "no sql block in DESIGN.md §12");
        var fromDoc = Statements(block.Groups[1].Value).Where(s => s.StartsWith("create table", StringComparison.Ordinal)).ToList();
        var ours = Statements(ThemesSchema.Graph + ";" + ThemesSchema.LayoutRun).ToList();
        Assert.Equal(5, ours.Count);
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
    [InlineData(40.0)]
    [InlineData(12.5)]
    public void AnchorsAreEquallySpacedOnTheRing(double radius)
    {
        var a = Enumerable.Range(1, 10).Select(id => ThemesSchema.AnchorPosition(id, radius)).ToArray();
        double chord = 2 * radius * Math.Sin(Math.PI / 10);
        static void Near(double expected, double actual) =>
            Assert.True(Math.Abs(expected - actual) < 1e-8, FormattableString.Invariant($"expected {expected}, got {actual}"));
        double sx = 0, sz = 0;
        for (int k = 0; k < 10; k++)
        {
            Assert.Equal(36.0 * k, ThemesSchema.AngleDegrees(k + 1), 12);
            Assert.Equal(0.0, a[k].Y);
            Near(radius, Math.Sqrt(a[k].X * a[k].X + a[k].Z * a[k].Z));
            Near(radius * Math.Cos(36.0 * k * Math.PI / 180), a[k].X);
            Near(radius * Math.Sin(36.0 * k * Math.PI / 180), a[k].Z);
            var b = a[(k + 1) % 10];
            Near(chord, Math.Sqrt((a[k].X - b.X) * (a[k].X - b.X) + (a[k].Z - b.Z) * (a[k].Z - b.Z)));
            sx += a[k].X;
            sz += a[k].Z;
        }
        Near(0.0, sx);
        Near(0.0, sz);
        Assert.Equal(radius, a[0].X);   // anchor 1 on +x
        Assert.Equal(0.0, a[0].Z);
    }

    [Fact]
    public void WeightsAreSharpenedAndNormalizedPerSong()
    {
        using var dir = new TempDir();
        double[][] scores = [ThemesFixture.Peaked(3, 1.0), [0.4, 0.2, 0.1, 0.1, 0.05, 0.05, 0.04, 0.03, 0.02, 0.01]];
        var g = ThemesInput.Load(ThemesFixture.Write(dir.File("w.db"), scores));
        foreach (double gamma in new[] { 1.0, 2.0, 3.0 })
        {
            var w = ThemesLayout.Weights(g, gamma);
            Assert.Equal(1.0, w[3 - 1], 12);   // all "I love you": all weight on anchor 3
            Assert.Equal(1.0, w.Skip(10).Take(10).Sum(), 12);
            double denom = scores[1].Sum(s => Math.Pow(s, gamma));
            for (int k = 0; k < 10; k++) Assert.Equal(Math.Pow(scores[1][k], gamma) / denom, w[10 + k], 12);
            var (bx, bz) = ThemesLayout.Barycentres(w, 2, 40);
            Assert.Equal(ThemesSchema.AnchorPosition(3, 40).X, bx[0], 9);
            Assert.Equal(ThemesSchema.AnchorPosition(3, 40).Z, bz[0], 9);
            var expect = ThemesFixture.Barycentre(scores[1], gamma, 40);
            Assert.Equal(expect.X, bx[1], 9);
            Assert.Equal(expect.Z, bz[1], 9);
        }
    }

    [Fact]
    public void DemoIsDeterministicAndFollowsTheContract()
    {
        using var dir = new TempDir();
        string a = ThemesFixture.Demo(dir, "a.db", 600);
        string b = ThemesFixture.Demo(dir, "b.db", 600);
        string[] tables = ["themes_meta", "theme_anchor", "theme_song", "theme_score"];
        Assert.Equal(TestSupport.Dump(a, tables), TestSupport.Dump(b, tables));
        Assert.Equal(File.ReadAllBytes(a), File.ReadAllBytes(b));
        Assert.NotEqual(TestSupport.Dump(a, tables), TestSupport.Dump(ThemesFixture.Demo(dir, "c.db", 600, seed: 7), tables));

        var g = ThemesInput.Load(a);
        Assert.Empty(g.Warnings);
        Assert.Equal(600, g.Count);
        Assert.Equal(0L, TestSupport.Scalar(a, "SELECT count(*) FROM theme_song WHERE position_x IS NOT NULL"));
        Assert.Equal(0L, TestSupport.Scalar(a, "SELECT count(*) FROM themes_layout_run"));
        Assert.Equal("1", TestSupport.Scalar(a, "SELECT value FROM themes_meta WHERE key = 'synthetic'"));
        Assert.Equal("600", TestSupport.Scalar(a, "SELECT value FROM themes_meta WHERE key = 'song_count'"));
        Assert.Equal("40", TestSupport.Scalar(a, "SELECT value FROM themes_meta WHERE key = 'ring_radius'"));
        var themes = JsonNode.Parse((string)TestSupport.Scalar(a, "SELECT value FROM themes_meta WHERE key = 'themes'")!)!.AsArray();
        Assert.Equal(ThemesSchema.Themes.Select(t => t.Label), themes.Select(t => t!.GetValue<string>()));
        Assert.Equal("delete", TestSupport.Scalar(a, "PRAGMA journal_mode"));

        var rows = TestSupport.Rows(a, """
            SELECT node_id, work_id, year, singer_gender, text_source, top_anchor, top_score, midi_path, excerpt_start_beat, excerpt_end_beat
            FROM theme_song ORDER BY node_id
            """);
        var scores = ThemesFixture.Scores(a);
        int title = 0, peaked = 0, other = 0, mixed = 0;
        var genders = new HashSet<string>();
        for (int i = 0; i < rows.Count; i++)
        {
            var r = rows[i];
            Assert.Equal((long)(i + 1), r[0]);
            if (i > 0)
            {
                var p = rows[i - 1];
                Assert.True((long)p[2]! < (long)r[2]! || ((long)p[2]! == (long)r[2]! && string.CompareOrdinal((string)p[1]!, (string)r[1]!) < 0),
                    "node ids follow (year, work_id)");
            }
            Assert.InRange((long)r[2]!, 1940, 2025);
            string gender = (string)r[3]!, source = (string)r[4]!;
            Assert.Contains(gender, ThemesSchema.SingerGenders);
            Assert.Contains(source, ThemesSchema.TextSources);
            genders.Add(gender);
            if (gender == "instrumental") Assert.Equal("title", source);
            if (source == "title") title++;
            double[] s = scores[i];
            Assert.All(s, x => Assert.InRange(x, 0.0, 1.0));
            Assert.Equal(1.0, s.Sum(), 9);
            int top = Array.IndexOf(s, s.Max());
            Assert.Equal((long)(top + 1), r[5]);
            Assert.Equal(s[top], (double)r[6]!);
            if (s[top] >= 0.9) peaked++;
            if (s[top] >= 0.8 && top == 9) other++;
            if (s[top] < 0.6 && s.OrderDescending().Skip(1).First() >= 0.15) mixed++;
            Assert.StartsWith("../songs/R", (string)r[7]!);
            Assert.True((double)r[9]! > (double)r[8]!);
        }
        Assert.InRange(title, 10, rows.Count / 5);          // a few title-only songs
        Assert.InRange(peaked, rows.Count / 6, rows.Count);  // peaked songs
        Assert.InRange(other, 20, rows.Count);               // 'Other' songs
        Assert.InRange(mixed, rows.Count / 6, rows.Count);   // mixed songs
        Assert.Superset(new HashSet<string> { "male", "female", "mixed" }, genders);
    }

    [Fact]
    public void DemoCanBorrowPlaybackFromAMusicGraph()
    {
        using var dir = new TempDir();
        string graph = dir.File(Path.Combine("graph", "music.db"));
        Directory.CreateDirectory(Path.GetDirectoryName(graph)!);
        DemoGraph.Write(graph, 40, 42, TestSupport.FixedTime);
        string outDb = dir.File(Path.Combine("elsewhere", "deeper", "themes.db"));
        var s = ThemesDemo.Write(outDb, 30, 42, ThemesFixture.FixedTime, fromGraph: graph);
        Assert.True(s.Borrowed);
        var src = TestSupport.Rows(graph, """
            SELECT year, midi_path, excerpt_start_beat, excerpt_end_beat, tonic_pc, mode, native_bpm, beats_per_bar, first_downbeat
            FROM song_node ORDER BY node_id LIMIT 30
            """);
        var dst = TestSupport.Rows(outDb, """
            SELECT year, midi_path, excerpt_start_beat, excerpt_end_beat, tonic_pc, mode, native_bpm, beats_per_bar, first_downbeat, title
            FROM theme_song
            """);
        foreach (var d in dst)
        {
            Assert.StartsWith("Song ", (string)d[9]!);   // titles stay synthetic
            string midi = (string)d[1]!;
            Assert.StartsWith("../../songs/R", midi);     // rebased to the output folder
            var match = src.Single(r => ((string)r[1]!)[3..] == midi[6..]);
            for (int c = 0; c < 9; c++)
                if (c != 1) Assert.Equal(match[c], d[c]);
        }
        Assert.Equal("music.db", TestSupport.Scalar(outDb, "SELECT value FROM themes_meta WHERE key = 'demo_playback_from'"));
        // Scores do not depend on --from (node order does: it follows the borrowed years).
        string plain = ThemesFixture.Demo(dir, "plain.db", 30);
        const string byWork = "SELECT s.work_id, c.anchor_id, c.score FROM theme_score c JOIN theme_song s ON s.node_id = c.node_id ORDER BY 1, 2";
        Assert.Equal(TestSupport.Rows(plain, byWork), TestSupport.Rows(outDb, byWork));
        Assert.Throws<UsageException>(() => ThemesDemo.Write(dir.File("x.db"), 41, 42, ThemesFixture.FixedTime, fromGraph: graph));
    }

    // ------------------------------------------------------------------ refusals

    private static void Refused(string db, string expected)
    {
        var ex = Assert.Throws<InvalidGraphException>(() => ThemesInput.Load(db));
        Assert.Contains(expected, ex.Message, StringComparison.OrdinalIgnoreCase);
        var (code, _, err) = TestSupport.Cli("themes", db, "--dry-run", "--iterations", "10");
        Assert.Equal(3, code);
        Assert.Contains(expected, err, StringComparison.OrdinalIgnoreCase);
    }

    private static string Mutated(TempDir dir, params string[] sql)
    {
        string db = ThemesFixture.Demo(dir, $"m{Guid.NewGuid():N}.db", 60);
        foreach (string s in sql) TestSupport.Exec(db, s);
        return db;
    }

    [Fact]
    public void RefusesBrokenThemesGraphs()
    {
        using var dir = new TempDir();
        Refused(Mutated(dir, "DELETE FROM theme_score WHERE node_id = 5 AND anchor_id = 7"), "song 5 has no theme_score row for anchor 7");
        Refused(Mutated(dir, "UPDATE theme_score SET score = -0.1 WHERE node_id = 3 AND anchor_id = 2"), "not a finite number >= 0");
        Refused(Mutated(dir, "UPDATE theme_score SET score = 'high' WHERE node_id = 3 AND anchor_id = 2"), "not a finite number >= 0");
        Refused(Mutated(dir, "UPDATE theme_score SET score = 0 WHERE node_id = 4"), "no positive score");
        Refused(Mutated(dir, "DELETE FROM theme_song WHERE node_id = 9", "DELETE FROM theme_score WHERE node_id = 9"), "contiguous");
        Refused(Mutated(dir, "INSERT INTO theme_score(node_id, anchor_id, score) VALUES (61, 1, 0.5)"), "unknown song or anchor");
        Refused(Mutated(dir, "DELETE FROM theme_anchor WHERE anchor_id = 10"), "anchor ids 1..10");
        Refused(Mutated(dir, "DROP TABLE theme_score"), "table theme_score is missing");
        Refused(Mutated(dir, "DELETE FROM theme_song", "DELETE FROM theme_score"), "no songs");
    }

    [Fact]
    public void ScoresNotSummingToOneAreNormalizedWithAWarning()
    {
        using var dir = new TempDir();
        string db = Mutated(dir, "UPDATE theme_score SET score = score * 3 WHERE node_id IN (2, 4)");
        var g = ThemesInput.Load(db);
        Assert.Single(g.Warnings);
        Assert.Contains("2 song(s)", g.Warnings[0]);
        for (int i = 0; i < g.Count; i++)
            Assert.Equal(1.0, Enumerable.Range(0, 10).Sum(a => g.ScoreOf(i, a)), 12);
    }

    [Fact]
    public void CommandLineErrorsHaveExitCodes()
    {
        using var dir = new TempDir();
        string db = ThemesFixture.Demo(dir, "g.db", 20);
        Assert.Equal(0, TestSupport.Cli("themes", "--help").Code);
        Assert.Contains("--sharpen", TestSupport.Cli("themes", "--help").Out);
        Assert.Contains("themes-demo", TestSupport.Cli("--help").Out);
        Assert.Equal(2, TestSupport.Cli("themes").Code);
        Assert.Equal(2, TestSupport.Cli("themes", dir.File("missing.db")).Code);
        Assert.Equal(2, TestSupport.Cli("themes", db, "--bogus", "1").Code);
        Assert.Equal(2, TestSupport.Cli("themes", db, "--sharpen", "0").Code);
        Assert.Equal(2, TestSupport.Cli("themes", db, "--radius=-3").Code);
        Assert.Equal(2, TestSupport.Cli("themes", db, "--repulsion", "lots").Code);
        Assert.Equal(2, TestSupport.Cli("themes", db, "--damping", "1").Code);
        Assert.Equal(2, TestSupport.Cli("themes", db, "--iterations").Code);
        Assert.Equal(2, TestSupport.Cli("themes", db, "--out", db).Code);
        Assert.Equal(2, TestSupport.Cli("themes", db, db).Code);
        Assert.Equal(2, TestSupport.Cli("themes-demo", "--songs", "10").Code);
        Assert.Equal(2, TestSupport.Cli("themes-demo", "--out", dir.File("d.db"), "--songs", "0").Code);
        Assert.Equal(2, TestSupport.Cli("themes-demo", "--out", dir.File("d.db"), "--from", dir.File("nope.db")).Code);
        var p = new ThemesParams();
        Assert.True(p.TryApply("--start-temperature", () => "1.5"));
        Assert.True(p.TryApply("--SHARPEN", () => "3"));
        Assert.False(p.TryApply("--yearScale", () => "2"));
        Assert.Equal(1.5f, p.StartTemperature);
        Assert.Equal(3.0, p.Sharpen);
        var d = new ThemesParams();
        Assert.Equal(1500, d.Iterations);
        Assert.Null(d.Radius);
        Assert.Equal(2.0, d.Sharpen);
        Assert.Equal(0f, d.Slab);
    }
}

/// <summary>GPU runs of the themes layout (the default DirectX 12 device; WARP where noted). Sequential: one device.</summary>
[Collection("gpu")]
public class ThemesLayoutTests(ITestOutputHelper output)
{
    [Theory]
    [InlineData(1.0)]
    [InlineData(2.0)]
    [InlineData(3.5)]
    public void WithoutRepulsionEverySongConvergesToItsWeightedBarycentre(double sharpen)
    {
        using var dir = new TempDir();
        string db = ThemesFixture.Demo(dir, "g.db", 400);
        var g = ThemesInput.Load(db);
        var r = ThemesLayout.Run(g, new ThemesParams { Repulsion = 0, Sharpen = sharpen, Jitter = 2f });
        var scores = ThemesFixture.Scores(db);
        double worst = 0;
        for (int i = 0; i < g.Count; i++)
        {
            var (bx, bz) = ThemesFixture.Barycentre(scores[i], sharpen, 40);
            worst = Math.Max(worst, Math.Sqrt((r.X[i] - bx) * (r.X[i] - bx) + (r.Z[i] - bz) * (r.Z[i] - bz)));
            Assert.Equal(0f, r.Y[i]);
        }
        output.WriteLine(FormattableString.Invariant($"sharpen {sharpen}: max |p - b| = {worst:E2}, final mean move {r.FinalMeanMove:E2}, residual max {r.ResidualMax:E2}"));
        Assert.True(worst < 1e-4, $"max distance from the barycentre {worst}");
        Assert.True(r.ResidualMax < 1e-4, $"residual {r.ResidualMax}");
    }

    [Fact]
    public void TwoRunsAreBitIdentical()
    {
        using var dir = new TempDir();
        string src = ThemesFixture.Demo(dir, "src.db", 500);
        var r1 = TestSupport.Cli("themes", src, "--out", dir.File("a.db"), "--quiet");
        var r2 = TestSupport.Cli("themes", src, "--out", dir.File("b.db"), "--quiet");
        var r3 = TestSupport.Cli("themes", src, "--out", dir.File("c.db"), "--quiet", "--seed", "7");
        Assert.True(r1.Code == 0, r1.Err);
        Assert.True(r2.Code == 0, r2.Err);
        Assert.True(r3.Code == 0, r3.Err);
        string[] tables = ["theme_anchor", "theme_song", "theme_score"];
        Assert.Equal(TestSupport.Dump(dir.File("a.db"), tables), TestSupport.Dump(dir.File("b.db"), tables));
        string ha = ThemesFixture.LastRun(dir.File("a.db"))["stats"]!["positions_sha256"]!.GetValue<string>();
        Assert.Equal(ha, ThemesFixture.LastRun(dir.File("b.db"))["stats"]!["positions_sha256"]!.GetValue<string>());
        Assert.NotEqual(ha, ThemesFixture.LastRun(dir.File("c.db"))["stats"]!["positions_sha256"]!.GetValue<string>());
        // The source was not touched by --out runs.
        Assert.Equal(0L, TestSupport.Scalar(src, "SELECT count(*) FROM theme_song WHERE position_x IS NOT NULL"));

        // Batching does not change the bits.
        var g = ThemesInput.Load(src);
        var one = ThemesLayout.Run(g, new ThemesParams { Iterations = 150, Batch = 1 });
        var many = ThemesLayout.Run(g, new ThemesParams { Iterations = 150, Batch = 64 });
        Assert.Equal(one.X, many.X);
        Assert.Equal(one.Z, many.Z);
    }

    [Fact]
    public void DefaultLayoutFormsCloudsCloseToTheBarycentres()
    {
        using var dir = new TempDir();
        string db = ThemesFixture.Demo(dir, "g.db", 1012);
        var (code, _, err) = TestSupport.Cli("themes", db);
        Assert.True(code == 0, err);
        output.WriteLine(err);
        var s = ThemesFixture.LastRun(db)["stats"]!.AsObject();
        double R = 40;
        Assert.Equal(0, s["non_finite_positions"]!.GetValue<int>());
        Assert.Equal(0.0, s["max_abs_y"]!.GetValue<double>());
        Assert.True(s["mean_displacement"]!.GetValue<double>() < 0.05 * R, "mean displacement from the barycentre stays small");
        Assert.True(s["max_displacement"]!.GetValue<double>() < 0.2 * R, "nobody is pushed across the ring");
        Assert.True(s["crossed_sector"]!.GetValue<int>() <= s["crossed_sector_eligible"]!.GetValue<int>() / 100);
        Assert.True(s["nn_min"]!.GetValue<double>() > 0.3, "songs that share a barycentre spread into a cloud");
        Assert.True(s["max_radius"]!.GetValue<double>() < 1.15 * R);
        Assert.True(s["peaked_mean_distance_to_anchor"]!.GetValue<double>() < 0.1 * R);
        Assert.True(s["final_mean_move"]!.GetValue<double>() < 1e-3);
        Assert.True(s["residual_max"]!.GetValue<double>() < 1e-2);

        // Every song is written, y = 0 (the plane), nothing non-finite.
        var pos = ThemesFixture.Positions(db);
        Assert.Equal(1012, pos.Length);
        Assert.All(pos, p => Assert.True(double.IsFinite(p.X) && double.IsFinite(p.Z) && p.Y == 0));

        // The most peaked song sits in its anchor's cloud.
        var best = TestSupport.Rows(db, "SELECT node_id, top_anchor, top_score FROM theme_song ORDER BY top_score DESC, node_id LIMIT 1")[0];
        var p0 = pos[(long)best[0]! - 1];
        var a = ThemesSchema.AnchorPosition((int)(long)best[1]!, R);
        double dist = Math.Sqrt((p0.X - a.X) * (p0.X - a.X) + (p0.Z - a.Z) * (p0.Z - a.Z));
        output.WriteLine(FormattableString.Invariant($"most peaked song ({best[2]}) is {dist:F3} from anchor {best[1]}"));
        Assert.True(dist < 0.15 * R, $"{dist}");
    }

    [Fact]
    public void AnAllOneThemeSongSitsOnItsAnchor()
    {
        // "The Man I Love" case: a song that is all "I love you" ends at that anchor, even among
        // mixed songs that repel it; a crowd of identical songs forms a cloud centred on the anchor.
        using var dir = new TempDir();
        var rng = new Random(5);
        var scores = new List<double[]> { ThemesFixture.Peaked(3, 1.0) };
        for (int i = 0; i < 150; i++)
        {
            var s = Enumerable.Range(0, 10).Select(_ => rng.NextDouble()).ToArray();
            double sum = s.Sum();
            scores.Add([.. s.Select(x => x / sum)]);
        }
        for (int i = 0; i < 60; i++) scores.Add(ThemesFixture.Peaked(10, 1.0));
        string db = ThemesFixture.Write(dir.File("love.db"), scores);
        Assert.Equal(0, TestSupport.Cli("themes", db, "--quiet").Code);
        var pos = ThemesFixture.Positions(db);
        var love = ThemesSchema.AnchorPosition(3, 40);
        double d = Math.Sqrt((pos[0].X - love.X) * (pos[0].X - love.X) + (pos[0].Z - love.Z) * (pos[0].Z - love.Z));
        output.WriteLine(FormattableString.Invariant($"all-'I love you' song: {d:E2} from its anchor"));
        Assert.True(d < 0.1, $"{d}");

        // The 60 identical 'Other' songs: a cloud around anchor 10, spread (no two coincide), not beyond a few units.
        var other = ThemesSchema.AnchorPosition(10, 40);
        var cloud = pos.Skip(151).ToArray();
        double cx = cloud.Average(p => p.X), cz = cloud.Average(p => p.Z);
        Assert.True(Math.Sqrt((cx - other.X) * (cx - other.X) + (cz - other.Z) * (cz - other.Z)) < 0.3);
        Assert.All(cloud, p => Assert.True(Math.Sqrt((p.X - other.X) * (p.X - other.X) + (p.Z - other.Z) * (p.Z - other.Z)) < 5));
        double minPair = double.PositiveInfinity;
        for (int i = 0; i < cloud.Length; i++)
            for (int j = i + 1; j < cloud.Length; j++)
                minPair = Math.Min(minPair, Math.Sqrt((cloud[i].X - cloud[j].X) * (cloud[i].X - cloud[j].X) + (cloud[i].Z - cloud[j].Z) * (cloud[i].Z - cloud[j].Z)));
        Assert.True(minPair > 0.3, $"{minPair}");
    }

    [Fact]
    public void OutputTablesFollowTheContract()
    {
        using var dir = new TempDir();
        string db = ThemesFixture.Demo(dir, "g.db", 200);
        // A foreign themes_layout_run is replaced (this command owns it).
        TestSupport.Exec(db, "DROP TABLE themes_layout_run");
        TestSupport.Exec(db, "CREATE TABLE themes_layout_run(run_id INTEGER PRIMARY KEY, note TEXT)");
        Assert.Equal(0, TestSupport.Cli("themes", db, "--quiet", "--iterations", "300").Code);
        Assert.Equal(0, TestSupport.Cli("themes", db, "--quiet", "--iterations", "300", "--radius", "25", "--sharpen", "3").Code);
        Assert.Equal(2L, TestSupport.Scalar(db, "SELECT count(*) FROM themes_layout_run"));
        var cols = TestSupport.Rows(db, "PRAGMA table_info(themes_layout_run)").Select(r => (string)r[1]!).ToArray();
        Assert.Equal(ThemesSchema.LayoutRunColumns, cols);

        var run = TestSupport.Rows(db, """
            SELECT created_at, device, iterations, sharpen, repulsion, final_mean_move, params_json
            FROM themes_layout_run ORDER BY run_id DESC LIMIT 1
            """)[0];
        Assert.Matches(@"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", (string)run[0]!);
        Assert.False(string.IsNullOrEmpty((string)run[1]!));
        Assert.Equal(300L, run[2]);
        Assert.Equal(3.0, run[3]);
        Assert.Equal(0.35, run[4]);
        Assert.InRange((double)run[5]!, 0, 1e-2);
        var pj = JsonNode.Parse((string)run[6]!)!;
        Assert.Equal(25.0, pj["radius"]!.GetValue<double>());
        Assert.Equal(200, pj["stats"]!["songs"]!.GetValue<int>());
        Assert.Equal(0, pj["stats"]!["non_finite_positions"]!.GetValue<int>());

        // Anchors and the meta follow the radius of the last run.
        Assert.Equal("25", TestSupport.Scalar(db, "SELECT value FROM themes_meta WHERE key = 'ring_radius'"));
        foreach (var r in TestSupport.Rows(db, "SELECT anchor_id, angle, position_x, position_y, position_z FROM theme_anchor ORDER BY anchor_id"))
        {
            var (x, y, z) = ThemesSchema.AnchorPosition((int)(long)r[0]!, 25);
            Assert.Equal(36.0 * ((long)r[0]! - 1), (double)r[1]!, 12);
            Assert.Equal(x, (double)r[2]!);
            Assert.Equal(y, (double)r[3]!);
            Assert.Equal(z, (double)r[4]!);
        }
        Assert.All(ThemesFixture.Positions(db), p => Assert.True(Math.Sqrt(p.X * p.X + p.Z * p.Z) < 25 * 1.2));

        Assert.Equal("delete", TestSupport.Scalar(db, "PRAGMA journal_mode"));
        byte[] header = new byte[100];
        using (var f = File.OpenRead(db)) f.ReadExactly(header);
        Assert.Equal(1, header[18]);
        Assert.Equal(1, header[19]);
        Assert.False(File.Exists(db + "-journal"));
        Assert.False(File.Exists(db + "-wal"));

        // --dry-run and --json write nothing but report the stats.
        var (code, stdout, _) = TestSupport.Cli("themes", db, "--dry-run", "--json", "--quiet", "--iterations", "100");
        Assert.Equal(0, code);
        Assert.Equal(2L, TestSupport.Scalar(db, "SELECT count(*) FROM themes_layout_run"));
        Assert.Equal(200, JsonNode.Parse(stdout)!["stats"]!["songs"]!.GetValue<int>());
    }

    [Fact]
    public void SlabModeKeepsSongsInAThinSlab()
    {
        using var dir = new TempDir();
        string db = ThemesFixture.Demo(dir, "g.db", 300);
        Assert.Equal(0, TestSupport.Cli("themes", db, "--quiet", "--slab", "3", "--flatten", "1").Code);
        var pos = ThemesFixture.Positions(db);
        Assert.All(pos, p => Assert.InRange(p.Y, -1.5, 1.5));
        Assert.Contains(pos, p => p.Y != 0);
        Assert.All(pos, p => Assert.True(double.IsFinite(p.X) && double.IsFinite(p.Y) && double.IsFinite(p.Z)));
        Assert.Equal(0.0, TestSupport.Scalar(db, "SELECT min(position_y) FROM theme_anchor"));
        Assert.Equal(0.0, TestSupport.Scalar(db, "SELECT max(position_y) FROM theme_anchor"));
    }

    [Fact]
    public void EdgeCasesRun()
    {
        using var dir = new TempDir();
        // One song; many identical score vectors (all 'Other'); a flat vector (sits at the centre);
        // the repulsion cutoff; a large sharpen exponent.
        string one = ThemesFixture.Write(dir.File("one.db"), [ThemesFixture.Peaked(7, 0.95)]);
        Assert.Equal(0, TestSupport.Cli("themes", one, "--quiet", "--iterations", "300").Code);
        var p = ThemesFixture.Positions(one)[0];
        var (bx, bz) = ThemesFixture.Barycentre(ThemesFixture.Peaked(7, 0.95), 2, 40);
        Assert.True(Math.Abs(p.X - bx) < 1e-4 && Math.Abs(p.Z - bz) < 1e-4);

        string same = ThemesFixture.Write(dir.File("same.db"),
            [.. Enumerable.Repeat(ThemesFixture.Peaked(10, 1.0), 120), Enumerable.Repeat(0.1, 10).ToArray()]);
        foreach (string[] extra in new[] { Array.Empty<string>(), ["--cutoff", "3"], ["--sharpen", "12"] })
        {
            var (code, _, err) = TestSupport.Cli(["themes", same, "--quiet", "--iterations", "400", .. extra]);
            Assert.True(code == 0, err);
            var s = ThemesFixture.LastRun(same)["stats"]!;
            Assert.Equal(0, s["non_finite_positions"]!.GetValue<int>());
            Assert.True(s["nn_min"]!.GetValue<double>() > 0.2, string.Join(" ", extra));
            var flat = ThemesFixture.Positions(same)[^1];
            Assert.True(Math.Sqrt(flat.X * flat.X + flat.Z * flat.Z) < 1.0, "a flat song sits near the centre");
        }
    }

    [Fact]
    public void WarpDeviceRuns()
    {
        using var dir = new TempDir();
        string db = ThemesFixture.Demo(dir, "g.db", 80);
        var (code, _, err) = TestSupport.Cli("themes", db, "--device", "warp", "--iterations", "100", "--quiet");
        Assert.True(code == 0, err);
        Assert.Contains("(software)", (string)TestSupport.Scalar(db, "SELECT device FROM themes_layout_run")!);
    }

    [Fact]
    public void ThemesDemoCommandWritesAndLaysOut()
    {
        using var dir = new TempDir();
        string db = dir.File("demo.db");
        var (code, _, err) = TestSupport.Cli("themes-demo", "--out", db, "--songs", "150", "--iterations", "200", "--generated-at", ThemesFixture.FixedTime);
        Assert.True(code == 0, err);
        Assert.Equal(150L, TestSupport.Scalar(db, "SELECT count(*) FROM theme_song WHERE position_x IS NOT NULL"));
        Assert.Equal(1L, TestSupport.Scalar(db, "SELECT count(*) FROM themes_layout_run"));
        Assert.Equal(ThemesFixture.FixedTime, TestSupport.Scalar(db, "SELECT value FROM themes_meta WHERE key = 'generated_at'"));
        string nl = dir.File("nolayout.db");
        Assert.Equal(0, TestSupport.Cli("themes-demo", "--out", nl, "--songs", "50", "--no-layout", "--quiet").Code);
        Assert.Equal(0L, TestSupport.Scalar(nl, "SELECT count(*) FROM theme_song WHERE position_x IS NOT NULL"));
    }

    /// <summary>Reads the result with the sqlite3.dll 3.15.0 that Unity ships, as the viewer's loader would.</summary>
    [Fact]
    public void Sqlite315CanReadTheThemesGraph()
    {
        string root = TestSupport.RepoRoot();
        string? dll = new[]
        {
            Environment.GetEnvironmentVariable("MUSICHISTORY_SQLITE315") ?? "",
            Path.Combine(Path.GetDirectoryName(root)!, "MusicHistory-Viewer", "Assets", "Plugins", "x86_64", "sqlite3.dll"),
            Path.Combine(root, "unity", "Assets", "Plugins", "x86_64", "sqlite3.dll"),
            Path.Combine(Path.GetDirectoryName(root)!, "Unity-FDG", "Assets", "Plugins", "x86_64", "sqlite3.dll"),
        }.FirstOrDefault(File.Exists);
        if (dll == null)
        {
            output.WriteLine("SKIPPED: no sqlite3.dll 3.15 found (set MUSICHISTORY_SQLITE315)");
            return;
        }
        using var dir = new TempDir();
        string db = ThemesFixture.Demo(dir, "g.db", 250);
        Assert.Equal(0, TestSupport.Cli("themes", db, "--quiet", "--iterations", "300").Code);
        using var old = new LayoutTests.NativeSqlite(dll);
        Assert.Equal("3.15.0", old.Version);
        Assert.Equal("ok", old.Query(db, "PRAGMA integrity_check")[0][0]);
        Assert.Equal("250", old.Query(db, "SELECT count(*) FROM theme_song WHERE position_x IS NOT NULL AND position_y = 0 AND position_z IS NOT NULL")[0][0]);
        Assert.Equal("10", old.Query(db, "SELECT count(*) FROM theme_anchor")[0][0]);
        Assert.Equal("250", old.Query(db, """
            SELECT count(*) FROM (SELECT s.node_id, sum(c.score) AS total, max(c.score) AS top FROM theme_song s
              JOIN theme_score c ON c.node_id = s.node_id JOIN theme_anchor a ON a.anchor_id = c.anchor_id
              GROUP BY s.node_id) WHERE abs(total - 1) < 1e-6
            """)[0][0]);
        var run = old.Query(db, "SELECT iterations, sharpen FROM themes_layout_run ORDER BY run_id DESC LIMIT 1")[0];
        Assert.Equal(new[] { "300", "2.0" }, run.Select(x => x ?? "NULL").ToArray());
    }
}
