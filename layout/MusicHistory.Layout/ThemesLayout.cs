using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using ComputeSharp;
using MusicHistory.Layout.Shaders;

namespace MusicHistory.Layout;

/// <summary>What one themes layout run produced (song positions as the GPU computed them).</summary>
internal sealed class ThemesResult
{
    public required float[] X { get; init; }
    public required float[] Y { get; init; }
    public required float[] Z { get; init; }
    /// <summary>Weighted barycentre of every song (x, z), exact (double).</summary>
    public required double[] BaryX { get; init; }
    public required double[] BaryZ { get; init; }
    public required double Radius { get; init; }
    public required string Device { get; init; }
    public int Iterations { get; init; }
    public double LoopMs { get; init; }
    public double FinalMeanMove { get; init; }
    public double FinalMaxMove { get; init; }
    public double ResidualMean { get; init; }
    public double ResidualMax { get; init; }
    public List<TracePoint> Trace { get; init; } = [];

    public int NaNCount
    {
        get
        {
            int bad = 0;
            for (int i = 0; i < X.Length; i++)
                if (!float.IsFinite(X[i]) || !float.IsFinite(Y[i]) || !float.IsFinite(Z[i])) bad++;
            return bad;
        }
    }
}

/// <summary>
/// The lyric-themes layout (DESIGN.md §12): ten anchors pinned on a ring, songs pulled to the
/// weighted barycentre of their theme scores and spread into clouds by a softened repulsion.
/// See <see cref="ThemesParams"/> for the model and <see cref="ThemesForceShader"/> for the kernel.
/// </summary>
internal static class ThemesLayout
{
    private const int K = ThemesSchema.AnchorCount;

    public static double RadiusFor(ThemesGraph g, ThemesParams p) => p.Radius ?? g.MetaRadius ?? ThemesParams.DefaultRadius;

    /// <summary>w_ik = s_ik^γ / Σ_k s_ik^γ (computed as (s/max)^γ, so a large γ cannot underflow).</summary>
    public static double[] Weights(ThemesGraph g, double sharpen)
    {
        var w = new double[g.Count * K];
        for (int i = 0; i < g.Count; i++)
        {
            double max = 0;
            for (int a = 0; a < K; a++) max = Math.Max(max, g.ScoreOf(i, a));
            double sum = 0;
            for (int a = 0; a < K; a++)
            {
                double x = Math.Pow(g.ScoreOf(i, a) / max, sharpen);
                w[i * K + a] = x;
                sum += x;
            }
            for (int a = 0; a < K; a++) w[i * K + a] /= sum;
        }
        return w;
    }

    public static (double X, double Z)[] AnchorPositions(double radius) =>
        [.. Enumerable.Range(1, K).Select(id => ThemesSchema.AnchorPosition(id, radius)).Select(t => (t.X, t.Z))];

    /// <summary>The weighted barycentre b_i = Σ_k w_ik A_k of every song: its exact equilibrium without repulsion.</summary>
    public static (double[] X, double[] Z) Barycentres(double[] weights, int count, double radius)
    {
        var anchors = AnchorPositions(radius);
        var bx = new double[count];
        var bz = new double[count];
        for (int i = 0; i < count; i++)
            for (int a = 0; a < K; a++)
            {
                bx[i] += weights[i * K + a] * anchors[a].X;
                bz[i] += weights[i * K + a] * anchors[a].Z;
            }
        return (bx, bz);
    }

    /// <summary>
    /// Deterministic start: each song at its barycentre plus a seeded offset (uniform in a disc of
    /// radius <c>jitter</c>; a random height inside half the slab), so songs with identical score
    /// vectors do not start on one point (where the repulsion between them would be 0).
    /// </summary>
    public static float4[] Start(double[] bx, double[] bz, ThemesParams p)
    {
        var rng = new Random(p.Seed);
        var start = new float4[bx.Length];
        for (int i = 0; i < bx.Length; i++)
        {
            double r = p.Jitter * Math.Sqrt(rng.NextDouble());
            double phi = 2 * Math.PI * rng.NextDouble();
            double h = (rng.NextDouble() - 0.5) * 0.5 * p.Slab;
            start[i] = new float4((float)(bx[i] + r * Math.Cos(phi)), p.Slab > 0 ? (float)h : 0f, (float)(bz[i] + r * Math.Sin(phi)), 0f);
        }
        return start;
    }

    public static ThemesResult Run(ThemesGraph g, ThemesParams p, int trace = 0, TextWriter? log = null)
    {
        p.Validate();
        var device = Gpu.Select(p.Device);
        try
        {
            return Run(device, g, p, trace, log);
        }
        catch (Exception ex) when (ex is not GpuException and not LayoutFailedException and not UsageException)
        {
            throw new GpuException($"the GPU themes layout failed on {Gpu.Describe(device)} ({ex.GetType().Name}: {ex.Message})", ex);
        }
    }

    /// <summary>Iterations per command list, reduced for big inputs (about 2·10⁹ pair interactions per submission).</summary>
    public static int EffectiveBatch(ThemesParams p, int songs) =>
        (int)Math.Clamp(2e9 / Math.Max(1.0, (double)songs * songs), 1, p.Batch);

    private static ThemesResult Run(GraphicsDevice device, ThemesGraph g, ThemesParams p, int trace, TextWriter? log)
    {
        int n = g.Count;
        double radius = RadiusFor(g, p);
        double[] w = Weights(g, p.Sharpen);
        var (bx, bz) = Barycentres(w, n, radius);
        float4[] start = Start(bx, bz, p);
        float4[] anchors = [.. AnchorPositions(radius).Select(a => new float4((float)a.X, 0f, (float)a.Z, 0f))];
        float[] wf = [.. w.Select(x => (float)x)];

        using ReadWriteBuffer<float4> a = device.AllocateReadWriteBuffer(start);
        using ReadWriteBuffer<float4> b = device.AllocateReadWriteBuffer(start);
        using ReadWriteBuffer<float4> vel = device.AllocateReadWriteBuffer<float4>(n);
        using ReadOnlyBuffer<float> weights = device.AllocateReadOnlyBuffer(wf);
        using ReadOnlyBuffer<float4> anchorB = device.AllocateReadOnlyBuffer(anchors);

        float cutoff2 = p.Cutoff > 0 ? p.Cutoff * p.Cutoff + p.Softening : 0f;
        float cutoffShift = p.Cutoff > 0 ? 1f / (cutoff2 * MathF.Sqrt(cutoff2)) : 0f;
        ThemesForceShader Shader(ReadWriteBuffer<float4> src, ReadWriteBuffer<float4> dst, ReadWriteBuffer<float4> v, float damping, float temperature) =>
            new(src, dst, v, weights, anchorB, n, K, p.Spring, p.Repulsion, p.Softening, cutoff2, cutoffShift, p.Slab / 2f, p.Flatten,
                damping, temperature);

        ReadWriteBuffer<float4> Src(int it) => (it & 1) == 0 ? a : b;
        int batch = EffectiveBatch(p, n);
        void RunRange(int from, int to)
        {
            for (int s = from; s < to; s += batch)
            {
                int e = Math.Min(to, s + batch);
                using var ctx = device.CreateComputeContext();
                for (int it = s; it < e; it++)
                {
                    var src = Src(it);
                    var dst = Src(it + 1);
                    ctx.For(n, Shader(src, dst, vel, p.Damping, p.Temperature(it)));
                    ctx.Barrier(dst);
                    ctx.Barrier(vel);
                    ctx.Barrier(src);
                }
            }
        }

        var checkpoints = new SortedSet<int> { p.Iterations - 1 };
        if (trace > 0)
            for (int it = trace - 1; it < p.Iterations; it += trace) checkpoints.Add(it);
        var traceOut = new List<TracePoint>();
        double lastMean = 0, lastMax = 0;
        var sw = Stopwatch.StartNew();
        int done = 0;
        foreach (int c in checkpoints)
        {
            RunRange(done, c);
            float4[] before = Src(c).ToArray();
            RunRange(c, c + 1);
            float4[] after = Src(c + 1).ToArray();
            (lastMean, lastMax) = Moves(before, after);
            done = c + 1;
            var tp = new TracePoint(c + 1, p.Temperature(c), lastMean, lastMax);
            traceOut.Add(tp);
            if (trace > 0)
                log?.WriteLine(FormattableString.Invariant(
                    $"  iteration {tp.Iteration,5}  T={tp.Temperature:F4}  mean move {tp.MeanMove:E2}  max move {tp.MaxMove:E2}"));
        }
        float4[] final = Src(p.Iterations).ToArray();
        sw.Stop();

        // Residual: the step one more undamped, uncapped iteration would take.
        double resMean, resMax;
        using (ReadWriteBuffer<float4> scratch = device.AllocateReadWriteBuffer<float4>(n))
        using (ReadWriteBuffer<float4> scratchVel = device.AllocateReadWriteBuffer<float4>(n))
        {
            device.For(n, Shader(Src(p.Iterations), scratch, scratchVel, 0f, float.MaxValue));
            (resMean, resMax) = Moves(final, scratch.ToArray());
        }

        return new ThemesResult
        {
            X = [.. final.Select(q => q.X)], Y = [.. final.Select(q => q.Y)], Z = [.. final.Select(q => q.Z)],
            BaryX = bx, BaryZ = bz, Radius = radius, Device = Gpu.Describe(device), Iterations = p.Iterations,
            LoopMs = sw.Elapsed.TotalMilliseconds, FinalMeanMove = lastMean, FinalMaxMove = lastMax,
            ResidualMean = resMean, ResidualMax = resMax, Trace = trace > 0 ? traceOut : [],
        };
    }

    /// <summary>Mean and max 3-D distance moved (double sums in index order: deterministic).</summary>
    private static (double Mean, double Max) Moves(float4[] before, float4[] after)
    {
        double sum = 0, max = 0;
        for (int i = 0; i < before.Length; i++)
        {
            double dx = (double)after[i].X - before[i].X, dy = (double)after[i].Y - before[i].Y, dz = (double)after[i].Z - before[i].Z;
            double m = Math.Sqrt(dx * dx + dy * dy + dz * dz);
            if (double.IsNaN(m)) m = double.PositiveInfinity;
            sum += m;
            max = Math.Max(max, m);
        }
        return (sum / before.Length, max);
    }

    // ------------------------------------------------------------------ measurements

    /// <summary>
    /// How far the repulsion moved songs off their barycentres, whether any left the ring or
    /// crossed into another anchor's sector, how the clouds are spaced, and where the peaked songs
    /// ended. Everything in world units unless named <c>*_over_radius</c>.
    /// </summary>
    public static JsonObject Measure(ThemesGraph g, ThemesResult r)
    {
        int n = g.Count;
        double radius = r.Radius;
        var anchors = AnchorPositions(radius);
        var disp = new double[n];
        int outside = 0, crossed = 0, eligible = 0;
        double maxRadius = 0, maxAbsY = 0;
        for (int i = 0; i < n; i++)
        {
            double dx = r.X[i] - r.BaryX[i], dz = r.Z[i] - r.BaryZ[i];
            disp[i] = Math.Sqrt(dx * dx + dz * dz);
            double rad = Math.Sqrt((double)r.X[i] * r.X[i] + (double)r.Z[i] * r.Z[i]);
            maxRadius = Math.Max(maxRadius, rad);
            maxAbsY = Math.Max(maxAbsY, Math.Abs((double)r.Y[i]));
            if (rad > radius) outside++;
            // A song whose barycentre is clearly in one anchor's sector (|b| >= R/2) crossed if it
            // ended nearer another anchor.
            if (Math.Sqrt(r.BaryX[i] * r.BaryX[i] + r.BaryZ[i] * r.BaryZ[i]) >= radius / 2)
            {
                eligible++;
                if (Nearest(anchors, r.BaryX[i], r.BaryZ[i]) != Nearest(anchors, r.X[i], r.Z[i])) crossed++;
            }
        }
        var sorted = disp.Order().ToArray();

        // Nearest-neighbour spacing (3-D), O(N²) on the CPU.
        var nn = new double[n];
        for (int i = 0; i < n; i++)
        {
            double best = double.PositiveInfinity;
            for (int j = 0; j < n; j++)
            {
                if (j == i) continue;
                double dx = (double)r.X[i] - r.X[j], dy = (double)r.Y[i] - r.Y[j], dz = (double)r.Z[i] - r.Z[j];
                best = Math.Min(best, dx * dx + dy * dy + dz * dz);
            }
            nn[i] = Math.Sqrt(best);
        }
        var nnSorted = nn.Order().ToArray();

        // Peaked songs: one theme holds >= 0.9 of the score.
        int peaked = 0;
        double peakedSum = 0, peakedMax = 0;
        for (int i = 0; i < n; i++)
        {
            int top = 0;
            for (int a = 1; a < K; a++)
                if (g.ScoreOf(i, a) > g.ScoreOf(i, top)) top = a;
            if (g.ScoreOf(i, top) < 0.9) continue;
            peaked++;
            double dx = r.X[i] - anchors[top].X, dz = r.Z[i] - anchors[top].Z;
            double d = Math.Sqrt(dx * dx + dz * dz);
            peakedSum += d;
            peakedMax = Math.Max(peakedMax, d);
        }

        static double Pct(double[] s, double q) => s.Length == 0 ? 0 : s[Math.Min(s.Length - 1, (int)Math.Floor(q * (s.Length - 1) + 0.5))];
        static double R6(double x) => double.IsFinite(x) ? Math.Round(x, 6) : -1;
        return new JsonObject
        {
            ["songs"] = n,
            ["radius"] = radius,
            ["non_finite_positions"] = r.NaNCount,
            ["mean_displacement"] = R6(disp.Average()),
            ["mean_displacement_over_radius"] = R6(disp.Average() / radius),
            ["median_displacement"] = R6(Pct(sorted, 0.5)),
            ["p95_displacement"] = R6(Pct(sorted, 0.95)),
            ["max_displacement"] = R6(sorted[^1]),
            ["outside_ring"] = outside,
            ["max_radius"] = R6(maxRadius),
            ["crossed_sector"] = crossed,
            ["crossed_sector_eligible"] = eligible,
            ["nn_mean"] = R6(n > 1 ? nn.Average() : 0),
            ["nn_p5"] = R6(n > 1 ? Pct(nnSorted, 0.05) : 0),
            ["nn_min"] = R6(n > 1 ? nnSorted[0] : 0),
            ["peaked_songs"] = peaked,
            ["peaked_mean_distance_to_anchor"] = R6(peaked > 0 ? peakedSum / peaked : 0),
            ["peaked_max_distance_to_anchor"] = R6(peakedMax),
            ["max_abs_y"] = R6(maxAbsY),
            ["positions_sha256"] = PositionsHash(r),
        };
    }

    private static int Nearest((double X, double Z)[] anchors, double x, double z)
    {
        int best = 0;
        double bd = double.PositiveInfinity;
        for (int a = 0; a < anchors.Length; a++)
        {
            double dx = x - anchors[a].X, dz = z - anchors[a].Z, d = dx * dx + dz * dz;
            if (d < bd)
            {
                bd = d;
                best = a;
            }
        }
        return best;
    }

    /// <summary>SHA-256 over the float bits of every position, in node order (bit-identity check).</summary>
    public static string PositionsHash(ThemesResult r)
    {
        var bytes = new byte[r.X.Length * 12];
        for (int i = 0; i < r.X.Length; i++)
        {
            BitConverter.TryWriteBytes(bytes.AsSpan(i * 12), r.X[i]);
            BitConverter.TryWriteBytes(bytes.AsSpan(i * 12 + 4), r.Y[i]);
            BitConverter.TryWriteBytes(bytes.AsSpan(i * 12 + 8), r.Z[i]);
        }
        return Convert.ToHexStringLower(SHA256.HashData(bytes));
    }
}
