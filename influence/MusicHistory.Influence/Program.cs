using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace MusicHistory.Influence;

/// <summary>
/// MusicHistory influence stage (DESIGN.md §8): scores rare shared material between earlier and
/// later songs against a Markov null, roots every song under its most-referenced influencer, and
/// exports the graph database of §10.
/// </summary>
internal static class Program
{
    private const string Usage = """
        MusicHistory.Influence <command> [options]

          run           --db <pipeline.sqlite> --graph <music_graph.db> [--report <json>] [--root <repo>]
                        [--threads N] [--generated-at <iso>] [--no-export] [--param name=value]...
                        Score pairs, write pair_score / influence_edge / tree_node, export the graph DB
                        and data/graph/influence_report.json.
          score-pairs   --db <pipeline.sqlite> --pairs <json | file>  [[a_id, b_id], ...]
                        Force-score pairs (a earlier than b) and print JSON.
          export        --db <pipeline.sqlite> --graph <music_graph.db> [--root <repo>] [--generated-at <iso>]
                        Rewrite the graph DB from the pipeline tables only.
          retree        --db <pipeline.sqlite> --graph <music_graph.db> [--param name=value]...
                        Redo credit, tree and export from the stored pair_score rows (no rescoring).
          make-fixture  --out <db> --songs N [--seed S]
                        Write a synthetic pipeline DB (db.py schema) with planted influence.
        """;

    public static int Main(string[] args)
    {
        CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture;
        CultureInfo.CurrentCulture = CultureInfo.InvariantCulture;
        if (args.Length == 0 || args[0] is "-h" or "--help" or "help")
        {
            Console.Error.WriteLine(Usage);
            return args.Length == 0 ? 2 : 0;
        }
        var log = Console.Error;
        try
        {
            var opts = Parse(args.Skip(1).ToArray());
            var p = new Params();
            if (opts.TryGetValue("threads", out var th)) p.Threads = Math.Max(1, int.Parse(th[0], CultureInfo.InvariantCulture));
            if (opts.TryGetValue("param", out var ps)) foreach (var a in ps) p.Set(a);
            var ro = new RunOptions
            {
                Db = One(opts, "db") ?? ro_Default.Db,
                Graph = One(opts, "graph") ?? ro_Default.Graph,
                Report = One(opts, "report"),
                Root = One(opts, "root"),
                GeneratedAt = One(opts, "generated-at"),
                Export = !opts.ContainsKey("no-export"),
            };
            switch (args[0])
            {
                case "run":
                    return Runner.Run(ro, p, log);
                case "retree":
                    return Runner.Retree(ro, p, log);
                case "export":
                {
                    var sw = Stopwatch.StartNew();
                    using var conn = PipelineDb.Open(ro.Db);
                    GraphExport.Export(conn, ro.Graph, RepoRoot.Find(ro.Db, ro.Root), p, ro.Timestamp, log);
                    log.WriteLine($"export: {sw.Elapsed.TotalSeconds:F1}s");
                    return 0;
                }
                case "score-pairs":
                    return ScorePairs(ro, One(opts, "pairs") ?? throw new ArgumentException("score-pairs needs --pairs"), p, log, opts.ContainsKey("debug"));
                case "make-fixture":
                {
                    string outPath = One(opts, "out") ?? throw new ArgumentException("make-fixture needs --out");
                    int n = int.Parse(One(opts, "songs") ?? "1000", CultureInfo.InvariantCulture);
                    int seed = int.Parse(One(opts, "seed") ?? "7", CultureInfo.InvariantCulture);
                    Fixture.Make(outPath, n, seed, log);
                    return 0;
                }
                case "bench":
                    return Bench.Run(ro, p, int.Parse(One(opts, "pairs") ?? "300", CultureInfo.InvariantCulture), log);
                default:
                    Console.Error.WriteLine($"unknown command '{args[0]}'\n\n{Usage}");
                    return 2;
            }
        }
        catch (Exception e) when (e is ArgumentException or InvalidOperationException or FileNotFoundException or FormatException)
        {
            Console.Error.WriteLine($"error: {e.Message}");
            return 1;
        }
    }

    private static readonly RunOptions ro_Default = new();

    private static Dictionary<string, List<string>> Parse(string[] args)
    {
        var d = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        for (int i = 0; i < args.Length; i++)
        {
            if (!args[i].StartsWith("--", StringComparison.Ordinal)) throw new ArgumentException($"unexpected argument '{args[i]}'");
            string key = args[i][2..];
            if (!d.TryGetValue(key, out var list)) d[key] = list = [];
            if (key is "no-export" or "debug") continue;
            if (i + 1 >= args.Length) throw new ArgumentException($"--{key} needs a value");
            list.Add(args[++i]);
        }
        return d;
    }

    private static string? One(Dictionary<string, List<string>> d, string key) => d.TryGetValue(key, out var v) && v.Count > 0 ? v[^1] : null;

    /// <summary><c>score-pairs</c>: full per-channel detail for given pairs, as JSON on stdout.</summary>
    private static int ScorePairs(RunOptions ro, string pairsArg, Params p, TextWriter log, bool debug)
    {
        string json = pairsArg.TrimStart().StartsWith('[') ? pairsArg : File.ReadAllText(pairsArg);
        p.SkipUnreachable = 0;   // force-score: always run the null, even when the bits gate is out of reach
        var pairs = JsonSerializer.Deserialize<string[][]>(json) ?? [];
        using var conn = PipelineDb.Open(ro.Db);
        var load = new LoadStats();
        var songs = PipelineDb.LoadSongs(conn, load);
        var sc = new NgramScratch();
        foreach (var s in songs) s.F = Features.Build(s, 0, sc, full: true, p.NullDistinct);
        var corpus = Corpus.Build(songs, p);
        var scorer = new PairScorer(p, corpus, songs);
        var idx = songs.ToDictionary(s => s.WorkId, s => s.Index, StringComparer.Ordinal);
        var w = new Worker(songs.Length, p);
        var outArr = new JsonArray();
        foreach (var pr in pairs)
        {
            if (pr.Length < 2) continue;
            var o = new JsonObject { ["a_id"] = pr[0], ["b_id"] = pr[1] };
            outArr.Add(o);
            if (!idx.TryGetValue(pr[0], out int a) || !idx.TryGetValue(pr[1], out int b))
            {
                o["error"] = "not among the selected, analyzed songs";
                continue;
            }
            var sa = songs[a];
            var sb = songs[b];
            o["a_title"] = sa.Title;
            o["b_title"] = sb.Title;
            o["order"] = DateOrder.Earlier(sa.Date, sb.Date) ? "a_before_b" : DateOrder.Earlier(sb.Date, sa.Date) ? "b_before_a" : "contemporaneous";
            w.SetB(sb, corpus, p.EvidenceStopGrams == 0);
            var (early, _) = CandidateGen.For(sb, songs, corpus, w.W, w.AccE, w.AccS, w.Touched, p);
            var cand = early.FirstOrDefault(c => c.A == a);
            o["candidate_rank"] = cand.Bits > 0 ? cand.Rank : null;
            o["candidate_bits"] = cand.Bits > 0 ? Math.Round(cand.Bits, 3) : null;
            if (debug) w.Debug = [];
            var r = scorer.Score(sa, sb, w);
            scorer.FillPmi(r, w);
            if (debug && w.Debug != null)
            {
                var obs = new JsonArray();
                foreach (var (phase, it) in w.Debug.Where(d => d.Phase == "obs"))
                {
                    int g = corpus.Group(it.Hash);
                    obs.Add(new JsonObject
                    {
                        ["kind"] = Features.KindNames[it.Kind], ["b"] = $"{it.Start}-{it.End}", ["bits"] = Math.Round(it.W, 2),
                        ["df"] = g >= 0 ? corpus.Df(g) : 0, ["stop"] = it.Stop,
                    });
                }
                o["debug_observed"] = obs;
                var nullItems = w.Debug.Where(d => d.Phase != "obs").ToList();
                int nSur = Math.Max(1, nullItems.Select(d => d.Phase).Distinct().Count());
                var byKind = new JsonObject();
                foreach (var grp in nullItems.GroupBy(d => Features.KindNames[d.Item.Kind]).OrderBy(g => g.Key, StringComparer.Ordinal))
                    byKind[grp.Key] = new JsonObject
                    {
                        ["items_per_surrogate"] = Math.Round(grp.Count() / (double)nSur, 2),
                        ["bits_per_surrogate"] = Math.Round(grp.Sum(d => d.Item.W) / nSur, 2),
                        ["mean_df"] = Math.Round(grp.Average(d => { int g = corpus.Group(d.Item.Hash); return g >= 0 ? corpus.Df(g) : 0; }), 1),
                    };
                o["debug_null_by_kind"] = byKind;
                w.Debug = null;
            }
            bool bits = r.Excess(Channel.Melody) >= p.MelodyBits || r.Excess(Channel.Bass) >= p.BassBits || r.Excess(Channel.Chord) >= p.ChordBits;
            o["z"] = Num(r.Zc);
            o["p"] = r.P;
            o["s_bits"] = Num(r.S);
            o["pmi"] = Num(r.Pmi);
            o["chord_identity"] = Num(r.ChordId);
            o["duration_ratio"] = Num(r.DurationRatio);
            o["version"] = r.Version;
            o["shift"] = r.Shift;
            o["surrogates"] = r.K;
            o["passes_z_and_bits"] = r.Zc >= p.ZMin && bits;
            var ch = new JsonObject();
            for (int c = 0; c < Channels.Count; c++)
                ch[Channels.Names[c]] = r.Avail[c]
                    ? new JsonObject { ["weight"] = r.W[c], ["e"] = Num(r.E[c]), ["mu"] = Num(r.Mu[c]), ["sigma"] = Num(r.Sigma[c]), ["z"] = Num(r.Z[c]) }
                    : null;
            o["channels"] = ch;
            o["segments"] = JsonNode.Parse(PipelineDb.SegmentsJson(r.Segments));
        }
        Console.Out.WriteLine(outArr.ToJsonString(Report.JsonOptions));
        log.WriteLine($"score-pairs: {outArr.Count} pairs");
        return 0;
    }

    private static JsonNode? Num(double v) => double.IsNaN(v) || double.IsInfinity(v) ? null : JsonValue.Create(Math.Round(v, 4));
}
