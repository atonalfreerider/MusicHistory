using System.Diagnostics;
using ComputeSharp;
using MusicHistory.Layout.Shaders;

namespace MusicHistory.Layout;

/// <summary>What one layout run produced. Free axes in float (as the GPU computed them), time exact.</summary>
internal sealed class LayoutResult
{
    public required float[] U { get; init; }            // first free axis (world x)
    public required float[] V { get; init; }            // second free axis (world z, or y when time is on z)
    public required double[] TimeCoord { get; init; }   // sign · (time_value − min_time) · yearScale, exact
    public required double[] Mass { get; init; }
    public required int PinnedNode { get; init; }       // index, −1 = none
    public required string Device { get; init; }
    public int Iterations { get; init; }
    public double LoopMs { get; init; }
    public double FinalMeanMove { get; init; }
    public double FinalMaxMove { get; init; }
    public double ResidualMean { get; init; }
    public double ResidualMax { get; init; }
    public List<TracePoint> Trace { get; init; } = [];
    public int NaNCount => U.Zip(V).Count(p => !float.IsFinite(p.First) || !float.IsFinite(p.Second));
}

internal readonly record struct TracePoint(int Iteration, float Temperature, double MeanMove, double MaxMove);

/// <summary>CPU-side inputs of the kernel, built deterministically from the graph and the parameters.</summary>
internal sealed class KernelInput
{
    public required int Count { get; init; }
    public required float4[] Start { get; init; }       // (u, time, v, mass)
    public required double[] TimeCoord { get; init; }
    public required double[] Mass { get; init; }
    public required int[] Pinned { get; init; }
    public required float[] StepScale { get; init; }
    public required int[] AdjStart { get; init; }        // Count + 1 entries (CSR)
    public required int[] AdjNode { get; init; }
    public required float[] AdjK { get; init; }
    public required int2[] Edges { get; init; }
    public int PinnedNode { get; init; }
}

/// <summary>
/// The temporal force-directed layout (successor of GPU-FDG's <c>ForceDirectedGraph</c>): time is
/// pinned on one axis, the two free axes are found on the GPU. See <see cref="LayoutParams"/> for
/// the force model and <see cref="TemporalForceShader"/> for the kernel.
/// </summary>
internal static class TemporalGraph
{
    private const float GoldenAngle = 2.39996323f;

    /// <summary>m = 1 + log2(1 + descendants) [+ A·log2(1 + out_degree)]: influential songs push harder and sit nearer the axis.</summary>
    public static double Mass(int descendants, int outDegree, float massOutDegree) =>
        1.0 + Math.Log2(1.0 + descendants) + massOutDegree * Math.Log2(1.0 + outDegree);

    public static KernelInput Prepare(LayoutGraph g, LayoutParams p)
    {
        int n = g.Count;
        var mass = new double[n];
        var time = new double[n];
        for (int i = 0; i < n; i++)
        {
            mass[i] = Mass(g.Descendants[i], g.OutDegree[i], p.MassOutDegree);
            time[i] = p.TimeSign * (g.Time[i] - g.MinTime) * p.YearScale;
        }

        // Symmetric springs in CSR form; k = spring · kindSpring · (weight | similarity | 1).
        var adj = new List<(int Node, float K)>[n];
        for (int i = 0; i < n; i++) adj[i] = [];
        foreach (var e in g.Edges)
        {
            double factor = p.SpringWeight switch
            {
                SpringWeight.Weight => e.Weight,
                SpringWeight.Similarity => e.Similarity,
                _ => 1.0,
            };
            float k = (float)(p.Spring * (e.IsTree ? p.TreeSpring : p.SecondarySpring) * factor);
            if (k <= 0) continue;
            adj[e.Source].Add((e.Target, k));
            adj[e.Target].Add((e.Source, k));
        }
        var adjStart = new int[n + 1];
        var adjNode = new List<int>();
        var adjK = new List<float>();
        var step = new float[n];
        for (int i = 0; i < n; i++)
        {
            adjStart[i] = adjNode.Count;
            float sumK = 0;
            foreach (var (node, k) in adj[i])
            {
                adjNode.Add(node);
                adjK.Add(k);
                sumK += k;
            }
            step[i] = 1f / ((float)mass[i] + sumK + 1f);
        }
        adjStart[n] = adjNode.Count;

        // Deterministic start. The largest tree's root sits on the axis; the other roots on a ring
        // whose circumference grows with their number; every child starts near its parent at a
        // golden-angle offset (sunflower spiral over its siblings), first child pointing outward.
        int largest = -1;
        for (int i = 0; i < n; i++)
            if (g.Parent[i] < 0 && (largest < 0 || g.Descendants[i] > g.Descendants[largest])) largest = i;
        int ringCount = g.Roots - 1;
        float ringRadius = MathF.Max(2f * p.RootSpacing, p.RootSpacing * ringCount / (2f * MathF.PI));
        var u = new float[n];
        var v = new float[n];
        var siblings = new int[n];
        int ringIndex = 0;
        for (int i = 0; i < n; i++)
        {
            int par = g.Parent[i];
            if (par < 0)
            {
                if (i == largest) continue; // (0, 0)
                float a = 2f * MathF.PI * ringIndex++ / Math.Max(1, ringCount);
                u[i] = MathF.Cos(a) * ringRadius;
                v[i] = MathF.Sin(a) * ringRadius;
            }
            else
            {
                int k = siblings[par]++;
                float baseAngle = u[par] == 0 && v[par] == 0 ? 0f : MathF.Atan2(v[par], u[par]);
                float a = baseAngle + k * GoldenAngle;
                float r = p.ChildSpacing * MathF.Sqrt(k + 1);
                u[i] = u[par] + MathF.Cos(a) * r;
                v[i] = v[par] + MathF.Sin(a) * r;
            }
        }

        var start = new float4[n];
        for (int i = 0; i < n; i++) start[i] = new float4(u[i], (float)time[i], v[i], (float)mass[i]);
        var pinned = new int[n];
        if (p.PinLargestRoot && largest >= 0) pinned[largest] = 1;
        return new KernelInput
        {
            Count = n, Start = start, TimeCoord = time, Mass = mass, Pinned = pinned, StepScale = step,
            AdjStart = adjStart, AdjNode = [.. adjNode], AdjK = [.. adjK],
            Edges = [.. g.Edges.Select(e => new int2(e.Source, e.Target))],
            PinnedNode = p.PinLargestRoot ? largest : -1,
        };
    }

    public static LayoutResult Run(LayoutGraph g, LayoutParams p, int trace = 0, TextWriter? log = null)
    {
        p.Validate();
        var input = Prepare(g, p);
        var device = Gpu.Select(p.Device);
        try
        {
            return Run(device, input, p, trace, log);
        }
        catch (Exception ex) when (ex is not GpuException and not LayoutFailedException and not UsageException)
        {
            throw new GpuException($"the GPU layout failed on {Gpu.Describe(device)} ({ex.GetType().Name}: {ex.Message})", ex);
        }
    }

    private static LayoutResult Run(GraphicsDevice device, KernelInput input, LayoutParams p, int trace, TextWriter? log)
    {
        int n = input.Count;
        // D3D12 buffers cannot be empty: a graph without edges gets one unused dummy entry.
        int[] adjNode = input.AdjNode.Length > 0 ? input.AdjNode : [0];
        float[] adjK = input.AdjK.Length > 0 ? input.AdjK : [0f];
        int2[] edges = input.Edges.Length > 0 ? input.Edges : [new int2(0, 0)];

        using ReadWriteBuffer<float4> a = device.AllocateReadWriteBuffer(input.Start);
        using ReadWriteBuffer<float4> b = device.AllocateReadWriteBuffer(input.Start);
        using ReadWriteBuffer<float2> vel = device.AllocateReadWriteBuffer<float2>(n);
        using ReadOnlyBuffer<int> pinned = device.AllocateReadOnlyBuffer(input.Pinned);
        using ReadOnlyBuffer<float> step = device.AllocateReadOnlyBuffer(input.StepScale);
        using ReadOnlyBuffer<int> adjStart = device.AllocateReadOnlyBuffer(input.AdjStart);
        using ReadOnlyBuffer<int> adjNodeB = device.AllocateReadOnlyBuffer(adjNode);
        using ReadOnlyBuffer<float> adjKB = device.AllocateReadOnlyBuffer(adjK);
        using ReadOnlyBuffer<int2> edgeB = device.AllocateReadOnlyBuffer(edges);

        TemporalForceShader Shader(ReadWriteBuffer<float4> src, ReadWriteBuffer<float4> dst, ReadWriteBuffer<float2> v,
            float damping, float temperature) =>
            new(src, dst, v, pinned, step, adjStart, adjNodeB, adjKB, edgeB, n, input.Edges.Length,
                p.Repulsion, p.Softening, p.RestLength, p.Centering, p.EdgeRepulsion, p.EdgeClearance * p.EdgeClearance,
                damping, temperature);

        // Iteration it reads (it even ? a : b) and writes the other buffer.
        ReadWriteBuffer<float4> Src(int it) => (it & 1) == 0 ? a : b;

        // Runs iterations [from, to) as command lists of up to `batch` dispatches. Scalars (the
        // temperature) are recorded per dispatch, and UAV barriers order the dispatches.
        int batch = EffectiveBatch(p, n, input.Edges.Length);
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

        // Checkpoints: the last iteration always (final mean move), plus every trace interval.
        var checkpoints = new SortedSet<int> { p.Iterations - 1 };
        if (trace > 0)
            for (int it = trace - 1; it < p.Iterations; it += trace) checkpoints.Add(it);

        var traceOut = new List<TracePoint>();
        var free = MovableIndices(input);
        double lastMean = 0, lastMax = 0;
        var sw = Stopwatch.StartNew();
        int done = 0;
        foreach (int c in checkpoints)
        {
            RunRange(done, c);
            float4[] before = Src(c).ToArray();
            RunRange(c, c + 1);
            float4[] after = Src(c + 1).ToArray();
            (lastMean, lastMax) = Moves(before, after, free);
            done = c + 1;
            var tp = new TracePoint(c + 1, p.Temperature(c), lastMean, lastMax);
            traceOut.Add(tp);
            if (trace > 0)
                log?.WriteLine(FormattableString.Invariant(
                    $"  iteration {tp.Iteration,5}  T={tp.Temperature:F4}  mean move {tp.MeanMove:E2}  max move {tp.MaxMove:E2}"));
        }
        float4[] final = Src(p.Iterations).ToArray();
        sw.Stop();

        // Residual: the step one more undamped, uncapped iteration would take from the final
        // state. The final mean move is bounded by the last temperature; this is not.
        double resMean = 0, resMax = 0;
        using (ReadWriteBuffer<float4> scratch = device.AllocateReadWriteBuffer<float4>(n))
        using (ReadWriteBuffer<float2> scratchVel = device.AllocateReadWriteBuffer<float2>(n))
        {
            device.For(n, Shader(Src(p.Iterations), scratch, scratchVel, 0f, float.MaxValue));
            float4[] next = scratch.ToArray();
            (resMean, resMax) = Moves(final, next, free);
            if (trace > 0 && log != null)
            {
                // The nodes furthest from equilibrium, for tuning.
                var worst = free.OrderByDescending(i => Math.Abs(next[i].X - final[i].X) + Math.Abs(next[i].Z - final[i].Z)).Take(5);
                foreach (int i in worst)
                    log.WriteLine(FormattableString.Invariant(
                        $"  residual node {i + 1}: step ({next[i].X - final[i].X:E2}, {next[i].Z - final[i].Z:E2}), mass {input.Mass[i]:F2}, springs {input.AdjStart[i + 1] - input.AdjStart[i]}, at ({final[i].X:F2}, {final[i].Y:F2}, {final[i].Z:F2})"));
            }
        }

        var u = new float[n];
        var v = new float[n];
        for (int i = 0; i < n; i++)
        {
            u[i] = final[i].X;
            v[i] = final[i].Z;
        }
        return new LayoutResult
        {
            U = u, V = v, TimeCoord = input.TimeCoord, Mass = input.Mass, PinnedNode = input.PinnedNode,
            Device = Gpu.Describe(device), Iterations = p.Iterations, LoopMs = sw.Elapsed.TotalMilliseconds,
            FinalMeanMove = lastMean, FinalMaxMove = lastMax, ResidualMean = resMean, ResidualMax = resMax,
            Trace = trace > 0 ? traceOut : [],
        };
    }

    /// <summary>
    /// Iterations per command list: <see cref="LayoutParams.Batch"/>, reduced for big graphs so one
    /// submission stays around 2·10⁹ pair interactions (well under Windows' 2 s GPU watchdog on a
    /// slow GPU; ~25 ms on an RTX 2080 Ti).
    /// </summary>
    public static int EffectiveBatch(LayoutParams p, int nodes, int edges)
    {
        double work = (double)nodes * nodes + (p.EdgeRepulsion > 0 ? (double)nodes * edges : 0);
        return (int)Math.Clamp(2e9 / Math.Max(1, work), 1, p.Batch);
    }

    private static int[] MovableIndices(KernelInput input)
    {
        var idx = Enumerable.Range(0, input.Count).Where(i => input.Pinned[i] == 0).ToArray();
        return idx.Length > 0 ? idx : [.. Enumerable.Range(0, input.Count)];
    }

    /// <summary>Mean and max distance moved on the free axes (double sums in index order: deterministic).</summary>
    private static (double Mean, double Max) Moves(float4[] before, float4[] after, int[] free)
    {
        double sum = 0, max = 0;
        foreach (int i in free)
        {
            double du = (double)after[i].X - before[i].X;
            double dv = (double)after[i].Z - before[i].Z;
            double m = Math.Sqrt(du * du + dv * dv);
            if (double.IsNaN(m)) m = double.PositiveInfinity;
            sum += m;
            max = Math.Max(max, m);
        }
        return (sum / free.Length, max);
    }
}
