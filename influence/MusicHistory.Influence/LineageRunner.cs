using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Influence;

/// <summary>
/// <c>run --mode lineage</c> (the default) and its <c>retree</c> / <c>export</c>: the strict evidence is scored exactly
/// as in <c>--mode evidence</c> (its significant pairs are the strong matches), then families, scores, the user's tree
/// rule, the graph with its identity tables and the report (DESIGN.md §8b).
/// </summary>
internal static class LineageRunner
{
    /// <summary>The minor tonic of the normalized frame: A (9) under relative normalization, C (0) under parallel.</summary>
    public static int MinorTonic(SqliteConnection conn, Song[] songs) =>
        ExportSettings.Resolve(conn, songs.Select(s => s.WorkId).ToList(), _ => null).Normalization == "parallel" ? 0 : 9;

    /// <summary>The tree variants the report compares (the degenerate-structure guard).</summary>
    public static readonly (string Name, TreeRule Rule)[] Variants =
    [
        ("membership only: every shared family counts fully, credit ties to the earliest", new TreeRule(false, false, false, false)),
        ("design score (phase/rhythm agreement, strength), credit ties to the earliest", new TreeRule(true, true, false, false)),
        ("design score, credit ties to the closest version", new TreeRule(true, true, true, false)),
        ("final: finer agreement in the score (design agreement x closeness: chord qualities, rhythm, loop length, strength), " +
         "credit ties to the closest version, then the earliest", TreeRule.Final),
    ];

    public static int Run(RunOptions o, Params p, LineageParams lp, TextWriter log)
    {
        var total = Stopwatch.StartNew();
        using var conn = PipelineDb.Open(o.Db);
        var st = Runner.Score(conn, p, log);
        var sw = Stopwatch.StartNew();
        int minorTonic = MinorTonic(conn, st.Songs);
        PipelineDb.LoadChordsL2(conn, st.Songs);
        var strong = st.Stored.Where(r => r.Significant).ToList();
        var spans = new Dictionary<(int, int), StrongSpan>();
        foreach (var pr in strong)
            if (Families.StrongSpans(st.Engine, pr) is { } sp) spans[(pr.A, pr.B)] = sp;
        var fams = Families.Build(st.Songs, strong, lp, minorTonic, spans);
        st.Timings["lineage_families"] = sw.Elapsed.TotalSeconds;
        LogFamilies(fams, st.Songs.Length, log);
        sw.Restart();
        var res = LineageTree.Build(st.Songs, fams, p, lp);
        var variants = Variants.Select(v => (v.Name, R: v.Rule == TreeRule.Final ? res : LineageTree.Build(st.Songs, fams, p, lp, v.Rule))).ToList();
        st.Timings["lineage_tree"] = sw.Elapsed.TotalSeconds;
        LogTree(res, log);

        sw.Restart();
        var all = st.Stored.Concat(st.Same).OrderBy(r => r.B).ThenBy(r => r.A).ToList();
        LineageStore.Ensure(conn);
        PipelineDb.WriteResults(conn, st.Songs, all, LineageStore.AsTree(res), [.. Runner.MetaOf(st, p), .. MetaOf(res, lp, minorTonic)],
            tx => LineageStore.Write(conn, tx, res));
        st.Timings["write_pipeline"] = sw.Elapsed.TotalSeconds;
        log.WriteLine($"pipeline db: {all.Count} pair_score rows, {res.Edges.Count} influence_edge rows, {fams.Count} lineage families ({sw.Elapsed.TotalSeconds:F1}s)");

        LineageGraphSummary? g = null;
        if (o.Export)
        {
            sw.Restart();
            g = LineageExport.Export(conn, o.Graph, RepoRoot.Find(o.Db, o.Root), p, lp, o.Timestamp, log);
            st.Timings["export"] = sw.Elapsed.TotalSeconds;
        }
        sw.Restart();
        var known = PipelineDb.LoadKnown(conn);
        var report = LineageReport.Build(st, res, variants, g, known, p, lp, o, log);
        st.Timings["report"] = sw.Elapsed.TotalSeconds;
        st.Timings["total"] = total.Elapsed.TotalSeconds;
        Report.SetTimings(report, st.Timings);
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(o.ReportPath))!);
        File.WriteAllText(o.ReportPath, report.ToJsonString(Report.JsonOptions));
        log.WriteLine($"report -> {o.ReportPath}; total {total.Elapsed.TotalSeconds:F1}s");
        return 0;
    }

    /// <summary><c>retree</c> in lineage mode: the tree again from the stored families (no rescoring), then the export.</summary>
    public static int Retree(RunOptions o, Params p, LineageParams lp, TextWriter log)
    {
        using var conn = PipelineDb.Open(o.Db);
        LineageStore.Ensure(conn);
        var songs = PipelineDb.LoadSongs(conn, new LoadStats());
        var fams = LineageStore.LoadFamilies(conn, songs);
        if (fams.Count == 0) throw new InvalidOperationException("no lineage families stored: run 'run --mode lineage' first");
        int minorTonic = MinorTonic(conn, songs);
        var res = LineageTree.Build(songs, fams, p, lp);
        // Keep the run's settings, replace the lineage keys (the parameters may differ).
        var meta = GraphExport.InfluenceMeta(pipeline: conn).Where(kv => !kv.Item1.StartsWith("influence_lineage", StringComparison.Ordinal)
                && kv.Item1 != LineageStore.MetaSemantics).Select(kv => (kv.Item1, kv.Item2 ?? "")).ToList();
        meta.AddRange(MetaOf(res, lp, minorTonic));
        PipelineDb.WriteResults(conn, songs, null, LineageStore.AsTree(res), meta, tx => LineageStore.Write(conn, tx, res));
        LogTree(res, log);
        if (o.Export) LineageExport.Export(conn, o.Graph, RepoRoot.Find(o.Db, o.Root), p, lp, o.Timestamp, log);
        return 0;
    }

    /// <summary>The lineage settings, stored in the pipeline meta (influence_*) and copied to graph_meta by export.</summary>
    public static List<(string Key, string Value)> MetaOf(LineageResult r, LineageParams lp, int minorTonic)
    {
        string F(double v) => v.ToString("R", CultureInfo.InvariantCulture);
        return
        [
            (LineageStore.MetaSemantics, LineageStore.Lineage),
            ("influence_lineage_families", r.Families.Count.ToString(CultureInfo.InvariantCulture)),
            ("influence_lineage_score", $"sum over shared families of log2(N/|F|) x agreement ({F(lp.AgreeSame)} same phase and rhythm, " +
                                        $"{F(lp.AgreePhase)} same phase, {F(lp.AgreeOther)} otherwise) x min(strength)^0.5; a strong match adds " +
                                        $"{F(lp.StrongWeight)} x its fused z"),
            ("influence_lineage_tree_rule", "each song credits its highest-scoring earlier song (exact ties: " +
                                            (lp.CreditCloseness != 0 ? "the closest version, then " : "") + "the earliest); parent = the most " +
                                            "referenced strong influencer (score >= ParentFraction x max)"),
            ("influence_lineage_minor_tonic", minorTonic.ToString(CultureInfo.InvariantCulture)),
            ("influence_lineage_params", lp.ToJson().ToJsonString(new JsonSerializerOptions { WriteIndented = false })),
        ];
    }

    private static void LogFamilies(List<Family> fams, int n, TextWriter log)
    {
        string By(Func<Family, bool> pred) => string.Join(", ", Enum.GetValues<FamilyKind>().Select(k => $"{Family.KindName(k)} {fams.Count(f => f.Kind == k && pred(f))}"));
        var songs = new HashSet<int>(fams.SelectMany(f => f.Members.Select(m => m.Song)));
        var shared = new HashSet<int>(fams.Where(f => f.Size >= 2).SelectMany(f => f.Members.Select(m => m.Song)));
        log.WriteLine($"families: {fams.Count} ({By(_ => true)}); shared by >= 2 songs: {By(f => f.Size >= 2)}; " +
                      $"{songs.Count} of {n} songs in a family, {shared.Count} in a shared one");
    }

    private static void LogTree(LineageResult r, TextWriter log)
    {
        var ch = r.Children;
        int maxc = ch.Max();
        int hub = Array.IndexOf(ch, maxc);
        log.WriteLine($"lineage tree: {r.Edges.Count(e => e.Kind == "tree")} tree edges, {r.Edges.Count(e => e.Kind == "secondary")} secondary, " +
                      $"{r.Roots} roots, max depth {r.Depth.Max()}, biggest parent {r.Songs[hub].Title} ({maxc} children)");
    }
}
