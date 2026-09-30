using System.Globalization;
using System.IO.Compression;
using System.Text;
using System.Text.Json;

namespace MusicHistory.Influence;

/// <summary>
/// <c>evaluate --benchmark &lt;dir&gt;</c>: scores the calibration benchmark (windows of one transcription against
/// whole transcriptions of the same or another work, <c>pairs.json</c>) with this C# code and prints the true
/// positive rate at a window FPR of 1e-3 (and 1e-2) per channel and fused. Part 1 reproduces the Python V8
/// (<c>analytic.py</c>: mel_mix, bass_mix_npc2, chord_mix, equal-weight Stouffer; df cap 5 and uncapped; bass_mix_rs;
/// lead vs every lane) and compares every pair's z with the Python score files when they are present. Part 2 is the
/// production configuration (weights, schema-filtered riff family in the bass channel, transposition hedge with its
/// penalty, lanes), and <c>--tune</c> prints the grid used to choose the weights and the penalty.
/// </summary>
internal static class BenchmarkEval
{
    private sealed class Ident
    {
        public string Key = "", Work = "";
        public double Fdb, Bpb = 4;
        public NoteLine? Mel, Bass;
        public ChordLine? Chg;
    }

    /// <summary>Whole-song target sets (null = channel not usable, as <c>sc.usable</c>).</summary>
    private sealed class TargetSets
    {
        public HashSet64? Mel, BassNpc2, BassMixRs, BassProd, Riff, Chord;
        public HashSet64? MelProd;                          // production melody set (figuration filter applied)
        public HashSet64[] Lanes = [], LanesAll = [];        // Lanes: production (filter, lead's own lane skipped)
    }

    /// <summary>A window's distinct keys per group and shift (index 0 = no shift).</summary>
    private sealed class WinKeys
    {
        public List<long>? Mel0, BassNpc2, BassMixRs, Chord0;           // shift 0, the Python families
        public List<long>?[] Mel = [], BassProd = [], Riff = [], Chord = [];   // per shift, production groups
        public List<long>?[] MelC = [], BassProdC = [], RiffC = [], ChordC = [];   // canonical keys of the production groups (KeyFreeDf)
        public List<long>?[] MelP = [], BassProdP = [], RiffP = [], ChordP = [];   // pitch-only projections (KeyFreeDf 2)
    }

    private const int NCh = 5;   // production channels on the benchmark: melody, bass, chord, lanes, riff

    /// <summary>The df cap of the Python port check (part 1): the benchmark's recommended cap, whatever the production cap.</summary>
    private const int PortCap = 5;
    private const int PMel = 0, PBass = 1, PChord = 2, PLanes = 3, PRiff = 4;

    public static int Run(string dir, Params p, TextWriter log, bool tune, double pairThreshold = double.NaN, string? db = null)
    {
        var t0 = System.Diagnostics.Stopwatch.StartNew();
        var idents = LoadIdents(Path.Combine(dir, "identities.json.gz"));
        using var winDoc = JsonDocument.Parse(File.ReadAllBytes(Path.Combine(dir, "windows.json")));
        using var pairDoc = JsonDocument.Parse(File.ReadAllBytes(Path.Combine(dir, "pairs.json")));
        var windows = winDoc.RootElement.EnumerateArray().ToDictionary(e => e.GetProperty("id").GetInt32(),
            e => (A: e.GetProperty("a_key").GetString()!, T0: e.GetProperty("t0").GetDouble(), T1: e.GetProperty("t1").GetDouble()));
        var pairs = pairDoc.RootElement.EnumerateArray().Select(e => (Win: e.GetProperty("window").GetInt32(),
            Target: e.GetProperty("target").GetString()!, Label: e.GetProperty("label").GetInt32())).ToArray();

        using var npz = new Npz(Path.Combine(dir, "df_corpus.npz"));
        int n = (int)npz.Int64("n")[0];
        var df = DfIndex.FromCounts(npz.Int64("keys"), npz.Int64("counts"), n);
        var perWork = new Dictionary<string, HashSet64>(StringComparer.Ordinal);
        foreach (string w in npz.Strings("works"))
        {
            var arr = npz.Int64("w_" + w);
            var set = new HashSet64(arr.Length);
            foreach (long k in arr) set.Add(unchecked((ulong)k));
            perWork[w] = set;
        }
        var lanes = LoadLanes(Path.Combine(dir, "target_lanes.json.gz"));
        log.WriteLine($"benchmark: {idents.Count} identities, {windows.Count} windows, {pairs.Length} pairs, df over {n} songs " +
                      $"({df.Distinct:N0} keys), lanes for {lanes.Count} targets ({t0.Elapsed.TotalSeconds:F1}s)");
        // Production df: from the pipeline DB's songs with the production n-gram documents (SongV8.Build: figuration filter,
        // canonical keys), so the production part sees exactly the run's df; else the npz (identical for the default settings).
        var dfProd = df;
        var perWorkProd = perWork;
        if (db != null)
        {
            using var conn = PipelineDb.Open(db);
            var songs = PipelineDb.LoadSongs(conn, new LoadStats());
            var docs = new SongV8[songs.Length];
            Parallel.For(0, songs.Length, new ParallelOptions { MaxDegreeOfParallelism = p.Threads }, i => docs[i] = SongV8.Build(songs[i], p));
            dfProd = DfIndex.Build(docs.Select(d => d.Doc).ToList());
            perWorkProd = new Dictionary<string, HashSet64>(StringComparer.Ordinal);
            for (int i = 0; i < songs.Length; i++) perWorkProd[songs[i].WorkId] = docs[i].DocSet;
            log.WriteLine($"production df from {db}: {songs.Length} songs, {dfProd.Distinct:N0} keys");
        }
        // Without --db the production part cannot weigh canonical / projection keys (their df is not in df_corpus.npz).
        bool prodOk = db != null || p.KeyFreeDf == 0;

        int[] shifts = p.Hedge != 0 ? [0, 3, -3, 5, -5] : [0];
        // Keys per window and target sets per target.
        var winKeys = new Dictionary<int, WinKeys>();
        foreach (var (id, w) in windows) winKeys[id] = WindowKeys(idents[w.A], w.T0, w.T1, shifts, p);
        var targets = new Dictionary<string, TargetSets>(StringComparer.Ordinal);
        foreach (string t in pairs.Select(x => x.Target).Distinct()) targets[t] = TargetsOf(idents[t], lanes.GetValueOrDefault(t), p);

        // NullSizeAdjust: target key-set size over the mean of the benchmark's targets, per channel.
        double Mean(Func<TargetSets, HashSet64?> f) =>
            targets.Values.Select(f).Where(x => x != null && x.Count > 0).Select(x => (double)x!.Count).DefaultIfEmpty(1).Average();
        double mMel = Mean(t => t.MelProd), mBass = Mean(t => t.BassProd), mChord = Mean(t => t.Chord), mRiff = Mean(t => t.Riff);
        double Rf(HashSet64? t, double mean) => p.NullSizeAdjust != 0 && t != null ? t.Count / mean : 1;
        var wt5 = new V8Weights(n, PortCap, p.SigmaFloor);          // part 1: the Python port's cap
        var wtP = new V8Weights(n, p.DfCap, p.SigmaFloor);          // part 2: the production cap
        var wt0 = new V8Weights(n, 0, p.SigmaFloor);
        int np = pairs.Length;
        // Python-equivalent families: [cap5, none] x [mel_mix, bass_mix_npc2, chord_mix, bass_mix_rs]; lanes (uncapped): lead, max lane.
        var refZ = new double[np, 2, 4];
        var laneLead = new double[np];
        var laneMax = new double[np];
        // Production: [pair, shift, channel].
        var prod = new double[np, shifts.Length, NCh];
        Parallel.For(0, np, new ParallelOptions { MaxDegreeOfParallelism = p.Threads }, i =>
        {
            var (wid, target, _) = pairs[i];
            var wk = winKeys[wid];
            var ts = targets[target];
            string wa = idents[windows[wid].A].Work, wb = idents[target].Work;
            var docA = perWork.GetValueOrDefault(wa);
            var docB = wa == wb ? docA : perWork.GetValueOrDefault(wb);
            var pdA = perWorkProd.GetValueOrDefault(wa);
            var pdB = wa == wb ? pdA : perWorkProd.GetValueOrDefault(wb);
            for (int ci = 0; ci < 2; ci++)
            {
                var wt = ci == 0 ? wt5 : wt0;
                refZ[i, ci, 0] = Z(wk.Mel0, ts.Mel, df, docA, docB, wt);
                refZ[i, ci, 1] = Z(wk.BassNpc2, ts.BassNpc2, df, docA, docB, wt);
                refZ[i, ci, 2] = Z(wk.Chord0, ts.Chord, df, docA, docB, wt);
                refZ[i, ci, 3] = Z(wk.BassMixRs, ts.BassMixRs, df, docA, docB, wt);
            }
            laneLead[i] = Z(wk.Mel0, ts.Mel, df, docA, docB, wt0);
            laneMax[i] = double.NaN;
            if (wk.Mel0 != null && ts.LanesAll.Length > 0)
            {
                double m = double.NegativeInfinity;
                foreach (var l in ts.LanesAll) m = Math.Max(m, wk.Mel0.Count > 0 ? V8Math.Direct(wk.Mel0, l, df, docA, docB, wt0).Z : -9);
                laneMax[i] = m;
            }
            for (int si = 0; si < shifts.Length; si++)
            {
                prod[i, si, PMel] = Z(wk.Mel[si], ts.MelProd, dfProd, pdA, pdB, wtP, Rf(ts.MelProd, mMel), wk.MelC[si], wk.MelP[si]);
                prod[i, si, PBass] = Z(wk.BassProd[si], ts.BassProd, dfProd, pdA, pdB, wtP, Rf(ts.BassProd, mBass), wk.BassProdC[si], wk.BassProdP[si]);
                prod[i, si, PChord] = Z(wk.Chord[si], ts.Chord, dfProd, pdA, pdB, wtP, Rf(ts.Chord, mChord), wk.ChordC[si], wk.ChordP[si]);
                prod[i, si, PRiff] = Z(wk.Riff[si], ts.Riff, dfProd, pdA, pdB, wtP, Rf(ts.Riff, mRiff), wk.RiffC[si], wk.RiffP[si]);
                double lz = double.NaN;
                if (wk.Mel[si] is { Count: > 0 } mk)
                    foreach (var l in ts.Lanes)
                    {
                        double z = V8Math.Direct(mk, l, dfProd, pdA, pdB, wtP, Rf(l, mMel), wk.MelC[si], wk.MelP[si]).Z;
                        if (double.IsNaN(lz) || z > lz) lz = z;
                    }
                prod[i, si, PLanes] = lz;
            }
        });
        log.WriteLine($"scored in {t0.Elapsed.TotalSeconds:F1}s");
        var labels = pairs.Select(x => x.Label).ToArray();

        // ---------------------------------------------------------------- part 1: port of the Python V8
        var py = LoadPython(dir, np);
        var sb = new StringBuilder();
        sb.AppendLine();
        sb.AppendLine("Part 1: port of the Python V8 (analytic.py), window FPR 1e-3 (1e-2 in brackets); Python from its score files");
        sb.AppendLine("| cap | family | C# TPR@1e-3 (thr) | Python TPR@1e-3 (thr) | C# TPR@1e-2 | Python TPR@1e-2 | C# AUC | pos/neg | max abs dz vs Python | pairs |dz| > 1e-3 |");
        sb.AppendLine("|---|---|---|---|---|---|---|---|---|---|");
        string[] famNames = ["mel_mix", "bass_mix_npc2", "chord_mix", "bass_mix_rs"];
        for (int ci = 0; ci < 2; ci++)
        {
            string cap = ci == 0 ? PortCap.ToString(CultureInfo.InvariantCulture) : "none";
            string capKey = ci == 0 ? "c" + PortCap.ToString(CultureInfo.InvariantCulture) : "cNone";
            for (int f = 0; f < 4; f++)
            {
                int ff = f, cc = ci;
                var mine = Enumerable.Range(0, np).Select(i => refZ[i, cc, ff]).ToArray();
                var theirs = py.TryGetValue((capKey, famNames[f]), out var arr) ? arr : null;
                sb.AppendLine(Row(cap, famNames[f], mine, theirs, labels));
            }
            var fz = Enumerable.Range(0, np).Select(i => Stouffer(refZ[i, ci, 0], refZ[i, ci, 1], refZ[i, ci, 2])).ToArray();
            double[]? pyF = null;
            if (py.TryGetValue((capKey, "mel_mix"), out var pm) && py.TryGetValue((capKey, "bass_mix_npc2"), out var pb) && py.TryGetValue((capKey, "chord_mix"), out var pc))
                pyF = Enumerable.Range(0, np).Select(i => Stouffer(pm[i], pb[i], pc[i])).ToArray();
            sb.AppendLine(Row(cap, "fused (equal-weight Stouffer mel_mix, bass_mix_npc2, chord_mix)", fz, pyF, labels));
        }
        py.TryGetValue(("lanes", "lead"), out var pyLead);
        py.TryGetValue(("lanes", "any"), out var pyAny);
        var anyl = Enumerable.Range(0, np).Select(i => double.IsNaN(laneMax[i]) ? laneLead[i] : double.IsNaN(laneLead[i]) ? laneMax[i] : Math.Max(laneMax[i], laneLead[i])).ToArray();
        if (pyAny != null)
        {
            // eval_lanes.py rows exist only for windows with a usable lead line.
            var keep = Enumerable.Range(0, np).Where(i => !double.IsNaN(pyAny[i]) || !double.IsNaN(pyLead![i])).ToHashSet();
            sb.AppendLine(Row("none", "lead vs target lead (lanes.py 'lead')", Mask(laneLead, keep), pyLead, labels));
            sb.AppendLine(Row("none", "lead vs every lane incl. lead (lanes.py 'any')", Mask(anyl, keep), pyAny, labels));
        }
        else sb.AppendLine(Row("none", "lead vs every lane incl. lead", anyl, null, labels));

        // ---------------------------------------------------------------- part 2: production configuration
        sb.AppendLine();
        if (!prodOk)
        {
            sb.AppendLine($"Part 2 skipped: KeyFreeDf {p.KeyFreeDf} weighs n-grams by the df of their canonical / pitch-only forms, which only a " +
                          "pipeline DB provides (evaluate --db <pipeline.sqlite>, or --param KeyFreeDf=0)");
            log.Write(sb.ToString());
            Console.Out.Write(sb.ToString());
            return 0;
        }
        sb.AppendLine($"Part 2: production V8 (df cap {p.DfCap}, null size adjustment {(p.NullSizeAdjust != 0 ? "on" : "off")}, riff family {(p.RiffInBass != 0 ? "in the bass channel" : "rule only")}, shifts [{string.Join(", ", shifts)}], " +
                      $"penalty {p.ShiftPenalty}, weights melody {p.WMelody} bass {p.WBass} chord {p.WChord} lanes {p.WLanes})");
        sb.AppendLine("| channel | TPR@1e-3 (thr) | TPR@1e-2 | AUC | pos/neg |");
        sb.AppendLine("|---|---|---|---|---|");
        string[] pn = ["melody (shift 0)", "bass incl. filtered riff (shift 0)", "chord (shift 0)", "lanes: lead vs every target lane (shift 0)", "riff family alone (shift 0)"];
        for (int c = 0; c < NCh; c++)
        {
            int cc = c;
            sb.AppendLine(ProdRow(pn[c], Enumerable.Range(0, np).Select(i => prod[i, 0, cc]).ToArray(), labels));
        }
        double[] w4 = [p.WMelody, p.WBass, p.WChord, p.WLanes];
        sb.AppendLine(ProdRow("fused, no hedge", Fuse(prod, np, [0], w4, 0, lanes: true, p), labels));
        sb.AppendLine(ProdRow("fused, no hedge, no lanes", Fuse(prod, np, [0], w4, 0, lanes: false, p), labels));
        var all = Enumerable.Range(0, shifts.Length).ToArray();
        sb.AppendLine(ProdRow($"fused, hedge (penalty {p.ShiftPenalty})", Fuse(prod, np, all, w4, p.ShiftPenalty, lanes: true, p), labels));
        sb.AppendLine(ProdRow($"fused, hedge (penalty {p.ShiftPenalty}), no lanes", Fuse(prod, np, all, w4, p.ShiftPenalty, lanes: false, p), labels));
        sb.AppendLine(ProdRow("fused, equal weights, no hedge, no lanes", Fuse(prod, np, [0], [1, 1, 1, 1], 0, lanes: false), labels));
        if (!double.IsNaN(pairThreshold))
        {
            // Recall of the production statistic at a pair-level threshold (a run's empirical threshold), as the
            // calibration report's "benchmark recall" column: the share of positive windows above it.
            var f = Fuse(prod, np, all, w4, p.ShiftPenalty, lanes: true, p);
            var pos = Pos(f, labels).ToArray();
            var neg = Neg(f, labels).ToArray();
            sb.AppendLine($"| recall at the pair-level threshold z > {pairThreshold:0.##} | {pos.Count(x => x > pairThreshold) / (double)pos.Length:F3} " +
                          $"(window FPR {neg.Count(x => x > pairThreshold) / (double)neg.Length:F5}) | | | {pos.Length}/{neg.Length} |");
        }

        if (tune)
        {
            sb.AppendLine();
            sb.AppendLine("Tuning grid (fused TPR@1e-3 / @1e-2; lanes on; hedge shifts all):");
            sb.AppendLine("| w_bass | w_lanes | w_chord | penalty | TPR@1e-3 | TPR@1e-2 | TPR@1e-3 positives with a key-frame offset |");
            sb.AppendLine("|---|---|---|---|---|---|---|");
            var offset = OffsetPositives(winDoc, pairs);
            foreach (double wbass in new[] { 0.4, 0.5, 0.6, 0.7, 0.8, 1.0 })
                foreach (double wl in new[] { 0.0, 0.5, 0.7, 1.0 })
                    foreach (double wc in new[] { 0.8, 1.0 })
                        foreach (double pen in new[] { double.PositiveInfinity, 0, 3, 6, 9, 12, 15 })
                        {
                            var f = Fuse(prod, np, double.IsPositiveInfinity(pen) ? [0] : all, [p.WMelody, wbass, wc, wl], double.IsPositiveInfinity(pen) ? 0 : pen, lanes: wl > 0, p);
                            var r3 = Metrics.TprAt(Pos(f, labels), Neg(f, labels), 1e-3);
                            var r2 = Metrics.TprAt(Pos(f, labels), Neg(f, labels), 1e-2);
                            double off = Pos(f, labels, offset).Count(x => x > r3.T) / (double)Math.Max(1, Pos(f, labels, offset).Count());
                            sb.AppendLine($"| {wbass} | {wl} | {wc} | {(double.IsPositiveInfinity(pen) ? "no hedge" : pen.ToString(CultureInfo.InvariantCulture))} | {r3.Tpr:F3} | {r2.Tpr:F3} | {off:F3} |");
                        }
        }
        log.Write(sb.ToString());
        Console.Out.Write(sb.ToString());
        return 0;
    }

    // ------------------------------------------------------------------------------------------------ scoring helpers
    private static double Z(List<long>? keys, HashSet64? target, DfIndex df, HashSet64? docA, HashSet64? docB, V8Weights wt, double r = 1,
        List<long>? canon = null, List<long>? proj = null) =>
        keys == null || target == null || keys.Count == 0 ? double.NaN : V8Math.Direct(keys, target, df, docA, docB, wt, r, canon, proj).Z;

    private static double Stouffer(params double[] zs)
    {
        double s = 0;
        int k = 0;
        foreach (double z in zs)
            if (!double.IsNaN(z))
            {
                s += z;
                k++;
            }
        return k > 0 ? s / Math.Sqrt(k) : double.NaN;
    }

    /// <summary>The engine's fusion (<see cref="V8Engine.Fuse"/>) over the benchmark channels, best shift with the penalty.</summary>
    private static double[] Fuse(double[,,] prod, int np, int[] shiftIdx, double[] w, double pen, bool lanes, Params? p = null)
    {
        var fp = p ?? new Params();
        double[] cw = [w[PMel], w[PBass], w[PChord], 0, lanes ? w[PLanes] : 0, 0];
        var outp = new double[np];
        Span<double> z = stackalloc double[Ch.N];
        for (int i = 0; i < np; i++)
        {
            double best = double.NaN;
            foreach (int si in shiftIdx)
            {
                int avail = 0;
                z.Clear();
                (int Ch, int P)[] map = [(Ch.Mel, PMel), (Ch.Bass, PBass), (Ch.Chord, PChord), (Ch.Lanes, PLanes)];
                foreach (var (c, pc) in map)
                {
                    if (c == Ch.Lanes && !lanes) continue;
                    double v = prod[i, si, pc];
                    if (double.IsNaN(v)) continue;
                    z[c] = v;
                    avail |= 1 << c;
                }
                double f = V8Engine.Fuse(avail, z, cw, fp);
                if (double.IsNaN(f)) continue;
                f -= si == 0 ? 0 : pen;
                if (double.IsNaN(best) || f > best) best = f;
            }
            outp[i] = best;
        }
        return outp;
    }

    private static IEnumerable<double> Pos(double[] v, int[] labels, HashSet<int>? only = null) =>
        Enumerable.Range(0, v.Length).Where(i => labels[i] == 1 && !double.IsNaN(v[i]) && (only == null || only.Contains(i))).Select(i => v[i]);

    private static IEnumerable<double> Neg(double[] v, int[] labels) =>
        Enumerable.Range(0, v.Length).Where(i => labels[i] == 0 && !double.IsNaN(v[i])).Select(i => v[i]);

    private static double[] Mask(double[] v, HashSet<int> keep) => v.Select((x, i) => keep.Contains(i) ? x : double.NaN).ToArray();

    private static string Row(string cap, string name, double[] mine, double[]? theirs, int[] labels)
    {
        var m3 = Metrics.TprAt(Pos(mine, labels), Neg(mine, labels), 1e-3);
        var m2 = Metrics.TprAt(Pos(mine, labels), Neg(mine, labels), 1e-2);
        double auc = Metrics.Auc(Pos(mine, labels), Neg(mine, labels));
        string pyc = "-", py2 = "-", dz = "-", nbad = "-";
        if (theirs != null)
        {
            var p3 = Metrics.TprAt(Pos(theirs, labels), Neg(theirs, labels), 1e-3);
            var p2 = Metrics.TprAt(Pos(theirs, labels), Neg(theirs, labels), 1e-2);
            pyc = $"{p3.Tpr:F3} (>{p3.T:0.##})";
            py2 = $"{p2.Tpr:F3}";
            double mx = 0;
            int bad = 0;
            for (int i = 0; i < mine.Length; i++)
            {
                if (double.IsNaN(mine[i]) != double.IsNaN(theirs[i]))
                {
                    bad++;
                    continue;
                }
                if (double.IsNaN(mine[i])) continue;
                double d = Math.Abs(mine[i] - theirs[i]);
                mx = Math.Max(mx, d);
                if (d > 1e-3) bad++;
            }
            dz = mx.ToString("0.0000", CultureInfo.InvariantCulture);
            nbad = bad.ToString(CultureInfo.InvariantCulture);
        }
        return $"| {cap} | {name} | {m3.Tpr:F3} (>{m3.T:0.##}) | {pyc} | {m2.Tpr:F3} | {py2} | {auc:F3} | {Pos(mine, labels).Count()}/{Neg(mine, labels).Count()} | {dz} | {nbad} |";
    }

    private static string ProdRow(string name, double[] v, int[] labels)
    {
        var m3 = Metrics.TprAt(Pos(v, labels), Neg(v, labels), 1e-3);
        var m2 = Metrics.TprAt(Pos(v, labels), Neg(v, labels), 1e-2);
        return $"| {name} | {m3.Tpr:F3} (>{m3.T:0.##}) | {m2.Tpr:F3} | {Metrics.Auc(Pos(v, labels), Neg(v, labels)):F3} | {Pos(v, labels).Count()}/{Neg(v, labels).Count()} |";
    }

    private static HashSet<int> OffsetPositives(JsonDocument winDoc, (int Win, string Target, int Label)[] pairs)
    {
        var off = winDoc.RootElement.EnumerateArray().Where(e => e.TryGetProperty("norm_offset", out var o) && o.ValueKind == JsonValueKind.Number && o.GetInt32() != 0)
            .Select(e => e.GetProperty("id").GetInt32()).ToHashSet();
        return Enumerable.Range(0, pairs.Length).Where(i => pairs[i].Label == 1 && off.Contains(pairs[i].Win)).ToHashSet();
    }

    // ------------------------------------------------------------------------------------------------ inputs
    private static Dictionary<string, Ident> LoadIdents(string path)
    {
        using var gz = new GZipStream(File.OpenRead(path), CompressionMode.Decompress);
        using var doc = JsonDocument.Parse(gz);
        var d = new Dictionary<string, Ident>(StringComparer.Ordinal);
        foreach (var e in doc.RootElement.EnumerateArray())
        {
            var id = new Ident
            {
                Key = e.GetProperty("key").GetString()!, Work = e.GetProperty("work_id").GetString()!,
                Fdb = e.GetProperty("first_downbeat").GetDouble(), Bpb = e.GetProperty("bpb").GetDouble(),
            };
            id.Mel = Line(e, "melody");
            id.Bass = Line(e, "bass");
            if (e.TryGetProperty("chg", out var c) && c.ValueKind == JsonValueKind.Object)
                id.Chg = new ChordLine
                {
                    Tokens = Ints(c.GetProperty("tokens")), Starts = Doubles(c.GetProperty("starts")), Durs = Doubles(c.GetProperty("durs")),
                    Downbeat = Ints(c.GetProperty("downbeat")),
                };
            d[id.Key] = id;
        }
        return d;
    }

    private static NoteLine? Line(JsonElement e, string role)
    {
        if (!e.TryGetProperty(role, out var l) || l.ValueKind != JsonValueKind.Object) return null;
        return new NoteLine
        {
            Onsets = Doubles(l.GetProperty("onsets")), Durs = Doubles(l.GetProperty("durs")),
            Pitches = Ints(l.GetProperty("pitches")), Met = Ints(l.GetProperty("met")),
        };
    }

    private static int[] Ints(JsonElement a) => a.EnumerateArray().Select(x => (int)Math.Round(x.GetDouble())).ToArray();
    private static double[] Doubles(JsonElement a) => a.EnumerateArray().Select(x => x.GetDouble()).ToArray();

    private static Dictionary<string, List<NoteLine>> LoadLanes(string path)
    {
        var d = new Dictionary<string, List<NoteLine>>(StringComparer.Ordinal);
        if (!File.Exists(path)) return d;
        using var gz = new GZipStream(File.OpenRead(path), CompressionMode.Decompress);
        using var doc = JsonDocument.Parse(gz);
        foreach (var t in doc.RootElement.EnumerateObject())
            d[t.Name] = t.Value.EnumerateArray().Select(l => new NoteLine
            {
                Onsets = Doubles(l.GetProperty("onsets")), Durs = Doubles(l.GetProperty("durs")),
                Pitches = Ints(l.GetProperty("pitches")), Met = Ints(l.GetProperty("met")),
            }).ToList();
        return d;
    }

    private static TargetSets TargetsOf(Ident t, List<NoteLine>? lanes, Params p)
    {
        var ts = new TargetSets();
        var occ = new List<GramOcc>();
        var keys = new List<long>();
        var mf = LineFilter.Of(p);
        if (t.Mel is { Count: >= 2 } mel && mel.Count >= p.MinLineNotes)
        {
            Grams.Line(true, mel.Onsets, mel.Pitches, t.Fdb, t.Bpb, 0, false, occ);
            ts.Mel = Set(occ, keys, Grp.Mel);
            if (mf.Active)
            {
                occ.Clear();
                Grams.Line(true, mel.Onsets, mel.Pitches, t.Fdb, t.Bpb, 0, false, occ, mf);
                ts.MelProd = Set(occ, keys, Grp.Mel);
            }
            else ts.MelProd = ts.Mel;
        }
        if (t.Bass is { Count: >= 2 } bass && bass.Count >= p.MinLineNotes)
        {
            occ.Clear();
            Grams.Line(false, bass.Onsets, bass.Pitches, t.Fdb, t.Bpb, 0, false, occ);
            ts.BassNpc2 = Set(occ, keys, Grp.BassNpc2);
            ts.Riff = Set(occ, keys, Grp.Riff);
            ts.BassMixRs = Union(ts.BassNpc2, Set(occ, keys, Grp.RiffRaw));
            ts.BassProd = p.RiffInBass != 0 ? Union(ts.BassNpc2, ts.Riff) : ts.BassNpc2;
        }
        if (t.Chg is { Count: >= 2 } ch && ch.Count >= p.MinChordChanges)
        {
            occ.Clear();
            Grams.Chords(ch.Tokens, ch.Starts, ch.Durs, t.Fdb, t.Bpb, 0, false, occ);
            ts.Chord = Set(occ, keys, Grp.Chord);
        }
        if (lanes != null)
        {
            HashSet64? lead = null;
            if (t.Mel is { Count: >= 2 } lm)
            {
                occ.Clear();
                Grams.Line(true, lm.Onsets, lm.Pitches, t.Fdb, t.Bpb, 0, false, occ, mf);
                lead = Set(occ, keys, Grp.Mel);
            }
            HashSet64[] LaneSets(LineFilter f) => lanes.Select(l =>
            {
                occ.Clear();
                Grams.Line(true, l.Onsets, l.Pitches, t.Fdb, t.Bpb, 0, false, occ, f);
                return Set(occ, keys, Grp.Mel);
            }).ToArray();
            var all = LaneSets(default);
            ts.LanesAll = all;
            // Production: the target lead's own lane is not a lane (as SongV8.Build); figuration filter applied.
            var prodLanes = mf.Active ? LaneSets(mf) : all;
            ts.Lanes = prodLanes.Where(x => lead == null || SongV8.Jaccard(x, lead) < p.LaneDupJaccard).ToArray();
        }
        return ts;
    }

    private static HashSet64 Set(List<GramOcc> occ, List<long> keys, Grp g)
    {
        Grams.GroupKeys(occ, g, keys);
        var s = new HashSet64(Math.Max(4, keys.Count));
        foreach (long k in keys) s.Add(unchecked((ulong)k));
        return s;
    }

    private static HashSet64 Union(HashSet64 a, HashSet64 b)
    {
        var s = new HashSet64(a.Count + b.Count);
        foreach (ulong k in a.Keys()) s.Add(k);
        foreach (ulong k in b.Keys()) s.Add(k);
        return s;
    }

    private static WinKeys WindowKeys(Ident a, double t0, double t1, int[] shifts, Params p)
    {
        var wk = new WinKeys
        {
            Mel = new List<long>?[shifts.Length], BassProd = new List<long>?[shifts.Length],
            Riff = new List<long>?[shifts.Length], Chord = new List<long>?[shifts.Length],
            MelC = new List<long>?[shifts.Length], BassProdC = new List<long>?[shifts.Length],
            RiffC = new List<long>?[shifts.Length], ChordC = new List<long>?[shifts.Length],
            MelP = new List<long>?[shifts.Length], BassProdP = new List<long>?[shifts.Length],
            RiffP = new List<long>?[shifts.Length], ChordP = new List<long>?[shifts.Length],
        };
        bool projOn = p.KeyFreeDf >= 2;
        bool projMel = projOn && (p.ProjScope & 2) != 0, projBass = projOn && (p.ProjScope & 4) != 0, projChord = projOn && (p.ProjScope & 1) != 0;
        var mf = LineFilter.Of(p);
        bool canon = p.KeyFreeDf != 0;
        var occ = new List<GramOcc>();
        var tmp = new List<long>();
        if (a.Mel != null && LineView.Of(a.Mel, t0, t1) is { Count: >= 2 } mv && mv.Count >= p.MinLineNotes)
        {
            for (int si = 0; si < shifts.Length; si++)
            {
                occ.Clear();
                Grams.Line(true, mv.On, mv.Pitch, a.Fdb, a.Bpb, shifts[si], false, occ, mf);
                (wk.Mel[si], wk.MelC[si], wk.MelP[si]) = KeysC(occ, Grp.Mel);
                if (!projMel) wk.MelP[si] = null;
                if (si == 0)
                {
                    if (!mf.Active) wk.Mel0 = wk.Mel[si];
                    else
                    {
                        occ.Clear();
                        Grams.Line(true, mv.On, mv.Pitch, a.Fdb, a.Bpb, 0, false, occ);
                        wk.Mel0 = Keys(occ, Grp.Mel);
                    }
                }
            }
        }
        if (a.Bass != null && LineView.Of(a.Bass, t0, t1) is { Count: >= 2 } bv && bv.Count >= p.MinLineNotes)
        {
            for (int si = 0; si < shifts.Length; si++)
            {
                occ.Clear();
                Grams.Line(false, bv.On, bv.Pitch, a.Fdb, a.Bpb, shifts[si], false, occ);
                var npc2 = Keys(occ, Grp.BassNpc2);
                var riff = Keys(occ, Grp.Riff);
                wk.Riff[si] = riff;
                var prod = new List<long>(npc2);
                if (p.RiffInBass != 0) prod.AddRange(riff);
                Grams.SortUnique(prod);
                wk.BassProd[si] = prod;
                if (canon)
                {
                    var cmap = new Dictionary<long, (long C, long P)>();
                    foreach (var o in occ) cmap[o.Key] = (o.Canon, o.Proj != 0 ? o.Proj : o.Canon);
                    wk.BassProdC[si] = prod.Select(k => cmap[k].C).ToList();
                    wk.RiffC[si] = riff.Select(k => cmap[k].C).ToList();
                    if (projBass)
                    {
                        wk.BassProdP[si] = prod.Select(k => cmap[k].P).ToList();
                        wk.RiffP[si] = riff.Select(k => cmap[k].P).ToList();
                    }
                }
                if (si == 0)
                {
                    wk.BassNpc2 = npc2;
                    var rs = new List<long>(npc2);
                    rs.AddRange(Keys(occ, Grp.RiffRaw));
                    Grams.SortUnique(rs);
                    wk.BassMixRs = rs;
                }
            }
        }
        if (a.Chg != null && ChordView.Of(a.Chg, t0, t1) is { Count: >= 2 } cv && cv.Count >= p.MinChordChanges)
        {
            for (int si = 0; si < shifts.Length; si++)
            {
                occ.Clear();
                Grams.Chords(cv.Tok, cv.Start, cv.Dur, a.Fdb, a.Bpb, shifts[si], false, occ);
                (wk.Chord[si], wk.ChordC[si], wk.ChordP[si]) = KeysC(occ, Grp.Chord);
                if (!projChord) wk.ChordP[si] = null;
                if (si == 0) wk.Chord0 = wk.Chord[si];
            }
        }
        return wk;

        List<long> Keys(List<GramOcc> o, Grp g)
        {
            var l = new List<long>();
            Grams.GroupKeys(o, g, l);
            return l;
        }

        (List<long>, List<long>?, List<long>?) KeysC(List<GramOcc> o, Grp g)
        {
            var l = new List<long>();
            if (!canon)
            {
                Grams.GroupKeys(o, g, l);
                return (l, null, null);
            }
            var c = new List<long>();
            var pj = projOn ? new List<long>() : null;
            Grams.GroupKeys(o, g, l, c, true, true, pj);
            return (l, c, pj);
        }
    }

    /// <summary>Per-pair z of the Python score files: (cap key, family) -> z by pair index (NaN when absent).</summary>
    private static Dictionary<(string, string), double[]> LoadPython(string dir, int np)
    {
        var d = new Dictionary<(string, string), double[]>();
        double[] Arr((string, string) k)
        {
            if (!d.TryGetValue(k, out var a))
            {
                a = Enumerable.Repeat(double.NaN, np).ToArray();
                d[k] = a;
            }
            return a;
        }
        void Read(string file, Action<JsonElement, int> each)
        {
            string path = Path.Combine(dir, file);
            if (!File.Exists(path)) return;
            foreach (string line in File.ReadLines(path))
            {
                using var doc = JsonDocument.Parse(line);
                int i = doc.RootElement.GetProperty("pair").GetInt32();
                if (i < np) each(doc.RootElement, i);
            }
        }
        void Fams(JsonElement r, int i, string prop, string capKey, string[] fams)
        {
            if (!r.TryGetProperty(prop, out var o)) return;
            foreach (string f in fams)
                if (o.TryGetProperty(f, out var v)) Arr((capKey, f))[i] = v[3].GetDouble();
        }
        Read("scores_v8.jsonl", (r, i) => Fams(r, i, "v8", "cNone", ["mel_mix", "bass_mix_npc2", "chord_mix"]));
        Read("scores_v8caps.jsonl", (r, i) =>
        {
            foreach (string c in new[] { "c3", "c5", "c10" }) Fams(r, i, c, c, ["mel_mix", "bass_mix_npc2", "chord_mix"]);
        });
        Read("scores_v8rs.jsonl", (r, i) =>
        {
            foreach (string c in new[] { "cNone", "c10", "c5" }) Fams(r, i, c, c, ["bass_mix_rs"]);
        });
        Read("scores_lanes.jsonl", (r, i) =>
        {
            double lead = r.GetProperty("lead").ValueKind == JsonValueKind.Number ? r.GetProperty("lead").GetDouble() : double.NaN;
            var lz = r.GetProperty("lanes").EnumerateArray().Select(x => x.GetDouble()).ToList();
            Arr(("lanes", "lead"))[i] = lead;
            if (!double.IsNaN(lead)) lz.Add(lead);
            Arr(("lanes", "any"))[i] = lz.Count > 0 ? lz.Max() : double.NaN;
        });
        return d;
    }
}

/// <summary>The benchmark's metrics (<c>analyze.py</c>).</summary>
internal static class Metrics
{
    /// <summary>
    /// <c>analyze.tpr_at</c>: t = the smallest observed negative value with P(neg &gt; t) &lt;= fpr (the
    /// (floor(fpr n) + 1)-th largest negative); TPR = P(pos &gt; t), strict.
    /// </summary>
    public static (double Tpr, double Fpr, double T) TprAt(IEnumerable<double> pos, IEnumerable<double> neg, double fpr)
    {
        var ns = neg.Order().ToArray();
        var ps = pos.ToArray();
        if (ns.Length == 0 || ps.Length == 0) return (double.NaN, double.NaN, double.NaN);
        int k = (int)Math.Floor(fpr * ns.Length);
        double t = k < ns.Length ? ns[ns.Length - 1 - k] : double.NegativeInfinity;
        return (ps.Count(x => x > t) / (double)ps.Length, ns.Count(x => x > t) / (double)ns.Length, t);
    }

    /// <summary>ROC AUC with average ranks for ties (<c>analyze.auc</c>).</summary>
    public static double Auc(IEnumerable<double> pos, IEnumerable<double> neg)
    {
        var p = pos.ToArray();
        var n = neg.ToArray();
        if (p.Length == 0 || n.Length == 0) return double.NaN;
        var all = p.Select(x => (V: x, P: true)).Concat(n.Select(x => (V: x, P: false))).OrderBy(x => x.V).ToArray();
        double rp = 0;
        int i = 0;
        while (i < all.Length)
        {
            int j = i;
            while (j + 1 < all.Length && all[j + 1].V == all[i].V) j++;
            double r = (i + j) / 2.0 + 1;
            for (int k = i; k <= j; k++)
                if (all[k].P) rp += r;
            i = j + 1;
        }
        return (rp - p.Length * (p.Length + 1) / 2.0) / ((double)p.Length * n.Length);
    }
}
