using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace MusicHistory.Influence;

/// <summary>
/// MusicHistory influence stage (DESIGN.md §8): scores rare shared material between every earlier and later
/// song (V8: analytic corpus null over rare n-grams, window max), decides with an empirical threshold, roots
/// every song under its most-referenced strong influencer, and exports the graph database of §10.
/// </summary>
internal static class Program
{
    private const string Usage = """
        MusicHistory.Influence <command> [options]

          run           --db <pipeline.sqlite> --graph <music_graph.db> [--mode lineage|evidence] [--report <json>]
                        [--root <repo>] [--threads N] [--generated-at <iso>] [--no-export] [--param name=value]...
                        Score every time-ordered pair, write pair_score / influence_edge / tree_node and the
                        influence_* meta keys, export the graph DB and data/graph/influence_report.json.
                        --mode lineage (default): edges are shared identities (DESIGN.md 8b: loop families, named
                        schemas, progression schemas, strong matches); --mode evidence: the strict v2 graph.
          score-pairs   --db <pipeline.sqlite> --pairs <json | file> [--debug]  [[a_id, b_id], ...]
                        Score pairs (every window of the later song) and print JSON; --debug lists the shared
                        rare n-grams of the winning window.
          evaluate      --benchmark <dir> [--db <pipeline.sqlite>] [--tune] [--threshold-z Z] [--param name=value]...
                        Score the calibration benchmark (pairs.json) and print TPR at window FPR 1e-3 per
                        channel and fused, next to the Python V8 when its score files are present.
          export        --db <pipeline.sqlite> --graph <music_graph.db> [--root <repo>] [--generated-at <iso>]
                        Rewrite the graph DB from the pipeline tables only (the graph the last run stored).
          retree        --db <pipeline.sqlite> --graph <music_graph.db> [--mode lineage|evidence] [--param name=value]...
                        Redo credit, tree and export without rescoring: from the stored lineage families, or
                        (evidence) from the stored pair_score rows. Default: the semantics of the last run.
          make-fixture  --out <db> --songs N [--seed S]
                        Write a synthetic pipeline DB (db.py schema) with planted influence.
          bench         --db <pipeline.sqlite> [--pairs N]
                        Time the V8 engine on N later songs of a pipeline DB.
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
            var lp = new LineageParams();
            if (opts.TryGetValue("threads", out var th)) p.Threads = Math.Max(1, int.Parse(th[0], CultureInfo.InvariantCulture));
            if (opts.TryGetValue("param", out var ps))
                foreach (var a in ps)
                {
                    int eq = a.IndexOf('=');
                    if (eq > 0 && LineageParams.Has(a[..eq].Trim())) lp.Set(a);
                    else p.Set(a);
                }
            string? mode = One(opts, "mode");
            if (mode != null && mode != "lineage" && mode != "evidence") throw new ArgumentException($"--mode must be 'lineage' or 'evidence', got '{mode}'");
            var ro = new RunOptions
            {
                Db = One(opts, "db") ?? DefaultOptions.Db,
                Graph = One(opts, "graph") ?? DefaultOptions.Graph,
                Report = One(opts, "report"),
                Root = One(opts, "root"),
                GeneratedAt = One(opts, "generated-at"),
                Export = !opts.ContainsKey("no-export"),
            };
            switch (args[0])
            {
                case "run":
                    return mode == "evidence" ? Runner.Run(ro, p, log) : LineageRunner.Run(ro, p, lp, log);
                case "retree":
                    return (mode ?? StoredMode(ro.Db)) == "lineage" ? LineageRunner.Retree(ro, p, lp, log) : Runner.Retree(ro, p, log);
                case "export":
                {
                    var sw = Stopwatch.StartNew();
                    using var conn = PipelineDb.Open(ro.Db);
                    if (StoredMode(conn) == "lineage") LineageExport.Export(conn, ro.Graph, RepoRoot.Find(ro.Db, ro.Root), p, lp, ro.Timestamp, log);
                    else GraphExport.Export(conn, ro.Graph, RepoRoot.Find(ro.Db, ro.Root), p, ro.Timestamp, log);
                    log.WriteLine($"export: {sw.Elapsed.TotalSeconds:F1}s");
                    return 0;
                }
                case "score-pairs":
                    return ScorePairs(ro, One(opts, "pairs") ?? throw new ArgumentException("score-pairs needs --pairs"), p, log, opts.ContainsKey("debug"));
                case "evaluate":
                    return BenchmarkEval.Run(One(opts, "benchmark") ?? throw new ArgumentException("evaluate needs --benchmark <dir>"), p, log,
                        opts.ContainsKey("tune"), One(opts, "threshold-z") is { } tz ? double.Parse(tz, CultureInfo.InvariantCulture) : double.NaN,
                        One(opts, "db"));
                case "make-fixture":
                {
                    string outPath = One(opts, "out") ?? throw new ArgumentException("make-fixture needs --out");
                    int n = int.Parse(One(opts, "songs") ?? "1000", CultureInfo.InvariantCulture);
                    int seed = int.Parse(One(opts, "seed") ?? "7", CultureInfo.InvariantCulture);
                    Fixture.Make(outPath, n, seed, log);
                    return 0;
                }
                case "bench":
                    return Bench.Run(ro, p, int.Parse(One(opts, "pairs") ?? "50", CultureInfo.InvariantCulture), log);
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

    private static readonly RunOptions DefaultOptions = new();

    private static Dictionary<string, List<string>> Parse(string[] args)
    {
        var d = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        for (int i = 0; i < args.Length; i++)
        {
            if (!args[i].StartsWith("--", StringComparison.Ordinal)) throw new ArgumentException($"unexpected argument '{args[i]}'");
            string key = args[i][2..];
            if (!d.TryGetValue(key, out var list)) d[key] = list = [];
            if (key is "no-export" or "debug" or "tune") continue;
            if (i + 1 >= args.Length) throw new ArgumentException($"--{key} needs a value");
            list.Add(args[++i]);
        }
        return d;
    }

    private static string? One(Dictionary<string, List<string>> d, string key) => d.TryGetValue(key, out var v) && v.Count > 0 ? v[^1] : null;

    /// <summary>The semantics of the last run stored in the pipeline (meta influence_edge_semantics): "lineage" or "evidence".</summary>
    internal static string StoredMode(Microsoft.Data.Sqlite.SqliteConnection conn) =>
        PipelineDb.Meta(conn, LineageStore.MetaSemantics) == LineageStore.Lineage ? "lineage" : "evidence";

    private static string StoredMode(string db)
    {
        using var conn = PipelineDb.Open(db);
        return StoredMode(conn);
    }

    /// <summary><c>score-pairs</c>: every window of the later song against the earlier one, as JSON on stdout.</summary>
    private static int ScorePairs(RunOptions ro, string pairsArg, Params p, TextWriter log, bool debug)
    {
        string json = pairsArg.TrimStart().StartsWith('[') ? pairsArg : File.ReadAllText(pairsArg);
        var pairs = JsonSerializer.Deserialize<string[][]>(json) ?? [];
        using var conn = PipelineDb.Open(ro.Db);
        var songs = PipelineDb.LoadSongs(conn, new LoadStats());
        foreach (var s in songs) s.F = Features.Build(s, 0);
        var engine = V8Engine.Build(songs, p);
        var details = new PairDetails(engine, p);
        var w = engine.NewWorker();
        double? threshold = double.TryParse(PipelineDb.Meta(conn, "influence_threshold_z"), NumberStyles.Float, CultureInfo.InvariantCulture, out double t) ? t : null;
        var idx = songs.ToDictionary(s => s.WorkId, s => s.Index, StringComparer.Ordinal);
        var outArr = new JsonArray();
        foreach (var pr in pairs)
        {
            if (pr.Length < 2) continue;
            var o = new JsonObject { ["a_id"] = pr[0], ["b_id"] = pr[1] };
            outArr.Add(o);
            if (!idx.TryGetValue(pr[0], out int a) || !idx.TryGetValue(pr[1], out int b) || a == b)
            {
                o["error"] = "not among the selected, analyzed songs";
                continue;
            }
            var sa = songs[a];
            var sb = songs[b];
            o["a_title"] = sa.Title;
            o["b_title"] = sb.Title;
            o["order"] = DateOrder.Earlier(sa.Date, sb.Date) ? "a_before_b" : DateOrder.Earlier(sb.Date, sa.Date) ? "b_before_a" : "contemporaneous";
            (int x, int y) = a < b ? (a, b) : (b, a);
            o["scored_as"] = $"{songs[x].WorkId} -> {songs[y].WorkId}";
            var detail = new List<WindowEval>();
            var recs = engine.ScoreB(y, w, x, detail);
            var res = details.From(recs[x], x, y);
            o["z"] = Num(res.Zc);
            o["threshold_z"] = threshold is double tz ? Num(tz) : null;
            o["above_threshold"] = threshold is double tz2 ? res.Zc > tz2 : null;
            o["s_bits"] = Num(res.S);
            o["shift"] = res.Shift;
            o["window_beats"] = double.IsNaN(res.WinStart) ? null : new JsonArray(Num(res.WinStart), Num(res.WinEnd));
            o["riff_z"] = Num(res.RiffZ);
            o["riff_z_window_max"] = Num(res.RiffMax);
            var ch = new JsonObject();
            for (int c = 0; c < Channels.Count; c++)
                ch[Channels.Names[c]] = res.Avail[c] || !double.IsNaN(res.ZMax[c])
                    ? new JsonObject
                    {
                        ["weight"] = res.W[c], ["s"] = res.Avail[c] ? Num(res.E[c]) : null, ["mu"] = res.Avail[c] ? Num(res.Mu[c]) : null,
                        ["sigma"] = res.Avail[c] ? Num(res.Sigma[c]) : null, ["z"] = Num(res.Z[c]), ["z_window_max"] = Num(res.ZMax[c]),
                    }
                    : null;
            o["channels"] = ch;
            if (res.LaneSrc >= 0)
            {
                o["lanes_match"] = res.LaneSrc == 0
                    ? $"later lead vs earlier lane #{res.LaneIdx}"
                    : $"later lane #{res.LaneSrc - 1} vs earlier lead";
                o["lane_role"] = LaneRole(engine, res.LaneSrc == 0 ? x : y, res.LaneSrc == 0 ? res.LaneIdx : res.LaneSrc - 1);
            }
            o["windows"] = detail.Count;
            o["windows_above_half"] = detail.Count(d => !double.IsNaN(d.Fused) && d.Fused >= 0.5 * res.Zc);
            o["best_windows"] = new JsonArray(detail.OrderByDescending(d => d.Fused).ThenBy(d => d.Window).Take(5).Select(d => (JsonNode)new JsonObject
            {
                ["beats"] = new JsonArray(Num(d.T0), Num(d.T1)), ["fused_z"] = Num(d.Fused), ["shift"] = d.Shift,
                ["z"] = new JsonObject(Enumerable.Range(0, Ch.N).Where(c => (d.Avail & (1 << c)) != 0)
                    .Select(c => KeyValuePair.Create(Ch.Names[c], Num(d.Z[c])))),
            }).ToArray());
            if (debug)
            {
                o["shared_rare_ngrams"] = SharedGrams(engine, x, y, res);
                // How unique the earlier song is for the winning window: every earlier song scored on that window alone.
                if (res.Window >= 0)
                {
                    var atWin = engine.ScoreB(y, w, -1, null, res.Window);
                    var others = Enumerable.Range(0, y).Where(a => a != x && DateOrder.Earlier(songs[a].Date, songs[y].Date) && !float.IsNaN(atWin[a].Z))
                        .Select(a => (A: a, Z: (double)atWin[a].Z)).OrderByDescending(t => t.Z).ToList();
                    double zx = atWin[x].Z;
                    o["window_z"] = Num(zx);
                    o["window_rank"] = 1 + others.Count(t => t.Z > zx);
                    o["window_others_half"] = others.Count(t => t.Z >= 0.5 * zx);
                    o["window_runner_up"] = others.Count > 0
                        ? new JsonObject { ["work_id"] = songs[others[0].A].WorkId, ["title"] = songs[others[0].A].Title, ["z"] = Num(others[0].Z) }
                        : null;
                }
            }
        }
        Console.Out.WriteLine(outArr.ToJsonString(Report.JsonOptions));
        log.WriteLine($"score-pairs: {outArr.Count} pairs");
        return 0;
    }

    /// <summary>The <c>melody_line</c> role of a song's usable lane (index into <see cref="SongV8.Lanes"/>).</summary>
    private static string? LaneRole(V8Engine e, int song, int usable)
    {
        var d = e.Data[song];
        var s = e.Songs[song];
        if (usable < 0 || usable >= d.LaneRows.Length) return null;
        int row = d.LaneRows[usable];
        return row >= 0 && row < s.LaneRoles.Length ? s.LaneRoles[row] : null;
    }

    /// <summary>
    /// The rare n-grams of the winning window that the earlier song contains, with their weight (score-pairs --debug):
    /// channel, family, df, pair df, bits, the n-gram's first and last onset in B (<c>b_beat</c>, <c>b_end_beat</c>), the
    /// line of each song it was matched in (<c>b_line</c> / <c>a_line</c>: melody, bass, chords or a lane role) and up to
    /// four of its occurrences in A (<c>a_beats</c>: [first onset, last onset] each), so the matched material can be
    /// printed side by side. The lanes channel lists the lane match of the winning window.
    /// </summary>
    internal static JsonArray SharedGrams(V8Engine e, int a, int b, PairResult res)
    {
        var arr = new JsonArray();
        if (res.Window < 0) return arr;
        var wins = e.Windows(e.Songs[b]);
        if (res.Window >= wins.Count) return arr;
        var win = wins[res.Window];
        var sa = e.Songs[a];
        var sb = e.Songs[b];
        double fdb = sb.FirstDownbeat, bpb = sb.BeatsPerBar > 0 ? sb.BeatsPerBar : 4.0;
        double fdbA = sa.FirstDownbeat, bpbA = sa.BeatsPerBar > 0 ? sa.BeatsPerBar : 4.0;
        var docA = e.Data[a].DocSet;
        var docB = e.Data[b].DocSet;

        // A's occurrences (whole line or chords, never shifted) of every n-gram key.
        Dictionary<long, List<(double S, double E)>> LineOcc(NoteLine? line, bool melody)
        {
            var map = new Dictionary<long, List<(double, double)>>();
            if (line == null) return map;
            var o = new List<GramOcc>();
            Grams.Line(melody, line.Onsets, line.Pitches, fdbA, bpbA, 0, false, o, melody ? e.MelFilter : default);
            foreach (var g in o)
            {
                if (!map.TryGetValue(g.Key, out var l)) map[g.Key] = l = [];
                l.Add((line.Onsets[g.Start], line.Onsets[g.End]));
            }
            return map;
        }

        void Emit(string ch, string bLine, string aLine, List<GramOcc> occ, Func<GramOcc, bool> inGroup, HashSet64? target,
            Func<int, double> beat, Dictionary<long, List<(double S, double E)>> aOcc)
        {
            if (target == null) return;
            var seen = new HashSet<long>();
            foreach (var o in occ)
            {
                if (!inGroup(o) || !seen.Add(o.Key)) continue;
                ulong u = unchecked((ulong)o.Key);
                if (!target.Contains(u)) continue;
                int df = e.Df.Df(o.Key);
                int d = e.Wt.Dfp(df, (docA.Contains(u) ? 1 : 0) + (docB.Contains(u) ? 1 : 0));
                if (e.P.KeyFreeDf != 0 && o.Canon != o.Key)
                {
                    ulong cu = unchecked((ulong)o.Canon);
                    d = e.Wt.Dfp(e.Df.Df(o.Canon), (docA.Contains(cu) ? 1 : 0) + (docB.Contains(cu) ? 1 : 0));
                }
                int scope = ch == "chord" ? 1 : ch == "bass" ? 4 : 2;
                if (e.P.KeyFreeDf >= 2 && (e.P.ProjScope & scope) != 0 && o.Proj != 0 && o.Proj != o.Canon)
                {
                    ulong pu = unchecked((ulong)o.Proj);
                    d = Math.Max(d, e.Wt.Dfp(e.Df.Df(o.Proj), (docA.Contains(pu) ? 1 : 0) + (docB.Contains(pu) ? 1 : 0)));
                }
                var at = new JsonArray();
                if (aOcc.TryGetValue(o.Key, out var l))
                    foreach (var (s0, e0) in l.Take(4)) at.Add(new JsonArray(Num(s0), Num(e0)));
                arr.Add(new JsonObject
                {
                    ["channel"] = ch, ["family"] = Grams.FamNames[(int)o.Fam], ["df"] = df, ["pair_df"] = d, ["bits"] = Num(e.Wt.W[d]),
                    ["b_beat"] = Num(beat(o.Start)), ["b_end_beat"] = Num(beat(o.End)), ["npc"] = o.Npc,
                    ["b_line"] = bLine, ["a_line"] = aLine, ["a_beats"] = at,
                });
            }
        }

        var occ = new List<GramOcc>();
        if (win.Mel is { } mv)
        {
            Grams.Line(true, mv.On, mv.Pitch, fdb, bpb, res.Shift, false, occ, e.MelFilter);
            Emit("melody", "melody", "melody", occ, o => Grams.InGroup(o, Grp.Mel), e.Data[a].Mel, i => mv.On[i], LineOcc(sa.Melody, true));
            // Lanes (i): the later lead window against the earlier song's lane of the winning window.
            if (res.LaneSrc == 0 && res.LaneIdx < e.Data[a].Lanes.Length)
            {
                int row = e.Data[a].LaneRows[res.LaneIdx];
                Emit("lanes", "melody", LaneRole(e, a, res.LaneIdx) ?? "lane", occ, o => Grams.InGroup(o, Grp.Mel), e.Data[a].Lanes[res.LaneIdx],
                    i => mv.On[i], LineOcc(sa.Lanes[row], true));
            }
        }
        // Lanes (ii): a later lane in the window against the earlier lead line.
        if (res.LaneSrc >= 1 && res.LaneSrc - 1 < win.Lanes.Length && win.Lanes[res.LaneSrc - 1] is { } lv)
        {
            occ.Clear();
            Grams.Line(true, lv.On, lv.Pitch, fdb, bpb, res.Shift, false, occ, e.MelFilter);
            Emit("lanes", LaneRole(e, b, res.LaneSrc - 1) ?? "lane", "melody", occ, o => Grams.InGroup(o, Grp.Mel), e.Data[a].Mel, i => lv.On[i],
                LineOcc(sa.Melody, true));
        }
        if (win.Bass is { } bv)
        {
            occ.Clear();
            Grams.Line(false, bv.On, bv.Pitch, fdb, bpb, res.Shift, false, occ);
            Emit("bass", "bass", "bass", occ, o => Grams.InGroup(o, Grp.BassNpc2) || Grams.InGroup(o, Grp.Riff), e.Data[a].Bass, i => bv.On[i],
                LineOcc(sa.Bass, false));
        }
        if (win.Chord is { } cv)
        {
            occ.Clear();
            Grams.Chords(cv.Tok, cv.Start, cv.Dur, fdb, bpb, res.Shift, false, occ);
            var aOcc = new Dictionary<long, List<(double S, double E)>>();
            if (sa.Chords is { } ac)
            {
                var o2 = new List<GramOcc>();
                Grams.Chords(ac.Tokens, ac.Starts, ac.Durs, fdbA, bpbA, 0, false, o2);
                foreach (var g in o2)
                {
                    if (!aOcc.TryGetValue(g.Key, out var l)) aOcc[g.Key] = l = [];
                    l.Add((ac.Starts[g.Start], ac.Starts[g.End]));
                }
            }
            Emit("chord", "chords", "chords", occ, o => Grams.InGroup(o, Grp.Chord), e.Data[a].Chord, i => cv.Start[i], aOcc);
        }
        return arr;
    }

    private static JsonNode? Num(double v) => double.IsNaN(v) || double.IsInfinity(v) ? null : JsonValue.Create(Math.Round(v, 4));
}
