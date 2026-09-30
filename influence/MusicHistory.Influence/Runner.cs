using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;

namespace MusicHistory.Influence;

internal sealed class RunOptions
{
    public string Db = Path.Combine("data", "musichistory.sqlite");
    public string Graph = Path.Combine("data", "graph", "music_graph.db");
    public string? Report;
    public string? Root;
    public string? GeneratedAt;
    public bool Export = true;

    public string ReportPath => Report ?? Path.Combine(Path.GetDirectoryName(Path.GetFullPath(Graph))!, "influence_report.json");
    public string Timestamp => GeneratedAt ?? DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", CultureInfo.InvariantCulture);
}

/// <summary>An empirical threshold: value, how it was obtained, and the sample it came from.</summary>
internal sealed record Threshold(double Value, string Method, int Sample, double Fpr)
{
    /// <summary>
    /// The value t with P(sample &gt; t) &lt;= fpr: the (floor(fpr n) + 1)-th largest sample value (benchmark
    /// <c>analyze.tpr_at</c>), when at least 10 sample values may lie above it; otherwise an exponential tail fitted
    /// over the sample's 99th percentile (<c>eval_pairlevel3.thr_for</c>): t = u + ln(0.01 / fpr) * mean excess.
    /// </summary>
    public static Threshold Of(double[] sortedAscending, double fpr)
    {
        int n = sortedAscending.Length;
        if (n == 0) return new Threshold(double.PositiveInfinity, "no sample", 0, fpr);
        int k = (int)Math.Floor(fpr * n);
        if (k >= 10 || fpr >= 1) return new Threshold(sortedAscending[Math.Max(0, n - 1 - k)], "empirical", n, fpr);
        double u = Quantile(sortedAscending, 0.99);
        double sum = 0;
        int m = 0;
        foreach (double v in sortedAscending)
            if (v > u)
            {
                sum += v - u;
                m++;
            }
        if (m == 0) return new Threshold(sortedAscending[^1], "sample maximum", n, fpr);
        return new Threshold(u + Math.Log(0.01 / Math.Min(fpr, 0.01)) * (sum / m), "exponential tail fit over p99", n, fpr);
    }

    /// <summary>numpy's default (linear) quantile of an ascending array.</summary>
    public static double Quantile(double[] s, double q)
    {
        if (s.Length == 0) return double.NaN;
        double pos = q * (s.Length - 1);
        int lo = (int)Math.Floor(pos);
        int hi = Math.Min(s.Length - 1, lo + 1);
        return s[lo] + (s[hi] - s[lo]) * (pos - lo);
    }

    /// <summary>Fraction of the sample at or above <paramref name="z"/> (the pair's empirical tail probability).</summary>
    public static double Tail(double[] sortedAscending, double z)
    {
        if (sortedAscending.Length == 0 || double.IsNaN(z)) return 1;
        int lo = 0, hi = sortedAscending.Length;
        while (lo < hi)
        {
            int mid = (lo + hi) >>> 1;
            if (sortedAscending[mid] < z) lo = mid + 1; else hi = mid;
        }
        return (sortedAscending.Length - lo) / (double)sortedAscending.Length;
    }
}

/// <summary>Everything the scoring pass produced, kept for the report.</summary>
internal sealed class RunState
{
    public required Song[] Songs;
    public required V8Engine Engine;
    public required PairRec[][] Recs;                 // Recs[b][a], a < b
    public required List<PairResult> Stored;          // pair_score rows (time-ordered), in (B, A) order
    public required List<PairResult> Same;            // same-time pairs checked for versions
    public required PairDetails Details;
    public TreeResult? Tree;
    public LoadStats Load = new();
    public readonly Dictionary<string, double> Timings = [];
    public Threshold Fused = null!, Riff = null!, Store = null!;
    public readonly Dictionary<string, Threshold> Count = new(StringComparer.Ordinal);
    public double[] Sample = [];                      // null-sample fused z, ascending (NaN pairs as -1e9)
    public double[] RiffSample = [];                  // null-sample riff window-max z (pairs with the riff family available), ascending
    public long TimeOrdered, Contemporaneous, Windows;
    public int AboveThreshold, BassAloneRejected, BassAloneRescued, HubRejected, VersionsAbove;
    public readonly HashSet<(int, int)> SampleSet = [];
    public double[] HubMeanB = [], HubSdB = [], HubMeanA = [], HubSdA = [];
    public double CountMinZ = 3, HubFloor = 2;

    public bool Earlier(int a, int b) => a < b && DateOrder.Earlier(Songs[a].Date, Songs[b].Date);

    public PairResult? StoredResult(int a, int b) => _byPair.TryGetValue((a, b), out var r) ? r : null;

    private Dictionary<(int, int), PairResult> _byPair = [];

    public void Index()
    {
        _byPair = [];
        foreach (var r in Stored) _byPair[(r.A, r.B)] = r;
        foreach (var r in Same) _byPair.TryAdd((r.A, r.B), r);
    }

    /// <summary>The hubness z of a pair: min over its two sides of (Z - mean) / max(sd, floor).</summary>
    public (double ZA, double ZB) Hub(int a, int b, double z, double floor) =>
        ((z - HubMeanA[a]) / Math.Max(HubSdA[a], floor), (z - HubMeanB[b]) / Math.Max(HubSdB[b], floor));
}

/// <summary>The <c>run</c> subcommand: order, V8 evidence for every pair, the empirical decision, tree, export and report.</summary>
internal static class Runner
{
    public static readonly string[] CountChannels = ["melody", "bass", "chord", "loop", "lanes"];

    public static RunState Score(Microsoft.Data.Sqlite.SqliteConnection conn, Params p, TextWriter log)
    {
        var sw = Stopwatch.StartNew();
        var load = new LoadStats();
        var songs = PipelineDb.LoadSongs(conn, load);
        if (songs.Length == 0) throw new InvalidOperationException("no selected, analyzed songs with a year in the pipeline database");
        double tLoad = sw.Elapsed.TotalSeconds;
        log.WriteLine($"load: {songs.Length} songs ({load.SkippedNoYear} without a year skipped; {load.NoMelody} without melody, " +
                      $"{load.NoBass} without bass, {load.NoChords} without chords, {load.NoLoops} without loops, {load.Lanes} lane lines " +
                      $"in {load.WithLanes} songs) in {tLoad:F1}s");
        var st = ScoreSongs(songs, load, p, log);
        st.Timings["load"] = tLoad;
        return st;
    }

    /// <summary>Scoring, decision and tree for songs in node order (Index = position, sorted by time value).</summary>
    public static RunState ScoreSongs(Song[] songs, LoadStats load, Params p, TextWriter log)
    {
        var sw = Stopwatch.StartNew();
        Parallel.For(0, songs.Length, new ParallelOptions { MaxDegreeOfParallelism = p.Threads }, i => songs[i].F = Features.Build(songs[i], 0));
        var engine = V8Engine.Build(songs, p);
        double tIndex = sw.Elapsed.TotalSeconds;
        log.WriteLine($"n-grams: {engine.Df.Distinct:N0} distinct, {engine.Df.PostingsCount:N0} song postings, {engine.TotalLanes} lanes, " +
                      $"df cap {p.DfCap}, shifts [{string.Join(", ", engine.Shifts)}] ({tIndex:F1}s)");

        // Every pair (A index < B index) scored exactly: windows of B against the whole of A.
        sw.Restart();
        var recs = new PairRec[songs.Length][];
        var windows = new long[songs.Length];
        int done = 0;
        var progress = Stopwatch.StartNew();
        double lastLog = 0;
        object logLock = new();
        var order = Enumerable.Range(0, songs.Length).Reverse().ToArray();
        Parallel.ForEach(Partitioner.Create(order, EnumerablePartitionerOptions.NoBuffering), new ParallelOptions { MaxDegreeOfParallelism = p.Threads },
            engine.NewWorker, (b, _, w) =>
            {
                recs[b] = engine.ScoreB(b, w);
                int d = Interlocked.Increment(ref done);
                double el = progress.Elapsed.TotalSeconds;
                if (el - lastLog > 30 || d == songs.Length)
                    lock (logLock)
                        if (el - lastLog > 30 || d == songs.Length)
                        {
                            lastLog = el;
                            log.WriteLine($"  scored {d}/{songs.Length} songs, {el:F0}s");
                        }
                return w;
            }, _ => { });
        double tScore = sw.Elapsed.TotalSeconds;
        for (int b = 0; b < songs.Length; b++) windows[b] = engine.Windows(songs[b]).Count;
        var details = new PairDetails(engine, p);
        var st = new RunState { Songs = songs, Engine = engine, Recs = recs, Stored = [], Same = [], Details = details, Load = load };
        st.Windows = windows.Sum();
        st.Timings["features_index"] = tIndex;
        st.Timings["score"] = tScore;   // every pair, all windows and shifts

        sw.Restart();
        Decide(st, p, log);
        st.Timings["decide"] = sw.Elapsed.TotalSeconds;

        sw.Restart();
        st.Tree = Tree.Build(songs, st.Stored.Where(r => r.Significant).ToList(), p);
        st.Timings["tree"] = sw.Elapsed.TotalSeconds;
        log.WriteLine($"tree: {st.Tree.Roots} roots, {st.Tree.Edges.Count(e => e.Kind == "tree")} tree edges, " +
                      $"{st.Tree.Edges.Count(e => e.Kind == "secondary")} secondary edges");
        return st;
    }

    /// <summary>
    /// The decision: the fused-z threshold at <c>TargetFpr</c> from a deterministic sample of time-ordered pairs,
    /// per-channel counting thresholds at <c>CountFpr</c>, the riff threshold for bass-only pairs, the optional
    /// hubness check, versions; then the rows to store.
    /// </summary>
    public static void Decide(RunState st, Params p, TextWriter log)
    {
        var songs = st.Songs;
        int n = songs.Length;
        // Time-ordered pairs and the deterministic null sample (smallest FNV hashes of "a|b").
        var ordered = new List<(ulong H, int A, int B)>();
        for (int b = 0; b < n; b++)
            for (int a = 0; a < b; a++)
            {
                if (st.Earlier(a, b)) ordered.Add((Fnv.Text(songs[a].WorkId + "|" + songs[b].WorkId), a, b));
                else st.Contemporaneous++;
            }
        st.TimeOrdered = ordered.Count;
        ordered.Sort((x, y) => x.H != y.H ? x.H.CompareTo(y.H) : x.A != y.A ? x.A.CompareTo(y.A) : x.B.CompareTo(y.B));
        int ns = Math.Min(p.NullSample, ordered.Count);
        var sample = ordered.Take(ns).ToList();
        foreach (var (_, a, b) in sample) st.SampleSet.Add((a, b));
        double Val(float z) => float.IsNaN(z) ? -1e9 : z;
        st.Sample = sample.Select(t => Val(st.Recs[t.B][t.A].Z)).Order().ToArray();
        st.Fused = Threshold.Of(st.Sample, p.TargetFpr);
        st.CountMinZ = p.CountMinZ;
        st.HubFloor = p.HubSdFloor;
        st.Store = Threshold.Of(st.Sample, p.StoreFpr);
        for (int c = 0; c < Channels.Count; c++)
        {
            var v = sample.Select(t => st.Recs[t.B][t.A].ZMax[c]).Where(x => !float.IsNaN(x)).Select(x => (double)x).Order().ToArray();
            st.Count[Channels.Names[c]] = Threshold.Of(v, p.CountFpr);
        }
        var rv = sample.Select(t => st.Recs[t.B][t.A].ZMax[Ch.Riff]).Where(x => !float.IsNaN(x)).Select(x => (double)x).Order().ToArray();
        st.RiffSample = rv;
        // The riff family is almost never shared by chance (most sample values are exactly 0), so its own quantiles
        // are degenerate; the riff evidence of a bass-only pair must instead clear the fused threshold itself.
        st.Riff = new Threshold(p.RiffFactor * st.Fused.Value, $"{p.RiffFactor:0.##} x the fused threshold", rv.Length, Threshold.Tail(rv, p.RiffFactor * st.Fused.Value));
        log.WriteLine($"threshold: fused z > {st.Fused.Value:F2} ({st.Fused.Method}, {ns} of {ordered.Count:N0} time-ordered pairs, target FPR {p.TargetFpr:g}); " +
                      $"bass-only pairs need riff z > {st.Riff.Value:F2} ({st.Riff.Fpr:g} of the sample's riff values above it); counting: " +
                      string.Join(", ", st.Count.Select(kv => $"{kv.Key} {kv.Value.Value:F1}")));

        // Hubness statistics (two-sided): each B over its earlier songs, each A over its later songs.
        st.HubMeanB = new double[n];
        st.HubSdB = new double[n];
        st.HubMeanA = new double[n];
        st.HubSdA = new double[n];
        var sumA = new double[n];
        var sqA = new double[n];
        var cntA = new int[n];
        for (int b = 0; b < n; b++)
        {
            double s = 0, q = 0;
            int c = 0;
            for (int a = 0; a < b; a++)
            {
                if (!st.Earlier(a, b)) continue;
                double z = Val(st.Recs[b][a].Z);
                if (z <= -1e8) continue;
                s += z;
                q += z * z;
                c++;
                sumA[a] += z;
                sqA[a] += z * z;
                cntA[a]++;
            }
            st.HubMeanB[b] = c > 0 ? s / c : 0;
            st.HubSdB[b] = c > 1 ? Math.Sqrt(Math.Max(0, q / c - st.HubMeanB[b] * st.HubMeanB[b])) : 0;
        }
        for (int a = 0; a < n; a++)
        {
            st.HubMeanA[a] = cntA[a] > 0 ? sumA[a] / cntA[a] : 0;
            st.HubSdA[a] = cntA[a] > 1 ? Math.Sqrt(Math.Max(0, sqA[a] / cntA[a] - st.HubMeanA[a] * st.HubMeanA[a])) : 0;
        }

        // Pairs to store and decide.
        var d = st.Details;
        var worker = st.Engine.NewWorker();
        double T = st.Fused.Value;
        var stored = new List<PairResult>();
        for (int b = 0; b < n; b++)
            for (int a = 0; a < b; a++)
            {
                ref var r = ref st.Recs[b][a];
                bool earlier = st.Earlier(a, b);
                double z = Val(r.Z);
                if (!earlier)
                {
                    if (z > T)
                    {
                        var c = d.From(r, a, b);
                        c.Contemporaneous = true;
                        c.Relation = "contemporaneous";
                        d.VersionMeasures(c);
                        if (c.Version) c.Relation = "version";
                        st.Same.Add(c);
                    }
                    continue;
                }
                bool inSample = st.SampleSet.Contains((a, b));
                if (!(z > T) && !(z > st.Store.Value) && !inSample) continue;
                var pr = d.From(r, a, b);
                pr.InSample = inSample;
                pr.P = Threshold.Tail(st.Sample, z);
                pr.Q = pr.P;
                (pr.HubZA, pr.HubZB) = st.Hub(a, b, z, p.HubSdFloor);
                if (!double.IsNaN(pr.Zc)) SetCounting(st, pr);
                if (z > T)
                {
                    st.AboveThreshold++;
                    DecideOne(st, pr, p, worker);
                }
                stored.Add(pr);
            }
        st.Stored = stored;
        // Display passages and PMI for the edges.
        foreach (var pr in stored.Where(x => x.Significant))
        {
            d.AddSegments(pr);
            d.FillPmi(pr);
        }
        st.Index();
        log.WriteLine($"decide: {st.AboveThreshold} time-ordered pairs above the threshold, {stored.Count(x => x.Significant)} significant, " +
                      $"{st.BassAloneRejected} bass-only rejected ({st.BassAloneRescued} moved to another window), {st.HubRejected} hub-rejected, " +
                      $"{st.VersionsAbove} versions; {st.Same.Count} same-time pairs above it ({st.Same.Count(x => x.Version)} versions); {stored.Count} pair_score rows");
    }

    /// <summary>Counting channels at the winning window, the bass-alone rule, hubness and versions for one pair above the threshold.</summary>
    private static void DecideOne(RunState st, PairResult pr, Params p, V8Worker worker)
    {
        SetCounting(st, pr);
        if (IsBassAlone(pr) && !(pr.RiffZ >= st.Riff.Value))
        {
            pr.BassAlone = true;
            // Look for the best other window above the threshold where the pair is not bass-only.
            var detail = new List<WindowEval>();
            st.Engine.ScoreB(pr.B, worker, pr.A, detail);
            WindowEval? best = null;
            foreach (var ev in detail)
            {
                if (!(ev.Fused > st.Fused.Value)) continue;
                var probe = new PairResult { A = pr.A, B = pr.B };
                Array.Copy(pr.W, probe.W, probe.W.Length);
                st.Details.SetWindow(probe, ev);
                SetCounting(st, probe);
                if (IsBassAlone(probe) && !(probe.RiffZ >= st.Riff.Value)) continue;
                if (best == null || ev.Fused > best.Fused) best = ev;
            }
            if (best == null)
            {
                st.BassAloneRejected++;
                pr.BassAloneRejected = true;
                pr.Relation = "none";
                return;
            }
            st.BassAloneRescued++;
            st.Details.SetWindow(pr, best);
            SetCounting(st, pr);
        }
        var (za, zb) = st.Hub(pr.A, pr.B, pr.Zc, p.HubSdFloor);
        pr.HubZA = za;
        pr.HubZB = zb;
        if (p.HubZ > 0 && Math.Min(za, zb) < p.HubZ)
        {
            pr.HubRejected = true;
            st.HubRejected++;
            return;
        }
        st.Details.VersionMeasures(pr);
        if (pr.Version)
        {
            pr.Relation = "version";
            st.VersionsAbove++;
            return;
        }
        pr.Significant = true;
        pr.Relation = "influence";
    }

    public static void SetCounting(RunState st, PairResult pr)
    {
        for (int c = 0; c < Channels.Count; c++)
            pr.Counting[c] = pr.Avail[c] && pr.Z[c] >= Math.Max(st.Count[Channels.Names[c]].Value, st.CountMinZ);
        // The loop channel is auxiliary: it counts only beside a counting melody, bass, chord or lanes channel.
        if (!(pr.Counting[0] || pr.Counting[1] || pr.Counting[2] || pr.Counting[4])) pr.Counting[3] = false;
    }

    /// <summary>Bass counts at the winning window and no melody, chord or lanes channel does.</summary>
    public static bool IsBassAlone(PairResult pr) =>
        pr.Counts(Channel.Bass) && !pr.Counts(Channel.Melody) && !pr.Counts(Channel.Chord) && !pr.Counts(Channel.Lanes);

    /// <summary><c>retree</c>: credit, ref counts, parents and secondary edges again from the stored significant pairs, then the export.</summary>
    public static int Retree(RunOptions o, Params p, TextWriter log)
    {
        using var conn = PipelineDb.Open(o.Db);
        var songs = PipelineDb.LoadSongs(conn, new LoadStats());
        var idx = songs.ToDictionary(s => s.WorkId, s => s.Index, StringComparer.Ordinal);
        var sig = new List<PairResult>();
        foreach (var sp in PipelineDb.LoadPairs(conn, significantOnly: true))
            if (sp.Relation == "influence" && idx.TryGetValue(sp.A, out int a) && idx.TryGetValue(sp.B, out int b))
                sig.Add(GraphExport.ToResult(sp, a, b, p));
        sig.Sort((x, y) => x.B != y.B ? x.B.CompareTo(y.B) : x.A.CompareTo(y.A));
        var tree = Tree.Build(songs, sig, p);
        PipelineDb.WriteResults(conn, songs, null, tree, null);
        log.WriteLine($"retree: {sig.Count} significant pairs, {tree.Roots} roots, {tree.Edges.Count} edges");
        if (o.Export) GraphExport.Export(conn, o.Graph, RepoRoot.Find(o.Db, o.Root), p, o.Timestamp, log);
        return 0;
    }

    public static int Run(RunOptions o, Params p, TextWriter log)
    {
        var total = Stopwatch.StartNew();
        using var conn = PipelineDb.Open(o.Db);
        var st = Score(conn, p, log);
        var sw = Stopwatch.StartNew();
        var all = st.Stored.Concat(st.Same).OrderBy(r => r.B).ThenBy(r => r.A).ToList();
        PipelineDb.WriteResults(conn, st.Songs, all, st.Tree!, MetaOf(st, p));
        st.Timings["write_pipeline"] = sw.Elapsed.TotalSeconds;
        log.WriteLine($"pipeline db: {all.Count} pair_score rows, {st.Tree!.Edges.Count} influence_edge rows ({sw.Elapsed.TotalSeconds:F1}s)");

        GraphSummary? g = null;
        if (o.Export)
        {
            sw.Restart();
            g = GraphExport.Export(conn, o.Graph, RepoRoot.Find(o.Db, o.Root), p, o.Timestamp, log);
            st.Timings["export"] = sw.Elapsed.TotalSeconds;
        }
        sw.Restart();
        var known = PipelineDb.LoadKnown(conn);
        var report = Report.Build(st, g, known, p, o, log);
        st.Timings["report"] = sw.Elapsed.TotalSeconds;
        st.Timings["total"] = total.Elapsed.TotalSeconds;
        Report.SetTimings(report, st.Timings);
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(o.ReportPath))!);
        File.WriteAllText(o.ReportPath, report.ToJsonString(Report.JsonOptions));
        log.WriteLine($"report -> {o.ReportPath}; total {total.Elapsed.TotalSeconds:F1}s");
        return 0;
    }

    /// <summary>The decision's settings, stored in the pipeline meta (keys influence_*) and copied to graph_meta by export.</summary>
    public static List<(string Key, string Value)> MetaOf(RunState st, Params p)
    {
        string F(double v) => double.IsFinite(v) ? v.ToString("R", CultureInfo.InvariantCulture) : "inf";
        var m = new List<(string, string)>
        {
            ("influence_evidence", "v8 analytic corpus null, rare-only df <= " + p.DfCap.ToString(CultureInfo.InvariantCulture) +
                                   ", 16-bar windows of the later song (hop 4 bars), window max of the weighted Stouffer z"),
            ("influence_df_cap", p.DfCap.ToString(CultureInfo.InvariantCulture)),
            ("influence_key_free_df", p.KeyFreeDf switch
            {
                0 => "0 (exact n-gram df)",
                1 => "1 (weights from the df of the transposition-invariant n-gram)",
                _ => $"{p.KeyFreeDf} (transposition-invariant df; rhythm-coded n-grams of scope {p.ProjScope} weigh by their pitch-only content)",
            }),
            ("influence_line_filter", $"melody n-grams need >= {p.LineMinPc} pitch classes, no pitch-class period 2..{p.FigPeriod} (share {F(p.FigShare)}); " +
                                      $"families dropped mask {p.MelDropFams}, rhythm families mtype5/mtype7 {(p.MelRhythmFams != 0 ? "on" : "off")}"),
            ("influence_lanes", p.WLanes > 0 ? $"on (weight {F(p.WLanes)})" : "off"),
            ("influence_threshold_z", F(st.Fused.Value)),
            ("influence_threshold_method", st.Fused.Method),
            ("influence_target_fpr", F(p.TargetFpr)),
            ("influence_null_sample", st.Fused.Sample.ToString(CultureInfo.InvariantCulture)),
            ("influence_time_ordered_pairs", st.TimeOrdered.ToString(CultureInfo.InvariantCulture)),
            ("influence_riff_threshold_z", F(st.Riff.Value)),
            ("influence_riff_rule", "a pair whose only counting channel is bass needs its riff family alone above the riff threshold"),
            ("influence_channel_weights", $"melody {F(p.WMelody)}, lanes {F(p.WLanes)}, chord {F(p.WChord)}, bass {F(p.WBass)}, loop {F(p.WLoop)}"),
            ("influence_shifts", p.Hedge != 0 ? $"0, +3, -3, +5, -5 (penalty {F(p.ShiftPenalty)})" : "0"),
            ("influence_hub_z", F(p.HubZ)),
            ("influence_null_size_adjust", p.NullSizeAdjust.ToString(CultureInfo.InvariantCulture)),
            ("influence_count_min_z", F(p.CountMinZ)),
        };
        foreach (var kv in st.Count) m.Add(($"influence_count_z_{kv.Key}", F(kv.Value.Value)));
        return m;
    }
}
