using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;
using Xunit.Abstractions;

namespace MusicHistory.Layout.Tests;

/// <summary>GPU runs (the default DirectX 12 device; WARP where noted). Sequential: one device.</summary>
[Collection("gpu")]
public class LayoutTests(ITestOutputHelper output)
{
    private static JsonObject Check(string db)
    {
        var (code, stdout, err) = TestSupport.Cli("check", db, "--json");
        Assert.True(code == 0, $"check exit {code}: {err}");
        return JsonNode.Parse(stdout)!.AsObject();
    }

    [Fact]
    public void TwoRunsAreIdenticalSettledAndExactInTime()
    {
        using var dir = new TempDir();
        string src = TestSupport.Demo(dir, "src.db", 600);
        var r1 = TestSupport.Cli("run", src, "--out", dir.File("a.db"), "--quiet");
        var r2 = TestSupport.Cli(src, "--out", dir.File("b.db"), "--quiet");
        Assert.True(r1.Code == 0, r1.Err);
        Assert.True(r2.Code == 0, r2.Err);
        var a = Check(dir.File("a.db"));
        var b = Check(dir.File("b.db"));
        output.WriteLine(a.ToJsonString());
        Assert.Equal(a["positions_sha256"]!.GetValue<string>(), b["positions_sha256"]!.GetValue<string>());
        Assert.Equal(TestSupport.Dump(dir.File("a.db"), "nodes", "node_layout_metadata"),
            TestSupport.Dump(dir.File("b.db"), "nodes", "node_layout_metadata"));
        Assert.Equal(0, a["non_finite_positions"]!.GetValue<int>());
        Assert.Equal(0.0, a["max_time_axis_error"]!.GetValue<double>());
        Assert.Equal(0.0, a["max_metadata_time_axis_error"]!.GetValue<double>());
        Assert.True(a["final_mean_move"]!.GetValue<double>() < 0.01);
        double tree = a["mean_free_distance_tree_edges"]!.GetValue<double>();
        double random = a["mean_free_distance_random_pairs"]!.GetValue<double>();
        double matched = a["mean_free_distance_parent_to_random_contemporary_of_child"]!.GetValue<double>();
        Assert.True(tree < 0.5 * random, $"tree {tree} vs random {random}");
        Assert.True(tree < 0.5 * matched, $"tree {tree} vs time-matched random {matched}");
        // The source file was not touched by --out runs.
        Assert.Equal(0L, TestSupport.Scalar(src, "SELECT count(*) FROM nodes WHERE position_x IS NOT NULL"));
    }

    [Fact]
    public void BatchingDoesNotChangeTheResult()
    {
        using var dir = new TempDir();
        var g = GraphInput.Load(TestSupport.Demo(dir, "g.db", 300));
        var one = TemporalGraph.Run(g, new LayoutParams { Iterations = 200, Batch = 1 });
        var many = TemporalGraph.Run(g, new LayoutParams { Iterations = 200, Batch = 64 });
        Assert.Equal(one.U, many.U);
        Assert.Equal(one.V, many.V);
        Assert.Equal(one.FinalMeanMove, many.FinalMeanMove);
    }

    [Fact]
    public void TimeAxisAndDirectionOnlyRelabelCoordinates()
    {
        using var dir = new TempDir();
        string src = TestSupport.Demo(dir, "src.db", 250);
        Assert.Equal(0, TestSupport.Cli(src, "--out", dir.File("y.db"), "--quiet", "--iterations", "300").Code);
        Assert.Equal(0, TestSupport.Cli(src, "--out", dir.File("z.db"), "--quiet", "--iterations", "300", "--timeAxis", "z",
            "--timeDirection", "down", "--yearScale", "3").Code);
        var y = TestSupport.Rows(dir.File("y.db"), "SELECT position_x, position_y, position_z FROM nodes ORDER BY id");
        var z = TestSupport.Rows(dir.File("z.db"), "SELECT position_x, position_y, position_z FROM nodes ORDER BY id");
        var t = TestSupport.Rows(src, "SELECT time_value FROM song_node ORDER BY node_id").Select(r => (double)r[0]!).ToArray();
        double min = t.Min();
        for (int i = 0; i < y.Count; i++)
        {
            Assert.Equal((t[i] - min) * 2.0, (double)y[i][1]!);
            Assert.Equal(-(t[i] - min) * 3.0, (double)z[i][2]!);
        }
        Assert.True(y.Max(r => (double)r[1]!) > 100);                 // oldest at 0, newest at the top
        Assert.Equal("z", TestSupport.Scalar(dir.File("z.db"), "SELECT time_axis FROM layout_run"));
        Assert.Equal("down", TestSupport.Scalar(dir.File("z.db"), "SELECT time_direction FROM layout_run"));
        Assert.Equal(0.0, Check(dir.File("z.db"))["max_time_axis_error"]!.GetValue<double>());
    }

    [Fact]
    public void OutputTablesFollowTheContract()
    {
        using var dir = new TempDir();
        string db = TestSupport.Demo(dir, "g.db", 200);
        // A GPU-FDG era node_layout_metadata is replaced, not appended to.
        TestSupport.Exec(db, "CREATE TABLE node_layout_metadata(node_id INTEGER PRIMARY KEY, inertia REAL, follower_count INTEGER, is_anchor INTEGER)");
        Assert.Equal(0, TestSupport.Cli(db, "--quiet", "--iterations", "200").Code);
        Assert.Equal(0, TestSupport.Cli(db, "--quiet", "--iterations", "200").Code);
        Assert.Equal(200L, TestSupport.Scalar(db, "SELECT count(*) FROM node_layout_metadata"));
        Assert.Equal(2L, TestSupport.Scalar(db, "SELECT count(*) FROM layout_run"));
        var cols = TestSupport.Rows(db, "PRAGMA table_info(node_layout_metadata)").Select(r => (string)r[1]!).ToArray();
        Assert.Equal(GraphSchema.NodeLayoutMetadataColumns, cols);
        cols = TestSupport.Rows(db, "PRAGMA table_info(layout_run)").Select(r => (string)r[1]!).ToArray();
        Assert.Equal(GraphSchema.LayoutRunColumns, cols);

        var run = TestSupport.Rows(db, """
            SELECT device, iterations, loop_ms, final_mean_move, time_axis, time_direction, year_scale, min_time, params_json, created_at
            FROM layout_run ORDER BY run_id DESC LIMIT 1
            """)[0];
        Assert.False(string.IsNullOrEmpty((string)run[0]!));
        Assert.Equal(200L, run[1]);
        Assert.True((double)run[2]! > 0);
        Assert.Equal("y", run[4]);
        Assert.Equal("up", run[5]);
        Assert.Equal(2.0, run[6]);
        Assert.Equal(TestSupport.Scalar(db, "SELECT min(time_value) FROM song_node"), run[7]);
        var pj = JsonNode.Parse((string)run[8]!)!;
        Assert.Equal(200, pj["iterations"]!.GetValue<int>());
        Assert.Equal(200, pj["stats"]!["nodes"]!.GetValue<int>());
        Assert.Matches(@"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", (string)run[9]!);

        // Metadata: roots flagged, mass and radius from descendants, depth copied.
        foreach (var r in TestSupport.Rows(db, """
                     SELECT m.mass, m.is_root, m.tree_depth, m.display_radius, s.descendants, s.tree_parent_node, s.tree_depth
                     FROM node_layout_metadata m JOIN song_node s ON s.node_id = m.node_id
                     """))
        {
            long desc = (long)r[4]!;
            Assert.Equal(1 + Math.Log2(1 + desc), (double)r[0]!, 12);
            Assert.Equal(r[5] == null ? 1L : 0L, r[1]);
            Assert.Equal(r[6], r[2]);
            Assert.Equal(0.2 * Math.Sqrt(1 + desc), (double)r[3]!, 12);
        }

        // Journal mode DELETE and a legacy (non-WAL) file header: bytes 18/19 are 1.
        Assert.Equal("delete", TestSupport.Scalar(db, "PRAGMA journal_mode"));
        byte[] header = new byte[100];
        using (var f = File.OpenRead(db)) f.ReadExactly(header);
        Assert.Equal(1, header[18]);
        Assert.Equal(1, header[19]);
        Assert.False(File.Exists(db + "-journal"));
        Assert.False(File.Exists(db + "-wal"));
    }

    [Fact]
    public void EdgeCasesRun()
    {
        using var dir = new TempDir();
        // Only roots (no edges), a single song, and the optional node-edge clearance force.
        foreach (var (name, nodes, roots, extra) in new (string, int, double, string[])[]
                 {
                     ("roots.db", 80, 1.0, []),
                     ("one.db", 1, 0.04, []),
                     ("edge.db", 150, 0.04, ["--edgeRepulsion", "0.05"]),
                     ("free.db", 150, 0.04, ["--pinLargestRoot", "false", "--springWeight", "similarity"]),
                 })
        {
            string db = TestSupport.Demo(dir, name, nodes, roots: roots);
            var (code, _, err) = TestSupport.Cli([db, "--quiet", "--iterations", "300", .. extra]);
            Assert.True(code == 0, $"{name}: {err}");
            var q = Check(db);
            Assert.Equal(0, q["non_finite_positions"]!.GetValue<int>());
            Assert.Equal(0.0, q["max_time_axis_error"]!.GetValue<double>());
        }
        Assert.Equal(0L, TestSupport.Scalar(dir.File("roots.db"), "SELECT count(*) FROM influence_edges"));
    }

    [Fact]
    public void WarpDeviceRuns()
    {
        using var dir = new TempDir();
        string db = TestSupport.Demo(dir, "g.db", 100);
        var (code, _, err) = TestSupport.Cli(db, "--device", "warp", "--iterations", "100");
        Assert.True(code == 0, err);
        Assert.Contains("(software)", (string)TestSupport.Scalar(db, "SELECT device FROM layout_run")!);
    }

    /// <summary>
    /// Opens the result with the sqlite3.dll 3.15.0 that Unity ships (Unity-FDG's plugin, loaded
    /// read-only in this test process) and runs the reads a §10 loader needs.
    /// </summary>
    [Fact]
    public void Sqlite315CanReadTheResult()
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
        string db = TestSupport.Demo(dir, "g.db", 300);
        Assert.Equal(0, TestSupport.Cli(db, "--quiet", "--iterations", "300").Code);

        using var old = new NativeSqlite(dll);
        output.WriteLine($"{dll}: SQLite {old.Version}");
        Assert.Equal("3.15.0", old.Version);
        var rows = old.Query(db, "PRAGMA integrity_check");
        Assert.Equal("ok", rows[0][0]);
        Assert.Equal("300", old.Query(db, "SELECT count(*) FROM nodes WHERE position_x IS NOT NULL AND position_y IS NOT NULL")[0][0]);
        Assert.Equal("300", old.Query(db, """
            SELECT count(*) FROM nodes n JOIN song_node s ON s.node_id = n.id
            LEFT JOIN node_layout_metadata m ON m.node_id = n.id WHERE m.mass >= 1
            """)[0][0]);
        var run = old.Query(db, "SELECT time_axis, time_direction, year_scale, iterations FROM layout_run ORDER BY run_id DESC LIMIT 1")[0];
        Assert.Equal(new[] { "y", "up", "2.0", "300" }, run.Select(x => x ?? "NULL").ToArray());
        Assert.Equal(TestSupport.Scalar(db, "SELECT count(*) FROM influence_edges")!.ToString(),
            old.Query(db, "SELECT count(*) FROM influence_edges WHERE source_node < target_node")[0][0]);
    }

    /// <summary>Minimal P/Invoke over a specific sqlite3.dll (read-only queries, text results).</summary>
    internal sealed unsafe class NativeSqlite : IDisposable
    {
        private readonly IntPtr _lib;
        private readonly delegate* unmanaged[Cdecl]<byte*, IntPtr*, int, byte*, int> _open;
        private readonly delegate* unmanaged[Cdecl]<IntPtr, int> _close;
        private readonly delegate* unmanaged[Cdecl]<IntPtr, byte*, int, IntPtr*, byte**, int> _prepare;
        private readonly delegate* unmanaged[Cdecl]<IntPtr, int> _step;
        private readonly delegate* unmanaged[Cdecl]<IntPtr, int> _columnCount;
        private readonly delegate* unmanaged[Cdecl]<IntPtr, int, byte*> _columnText;
        private readonly delegate* unmanaged[Cdecl]<IntPtr, int> _finalize;
        private readonly delegate* unmanaged[Cdecl]<IntPtr, byte*> _errmsg;

        public string Version { get; }

        public NativeSqlite(string path)
        {
            _lib = NativeLibrary.Load(path);
            _open = (delegate* unmanaged[Cdecl]<byte*, IntPtr*, int, byte*, int>)NativeLibrary.GetExport(_lib, "sqlite3_open_v2");
            _close = (delegate* unmanaged[Cdecl]<IntPtr, int>)NativeLibrary.GetExport(_lib, "sqlite3_close");
            _prepare = (delegate* unmanaged[Cdecl]<IntPtr, byte*, int, IntPtr*, byte**, int>)NativeLibrary.GetExport(_lib, "sqlite3_prepare_v2");
            _step = (delegate* unmanaged[Cdecl]<IntPtr, int>)NativeLibrary.GetExport(_lib, "sqlite3_step");
            _columnCount = (delegate* unmanaged[Cdecl]<IntPtr, int>)NativeLibrary.GetExport(_lib, "sqlite3_column_count");
            _columnText = (delegate* unmanaged[Cdecl]<IntPtr, int, byte*>)NativeLibrary.GetExport(_lib, "sqlite3_column_text");
            _finalize = (delegate* unmanaged[Cdecl]<IntPtr, int>)NativeLibrary.GetExport(_lib, "sqlite3_finalize");
            _errmsg = (delegate* unmanaged[Cdecl]<IntPtr, byte*>)NativeLibrary.GetExport(_lib, "sqlite3_errmsg");
            var version = (delegate* unmanaged[Cdecl]<byte*>)NativeLibrary.GetExport(_lib, "sqlite3_libversion");
            Version = Marshal.PtrToStringUTF8((IntPtr)version())!;
        }

        public List<string?[]> Query(string dbPath, string sql)
        {
            const int ReadOnly = 1, Row = 100, Done = 101;
            IntPtr db = IntPtr.Zero, stmt = IntPtr.Zero;
            byte[] file = Encoding.UTF8.GetBytes(dbPath + "\0");
            byte[] text = Encoding.UTF8.GetBytes(sql + "\0");
            var rows = new List<string?[]>();
            fixed (byte* f = file)
            fixed (byte* s = text)
            {
                int rc = _open(f, &db, ReadOnly, null);
                try
                {
                    if (rc != 0) throw new InvalidOperationException($"sqlite3_open_v2: {rc}");
                    if (_prepare(db, s, -1, &stmt, null) != 0)
                        throw new InvalidOperationException("prepare: " + Marshal.PtrToStringUTF8((IntPtr)_errmsg(db)));
                    int cols = _columnCount(stmt);
                    while ((rc = _step(stmt)) == Row)
                    {
                        var row = new string?[cols];
                        for (int i = 0; i < cols; i++) row[i] = Marshal.PtrToStringUTF8((IntPtr)_columnText(stmt, i));
                        rows.Add(row);
                    }
                    if (rc != Done) throw new InvalidOperationException("step: " + Marshal.PtrToStringUTF8((IntPtr)_errmsg(db)));
                }
                finally
                {
                    if (stmt != IntPtr.Zero) _finalize(stmt);
                    _close(db);
                }
            }
            return rows;
        }

        public void Dispose() => NativeLibrary.Free(_lib);
    }
}
