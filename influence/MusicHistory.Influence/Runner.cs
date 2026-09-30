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

/// <summary>Everything the scoring pass produced, kept for the report.</summary>
internal sealed class RunState
{
    public required Song[] Songs;
    public required Corpus Corpus;
    public required PairScorer Scorer;
    public required List<PairResult> Tested;          // A earlier than B, scored, in (B, A) order
    public required List<PairResult> Same;            // contemporaneous version checks
    public required List<Candidate>[] Candidates;     // per B
    public TreeResult? Tree;
    public LoadStats Load = new();
    public readonly Dictionary<string, double> Timings = [];
    public long Cells, Surrogates, SurrogatesAligned, Confirmed;
}

/// <summary>The <c>run</c> subcommand: DESIGN.md §8 steps 1-12, then the graph export and report.</summary>
internal static class Runner
{
    public static RunState Score(Microsoft.Data.Sqlite.SqliteConnection conn, Params p, TextWriter log)
    {
        var sw = Stopwatch.StartNew();
        var load = new LoadStats();
        var songs = PipelineDb.LoadSongs(conn, load);
        if (songs.Length == 0) throw new InvalidOperationException("no selected, analyzed songs with a year in the pipeline database");
        double tLoad = sw.Elapsed.TotalSeconds;
        log.WriteLine($"load: {songs.Length} songs ({load.SkippedNoYear} without a year skipped; {load.NoMelody} without melody, " +
                      $"{load.NoBass} without bass, {load.NoChords} without chords, {load.NoLoops} without loops) in {tLoad:F1}s");

        sw.Restart();
        Parallel.For(0, songs.Length, new ParallelOptions { MaxDegreeOfParallelism = p.Threads },
            () => new NgramScratch(), (i, _, sc) =>
            {
                songs[i].F = Features.Build(songs[i], 0, sc, full: true, p.NullDistinct);
                return sc;
            }, _ => { });
        double tFeat = sw.Elapsed.TotalSeconds;
        sw.Restart();
        var corpus = Corpus.Build(songs, p);
        double tCorpus = sw.Elapsed.TotalSeconds;
        log.WriteLine($"n-grams: {corpus.DistinctCount:N0} distinct, {corpus.Postings:N0} song postings, stop-gram df > {corpus.StopDf} " +
                      $"(features {tFeat:F1}s, index {tCorpus:F1}s)");

        var scorer = new PairScorer(p, corpus, songs);
        var perB = new List<PairResult>[songs.Length];
        var sameB = new List<PairResult>[songs.Length];
        var cands = new List<Candidate>[songs.Length];
        var candTime = new double[songs.Length];
        var workers = new ConcurrentBag<Worker>();
        int done = 0;
        var progress = Stopwatch.StartNew();
        double lastLog = 0;
        object logLock = new();
        sw.Restart();
        var order = Enumerable.Range(0, songs.Length).Reverse().ToArray();   // later songs have more candidates
        Parallel.ForEach(System.Collections.Concurrent.Partitioner.Create(order, EnumerablePartitionerOptions.NoBuffering),
            new ParallelOptions { MaxDegreeOfParallelism = p.Threads },
            () => new Worker(songs.Length, p),
            (bi, _, w) =>
            {
                var b = songs[bi];
                w.SetB(b, corpus, p.EvidenceStopGrams == 0);
                long t0 = Stopwatch.GetTimestamp();
                var (early, same) = CandidateGen.For(b, songs, corpus, w.W, w.AccE, w.AccS, w.Touched, p);
                candTime[bi] = Stopwatch.GetElapsedTime(t0).TotalSeconds;
                cands[bi] = early;
                var list = new List<PairResult>(early.Count);
                foreach (var c in early)
                {
                    var r = scorer.Score(songs[c.A], b, w);
                    r.CandBits = c.Bits;
                    r.CandRank = c.Rank;
                    list.Add(r);
                }
                perB[bi] = list;
                var sl = new List<PairResult>();
                foreach (var c in same)
                {
                    var r = scorer.ScoreContemporaneous(songs[c.A], b, w);
                    r.CandBits = c.Bits;
                    r.CandRank = c.Rank;
                    sl.Add(r);
                }
                sameB[bi] = sl;
                int d = Interlocked.Increment(ref done);
                double el = progress.Elapsed.TotalSeconds;
                if (el - lastLog > 30 || d == songs.Length)
                    lock (logLock)
                    {
                        if (el - lastLog > 30 || d == songs.Length)
                        {
                            lastLog = el;
                            log.WriteLine($"  scored {d}/{songs.Length} songs, {el:F0}s");
                        }
                    }
                return w;
            },
            w => workers.Add(w));
        double tScore = sw.Elapsed.TotalSeconds;

        var tested = new List<PairResult>();
        var sameAll = new List<PairResult>();
        for (int b = 0; b < songs.Length; b++)
        {
            tested.AddRange(perB[b].Where(r => r.Tested));
            sameAll.AddRange(sameB[b]);
        }
        var st = new RunState { Songs = songs, Corpus = corpus, Scorer = scorer, Tested = tested, Same = sameAll, Candidates = cands, Load = load };
        foreach (var w in workers)
        {
            st.Cells += w.Aligner.Cells;
            st.Surrogates += w.Surrogates;
            st.SurrogatesAligned += w.SurrogatesAligned;
            st.Confirmed += w.PairsConfirmed;
        }
        st.Timings["load"] = tLoad;
        st.Timings["features"] = tFeat;
        st.Timings["index"] = tCorpus;
        st.Timings["candidates_cpu"] = candTime.Sum();
        st.Timings["score"] = tScore;
        log.WriteLine($"score: {tested.Count:N0} pairs tested ({st.Confirmed:N0} confirmed with K={p.KConfirm}), {sameAll.Count} same-time checks, " +
                      $"{st.Cells / 1e9:F2} G cells, {st.Surrogates:N0} surrogates ({st.SurrogatesAligned:N0} aligned) in {tScore:F1}s");

        // 8. Benjamini-Hochberg over every tested pair, significance, 9. versions.
        sw.Restart();
        var q = Stats.BenjaminiHochberg(tested.Select(r => r.P).ToList());
        for (int i = 0; i < tested.Count; i++)
        {
            var r = tested[i];
            r.Q = q[i];
            bool bits = r.Excess(Channel.Melody) >= p.MelodyBits || r.Excess(Channel.Bass) >= p.BassBits || r.Excess(Channel.Chord) >= p.ChordBits;
            bool sig = r.Zc >= p.ZMin && r.Q <= p.QMax && bits;
            r.Relation = r.Version ? "version" : sig ? "influence" : "none";
            r.Significant = sig && !r.Version;
        }
        var needPmi = tested.Where(r => r.Significant && double.IsNaN(r.Pmi)).ToList();
        Parallel.ForEach(needPmi, new ParallelOptions { MaxDegreeOfParallelism = p.Threads }, () => new Worker(songs.Length, p),
            (r, _, w) =>
            {
                scorer.FillPmi(r, w);
                return w;
            }, _ => { });
        st.Timings["decide"] = sw.Elapsed.TotalSeconds;
        log.WriteLine($"decide: {tested.Count(r => r.Significant)} significant, {tested.Count(r => r.Version) + sameAll.Count(r => r.Version)} versions " +
                      $"({sw.Elapsed.TotalSeconds:F1}s)");

        // 10. credit and tree.
        sw.Restart();
        st.Tree = Tree.Build(songs, tested.Where(r => r.Significant).ToList(), p);
        st.Timings["tree"] = sw.Elapsed.TotalSeconds;
        log.WriteLine($"tree: {st.Tree.Roots} roots, {st.Tree.Edges.Count(e => e.Kind == "tree")} tree edges, " +
                      $"{st.Tree.Edges.Count(e => e.Kind == "secondary")} secondary edges");
        return st;
    }

    /// <summary>
    /// <c>retree</c>: steps 10-11 again from the stored significant pairs (credit, ref counts,
    /// parents, secondary edges), then the export -- for trying tree rules without rescoring.
    /// </summary>
    public static int Retree(RunOptions o, Params p, TextWriter log)
    {
        using var conn = PipelineDb.Open(o.Db);
        var songs = PipelineDb.LoadSongs(conn, new LoadStats());
        var idx = songs.ToDictionary(s => s.WorkId, s => s.Index, StringComparer.Ordinal);
        var sig = new List<PairResult>();
        foreach (var sp in PipelineDb.LoadPairs(conn, significantOnly: true))
            if (sp.Relation == "influence" && idx.TryGetValue(sp.A, out int a) && idx.TryGetValue(sp.B, out int b))
                sig.Add(GraphExport.ToResult(sp, songs[a], songs[b], a, b, p));
        sig.Sort((x, y) => x.B != y.B ? x.B.CompareTo(y.B) : x.A.CompareTo(y.A));
        var tree = Tree.Build(songs, sig, p);
        PipelineDb.WriteResults(conn, songs, null, tree);
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
        var all = st.Tested.Concat(st.Same).OrderBy(r => r.B).ThenBy(r => r.A).ToList();
        PipelineDb.WriteResults(conn, st.Songs, all, st.Tree!);
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
}
