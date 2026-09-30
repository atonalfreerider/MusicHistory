using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Layout;

/// <summary>
/// Measures a laid-out graph straight from the database (the <c>check</c> command, and the numbers
/// every run prints): exactness of the time axis, non-finite positions, whether lineages cluster
/// (tree edges vs random pairs on the free plane), spacing, and a hash of the positions for
/// run-to-run comparisons.
/// </summary>
internal static class LayoutQuality
{
    public sealed class Snapshot
    {
        public required int Count { get; init; }
        public required double[] X { get; init; }
        public required double[] Y { get; init; }
        public required double[] Z { get; init; }
        public required double[] Time { get; init; }
        public required (int S, int T, bool Tree)[] Edges { get; init; }
        public required int[] Descendants { get; init; }
        public double[]? MetadataTimeAxis { get; init; }
        public double[]? DisplayRadius { get; init; }
        public string TimeAxis { get; init; } = "y";
        public string TimeDirection { get; init; } = "up";
        public double YearScale { get; init; } = 2;
        public double MinTime { get; init; }
        public long? RunId { get; init; }
        public double? LoopMs { get; init; }
        public int? Iterations { get; init; }
        public double? FinalMeanMove { get; init; }
        public string? Device { get; init; }
    }

    public static Snapshot Load(string path)
    {
        using var c = GraphInput.OpenReadOnly(path);
        var g = GraphInput.Load(c, lenient: true);
        int n = g.Count;
        var x = new double[n];
        var y = new double[n];
        var z = new double[n];
        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText = "SELECT id, position_x, position_y, position_z FROM nodes ORDER BY id";
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                int i = r.GetInt32(0) - 1;
                x[i] = r.IsDBNull(1) ? double.NaN : r.GetDouble(1);
                y[i] = r.IsDBNull(2) ? double.NaN : r.GetDouble(2);
                z[i] = r.IsDBNull(3) ? double.NaN : r.GetDouble(3);
            }
        }
        double[]? meta = null, radius = null;
        if (GraphInput.Columns(c, "node_layout_metadata").Count > 0)
        {
            meta = new double[n];
            radius = new double[n];
            Array.Fill(meta, double.NaN);
            Array.Fill(radius, double.NaN);
            using var cmd = c.CreateCommand();
            cmd.CommandText = "SELECT node_id, time_axis_value, display_radius FROM node_layout_metadata";
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                long id = r.GetInt64(0);
                if (id < 1 || id > n) continue;
                meta[id - 1] = r.IsDBNull(1) ? double.NaN : r.GetDouble(1);
                radius[id - 1] = r.IsDBNull(2) ? double.NaN : r.GetDouble(2);
            }
        }
        string axis = "y", dir = "up";
        double scale = 2, min = g.MinTime;
        long? runId = null;
        double? loopMs = null, move = null;
        int? iters = null;
        string? device = null;
        if (GraphInput.Columns(c, "layout_run").Count > 0)
        {
            using var cmd = c.CreateCommand();
            cmd.CommandText = """
                SELECT run_id, time_axis, time_direction, year_scale, min_time, loop_ms, iterations, final_mean_move, device
                FROM layout_run ORDER BY run_id DESC LIMIT 1
                """;
            using var r = cmd.ExecuteReader();
            if (r.Read())
            {
                runId = r.GetInt64(0);
                axis = r.GetString(1);
                dir = r.GetString(2);
                scale = r.GetDouble(3);
                min = r.GetDouble(4);
                loopMs = r.IsDBNull(5) ? null : r.GetDouble(5);
                iters = r.IsDBNull(6) ? null : r.GetInt32(6);
                move = r.IsDBNull(7) ? null : r.GetDouble(7);
                device = r.IsDBNull(8) ? null : r.GetString(8);
            }
        }
        return new Snapshot
        {
            Count = n, X = x, Y = y, Z = z, Time = g.Time, Descendants = g.Descendants,
            Edges = [.. g.Edges.Select(e => (e.Source, e.Target, e.IsTree))],
            MetadataTimeAxis = meta, DisplayRadius = radius, TimeAxis = axis, TimeDirection = dir, YearScale = scale, MinTime = min,
            RunId = runId, LoopMs = loopMs, Iterations = iters, FinalMeanMove = move, Device = device,
        };
    }

    public static JsonObject Measure(Snapshot s)
    {
        int n = s.Count;
        bool zAxis = s.TimeAxis == "z";
        double sign = s.TimeDirection == "down" ? -1 : 1;
        double[] t = zAxis ? s.Z : s.Y;
        double[] u = s.X;
        double[] v = zAxis ? s.Y : s.Z;

        int nonFinite = 0;
        double maxTimeErr = 0, maxMetaErr = 0;
        for (int i = 0; i < n; i++)
        {
            if (!double.IsFinite(s.X[i]) || !double.IsFinite(s.Y[i]) || !double.IsFinite(s.Z[i]))
            {
                nonFinite++;    // counted here; the other measures skip nothing but may come out null
                continue;
            }
            double expected = sign * (s.Time[i] - s.MinTime) * s.YearScale;
            maxTimeErr = Math.Max(maxTimeErr, Math.Abs(t[i] - expected));
            if (s.MetadataTimeAxis != null)
                maxMetaErr = double.IsFinite(s.MetadataTimeAxis[i]) ? Math.Max(maxMetaErr, Math.Abs(s.MetadataTimeAxis[i] - expected)) : double.PositiveInfinity;
        }

        double Free(int a, int b) => Math.Sqrt((u[a] - u[b]) * (u[a] - u[b]) + (v[a] - v[b]) * (v[a] - v[b]));
        double Dist3(int a, int b)
        {
            double dx = s.X[a] - s.X[b], dy = s.Y[a] - s.Y[b], dz = s.Z[a] - s.Z[b];
            return Math.Sqrt(dx * dx + dy * dy + dz * dz);
        }

        var tree = s.Edges.Where(e => e.Tree).ToArray();
        var sec = s.Edges.Where(e => !e.Tree).ToArray();
        double treeFree = tree.Length > 0 ? tree.Average(e => Free(e.S, e.T)) : double.NaN;
        double secFree = sec.Length > 0 ? sec.Average(e => Free(e.S, e.T)) : double.NaN;
        var rng = new Random(12345);
        double randFree = double.NaN;
        if (n > 1)
        {
            double sum = 0;
            const int samples = 20000;
            for (int k = 0; k < samples; k++)
            {
                int a = rng.Next(n), b = rng.Next(n - 1);
                if (b >= a) b++;
                sum += Free(a, b);
            }
            randFree = sum / samples;
        }
        // Same time gap, random partner: the parent vs a random song within 5 places of the
        // child in time order (ids are time-ordered), so the time gap is matched.
        double matched = double.NaN;
        if (tree.Length > 0 && n > 11)
        {
            double sum = 0;
            int cnt = 0;
            foreach (var e in tree)
                for (int k = 0; k < 4; k++)
                {
                    int r = Math.Clamp(e.T + rng.Next(-5, 6), 0, n - 1);
                    if (r == e.T || r == e.S) continue;
                    sum += Free(e.S, r);
                    cnt++;
                }
            matched = cnt > 0 ? sum / cnt : double.NaN;
        }

        // Nearest neighbours in 3-D and bubble overlaps (O(N²) on the CPU; fine up to ~20k).
        var nn = new double[n];
        Array.Fill(nn, double.PositiveInfinity);
        int overlaps = 0;
        for (int a = 0; a < n; a++)
            for (int b = a + 1; b < n; b++)
            {
                double d = Dist3(a, b);
                if (d < nn[a]) nn[a] = d;
                if (d < nn[b]) nn[b] = d;
                if (s.DisplayRadius != null && d < s.DisplayRadius[a] + s.DisplayRadius[b]) overlaps++;
            }
        var nnSorted = nn.Where(double.IsFinite).OrderBy(d => d).ToArray();
        var radii = Enumerable.Range(0, n).Select(i => Math.Sqrt(u[i] * u[i] + v[i] * v[i])).OrderBy(d => d).ToArray();

        var o = new JsonObject
        {
            ["nodes"] = n,
            ["edges"] = s.Edges.Length,
            ["tree_edges"] = tree.Length,
            ["secondary_edges"] = sec.Length,
            ["run_id"] = s.RunId,
            ["device"] = s.Device,
            ["iterations"] = s.Iterations,
            ["loop_ms"] = R(s.LoopMs),
            ["ms_per_iteration"] = s.LoopMs is double ms && s.Iterations is int it && it > 0 ? R(ms / it) : null,
            ["final_mean_move"] = s.FinalMeanMove is double fm && double.IsFinite(fm) ? fm : null,
            ["time_axis"] = s.TimeAxis,
            ["time_direction"] = s.TimeDirection,
            ["year_scale"] = s.YearScale,
            ["min_time"] = s.MinTime,
            ["non_finite_positions"] = nonFinite,
            ["max_time_axis_error"] = maxTimeErr,
            ["max_metadata_time_axis_error"] = s.MetadataTimeAxis != null && double.IsFinite(maxMetaErr) ? maxMetaErr : null,
            ["mean_free_distance_tree_edges"] = R(treeFree),
            ["mean_free_distance_secondary_edges"] = R(secFree),
            ["mean_free_distance_random_pairs"] = R(randFree),
            ["mean_free_distance_parent_to_random_contemporary_of_child"] = R(matched),
            ["tree_over_random"] = R(treeFree / randFree),
            ["nearest_neighbour_min"] = nnSorted.Length > 0 ? R(nnSorted[0]) : null,
            ["nearest_neighbour_p05"] = nnSorted.Length > 0 ? R(nnSorted[(int)(0.05 * (nnSorted.Length - 1))]) : null,
            ["nearest_neighbour_median"] = nnSorted.Length > 0 ? R(nnSorted[nnSorted.Length / 2]) : null,
            ["display_radius_overlapping_pairs"] = s.DisplayRadius != null ? overlaps : null,
            ["free_radius_median"] = R(radii[n / 2]),
            ["free_radius_p95"] = R(radii[(int)(0.95 * (n - 1))]),
            ["free_radius_max"] = R(radii[n - 1]),
            ["time_extent"] = nonFinite == 0 ? R(t.Max() - t.Min()) : null,
            ["positions_sha256"] = PositionsHash(s),
        };
        return o;
    }

    /// <summary>SHA-256 over the raw IEEE-754 bytes of every stored position, in id order.</summary>
    public static string PositionsHash(Snapshot s)
    {
        using var sha = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        Span<byte> buf = stackalloc byte[8];
        for (int i = 0; i < s.Count; i++)
            foreach (double d in new[] { s.X[i], s.Y[i], s.Z[i] })
            {
                BitConverter.TryWriteBytes(buf, d);
                sha.AppendData(buf);
            }
        return Convert.ToHexStringLower(sha.GetHashAndReset());
    }

    private static double? R(double? x) =>
        x is double d && double.IsFinite(d) ? Math.Round(d, 6) : null;

    public static string Format(JsonObject o) =>
        string.Join('\n', o.Select(kv => $"  {kv.Key,-58} {Convert.ToString(kv.Value?.ToJsonString().Trim('"'), CultureInfo.InvariantCulture)}"));
}
