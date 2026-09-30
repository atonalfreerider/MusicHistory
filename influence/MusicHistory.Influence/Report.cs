using System.Text.Json;
using System.Text.Json.Nodes;

namespace MusicHistory.Influence;

/// <summary>
/// <c>data/graph/influence_report.json</c>: counts, the decision (empirical thresholds and the null sample's
/// quantiles), degree distributions, channel mix, timings, and validation against <c>known_influence</c>
/// (DESIGN.md §8.12): positives found and their rank, commonplace negatives edge-free, versions classified.
/// </summary>
internal static class Report
{
    public static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };

    private static JsonNode? J(double v) => double.IsNaN(v) || double.IsInfinity(v) ? null : JsonValue.Create(Math.Round(v, 6));
    private static JsonNode? J(double? v) => v is double d ? J(d) : null;

    public static JsonObject Build(RunState st, GraphSummary? g, List<KnownInfluence> known, Params p, RunOptions o, TextWriter log)
    {
        var songs = st.Songs;
        var tree = st.Tree!;
        int n = songs.Length;
        var sig = st.Stored.Where(r => r.Significant).ToList();
        var edgeKind = new Dictionary<(int, int), string>();
        foreach (var (pr, kind, _) in tree.Edges) edgeKind[(pr.A, pr.B)] = kind;

        var rep = new JsonObject
        {
            ["generated_at"] = o.Timestamp,
            ["pipeline_db"] = Path.GetFullPath(o.Db),
            ["graph_db"] = o.Export ? Path.GetFullPath(o.Graph) : null,
            ["graph_content_sha256"] = g?.ContentSha256,
            ["songs"] = new JsonObject
            {
                ["rows"] = st.Load.Rows, ["loaded"] = st.Load.Loaded, ["skipped_no_year"] = st.Load.SkippedNoYear,
                ["missing_key"] = st.Load.MissingKey, ["no_melody"] = st.Load.NoMelody, ["no_bass"] = st.Load.NoBass,
                ["no_chords"] = st.Load.NoChords, ["no_loops"] = st.Load.NoLoops, ["lane_lines"] = st.Load.Lanes,
                ["songs_with_lanes"] = st.Load.WithLanes, ["key_ambiguous_fifth"] = songs.Count(s => s.Fifth),
            },
            ["ngrams"] = new JsonObject
            {
                ["distinct"] = st.Engine.Df.Distinct, ["song_postings"] = st.Engine.Df.PostingsCount, ["df_cap"] = p.DfCap,
                ["lanes_indexed"] = st.Engine.TotalLanes,
            },
        };
        rep["pairs"] = new JsonObject
        {
            ["time_ordered"] = st.TimeOrdered,
            ["contemporaneous"] = st.Contemporaneous,
            ["windows_of_later_songs"] = st.Windows,
            ["above_threshold"] = st.AboveThreshold,
            ["significant"] = sig.Count,
            ["bass_alone_rejected"] = st.BassAloneRejected,
            ["bass_alone_moved_to_another_window"] = st.BassAloneRescued,
            ["hub_rejected"] = st.HubRejected,
            ["versions_above_threshold"] = st.VersionsAbove,
            ["same_time_above_threshold"] = st.Same.Count,
            ["same_time_versions"] = st.Same.Count(r => r.Version),
            ["pair_score_rows"] = st.Stored.Count + st.Same.Count,
            ["significant_by_shift"] = Histogram(sig.Select(r => r.Shift)),
            ["s_bits_quantiles_significant"] = Quantiles(sig.Select(r => r.S)),
        };
        rep["decision"] = Decision(st, p, sig, known);
        var inDeg = g?.InDegree ?? new int[n];
        var outDeg = g?.OutDegree ?? new int[n];
        rep["graph"] = new JsonObject
        {
            ["nodes"] = n,
            ["tree_edges"] = tree.Edges.Count(e => e.Kind == "tree"),
            ["secondary_edges"] = tree.Edges.Count(e => e.Kind == "secondary"),
            ["roots"] = tree.Roots,
            ["depth_histogram"] = Histogram(tree.Depth),
            ["in_degree_histogram"] = Histogram(inDeg),
            ["out_degree_histogram"] = Histogram(outDeg),
            ["ref_count_histogram"] = Histogram(tree.RefCount),
            ["largest_subtrees"] = new JsonArray(Enumerable.Range(0, n).Where(i => tree.Parent[i] < 0)
                .OrderByDescending(i => tree.Descendants[i]).ThenBy(i => i).Take(10)
                .Select(i => (JsonNode)new JsonObject { ["work_id"] = songs[i].WorkId, ["title"] = songs[i].Title, ["year"] = songs[i].Year, ["descendants"] = tree.Descendants[i] })
                .ToArray()),
            ["most_referenced"] = new JsonArray(Enumerable.Range(0, n).OrderByDescending(i => tree.RefCount[i]).ThenByDescending(i => tree.Katz[i]).ThenBy(i => i)
                .Take(15).Where(i => tree.RefCount[i] > 0)
                .Select(i => (JsonNode)new JsonObject
                {
                    ["work_id"] = songs[i].WorkId, ["title"] = songs[i].Title, ["artist"] = songs[i].Artist, ["year"] = songs[i].Year,
                    ["ref_count"] = tree.RefCount[i], ["ref_norm"] = J(tree.RefNorm[i]), ["katz"] = J(tree.Katz[i]), ["descendants"] = tree.Descendants[i],
                }).ToArray()),
        };
        if (g != null)
        {
            rep["graph"]!["excerpts_outside_home_key"] = g.ExcerptsOutsideHomeKey;
            rep["graph_meta_settings"] = new JsonObject
            {
                ["normalization"] = g.Settings.Normalization, ["normalization_source"] = g.Settings.NormalizationSource,
                ["target_key"] = g.Settings.TargetKey,
                ["target_bpm"] = J(g.Settings.TargetBpm), ["target_bpm_source"] = g.Settings.TargetBpmSource,
            };
        }
        rep["warnings"] = new JsonArray((g?.Settings.Warnings ?? []).Select(w => (JsonNode?)JsonValue.Create("graph_meta: " + w)).ToArray());
        var primary = new Dictionary<string, int>();
        var treePrimary = new Dictionary<string, int>();
        if (g != null)
            foreach (var e in g.EdgeRows)
            {
                primary[e.Primary] = primary.GetValueOrDefault(e.Primary) + 1;
                if (e.Kind == "tree") treePrimary[e.Primary] = treePrimary.GetValueOrDefault(e.Primary) + 1;
            }
        var counting = new JsonObject();
        foreach (Channel c in Enum.GetValues<Channel>()) counting[Channels.Name(c)] = sig.Count(r => r.Counts(c));
        rep["channel_mix"] = new JsonObject
        {
            ["edge_primary"] = Obj(primary), ["tree_edge_primary"] = Obj(treePrimary), ["significant_pairs_counting"] = counting,
            ["significant_bass_only_riff_cleared"] = sig.Count(r => Runner.IsBassAlone(r)),
            ["significant_single_counting_channel"] = sig.Count(r => r.Counting.Count(x => x) == 1),
        };
        rep["validation"] = Validation(st, known, edgeKind, p, log);
        rep["params"] = p.ToJson();
        return rep;
    }

    /// <summary>The empirical thresholds, the null sample's quantiles and an evaluation of the optional hubness check.</summary>
    private static JsonObject Decision(RunState st, Params p, List<PairResult> sig, List<KnownInfluence> known)
    {
        var s = st.Sample;
        var q = new JsonObject { ["n"] = s.Length };
        if (s.Length > 0)
            foreach (var (name, v) in new[] { ("p50", 0.5), ("p90", 0.9), ("p99", 0.99), ("p99.9", 0.999) })
                q[name] = J(Threshold.Quantile(s, v));
        if (s.Length > 0) q["max"] = J(s[^1]);
        var cnt = new JsonObject();
        foreach (var (k, t) in st.Count) cnt[k] = J(t.Value);
        // Operating curve: at each pair FPR, the empirical threshold, the time-ordered pairs above it and the expected false
        // ones (FPR x pairs): 1 - expected / above estimates the share of real (non-chance) pairs among them.
        var all = new List<double>();
        for (int b = 0; b < st.Songs.Length; b++)
            for (int a = 0; a < b; a++)
                if (st.Earlier(a, b) && !float.IsNaN(st.Recs[b][a].Z)) all.Add(st.Recs[b][a].Z);
        all.Sort();
        var curve = new JsonArray();
        foreach (double f in new[] { 1e-2, 3e-3, 1e-3, 3e-4, 1e-4, 3e-5, 1e-5 })
        {
            var t = Threshold.Of(s, f);
            int above = all.Count - UpperBound(all, t.Value);
            double exp = f * st.TimeOrdered;
            curve.Add(new JsonObject
            {
                ["fpr"] = f, ["threshold_z"] = J(t.Value), ["method"] = t.Method, ["pairs_above"] = above, ["expected_false"] = J(exp),
                ["estimated_real_share"] = above > 0 ? J(Math.Max(0, 1 - exp / above)) : null,
            });
        }
        var rq = new JsonObject { ["n"] = st.RiffSample.Length };
        if (st.RiffSample.Length > 0)
            foreach (var (name, v) in new[] { ("p50", 0.5), ("p99", 0.99), ("p99.9", 0.999) })
                rq[name] = J(Threshold.Quantile(st.RiffSample, v));
        if (st.RiffSample.Length > 0) rq["max"] = J(st.RiffSample[^1]);
        foreach (double t in new[] { 5.0, 10, 15, 20 }) rq[$"share_above_{t:0}"] = J(Threshold.Tail(st.RiffSample, t + 1e-9));
        // Hubness: what min(zA, zB) >= t would remove among the significant pairs and the known pairs.
        var idx = st.Songs.Select((x, i) => (x.WorkId, i)).ToDictionary(t => t.WorkId, t => t.i, StringComparer.Ordinal);
        var knownSig = new List<(string Kind, PairResult R)>();
        foreach (var k in known)
            if (idx.TryGetValue(k.Src, out int a) && idx.TryGetValue(k.Dst, out int b) && st.StoredResult(a, b) is { Significant: true } r)
                knownSig.Add((k.Kind, r));
        var hub = new JsonArray();
        foreach (double t in new[] { 1.0, 2.0, 3.0, 4.0 })
        {
            bool Removed(PairResult r) => Math.Min(r.HubZA, r.HubZB) < t;
            hub.Add(new JsonObject
            {
                ["min_hub_z"] = t,
                ["significant_removed"] = sig.Count(Removed),
                ["tree_parents_with_5plus_children_removed"] = sig.Count(r => Removed(r) && st.Tree!.Parent[r.B] == r.A && st.Tree.Descendants[r.A] >= 5),
                ["known_positive_removed"] = knownSig.Count(x => x.Kind != "control_negative" && x.Kind != "control_version" && Removed(x.R)),
                ["known_negative_removed"] = knownSig.Count(x => x.Kind == "control_negative" && Removed(x.R)),
            });
        }
        return new JsonObject
        {
            ["statistic"] = "max over 16-bar windows of the later song (hop 4 bars) of the weighted Stouffer z of the V8 channel z values",
            ["threshold_z"] = J(st.Fused.Value),
            ["threshold_method"] = st.Fused.Method,
            ["target_fpr"] = p.TargetFpr,
            ["null_sample"] = st.Fused.Sample,
            ["time_ordered_pairs"] = st.TimeOrdered,
            ["expected_false_pairs"] = J(p.TargetFpr * st.TimeOrdered),
            ["sample_quantiles"] = q,
            ["riff_threshold_z"] = J(st.Riff.Value),
            ["riff_threshold_method"] = st.Riff.Method,
            ["riff_sample_share_above"] = J(st.Riff.Fpr),
            ["riff_sample_quantiles"] = rq,
            ["count_threshold_z"] = cnt,
            ["count_fpr"] = p.CountFpr,
            ["store_threshold_z"] = J(st.Store.Value),
            ["bass_only_pairs_above_threshold"] = new JsonArray(st.Stored.Where(r => r.BassAlone).OrderByDescending(r => r.Zc).ThenBy(r => r.B).ThenBy(r => r.A).Take(40)
                .Select(r => (JsonNode)new JsonObject
                {
                    ["a"] = st.Songs[r.A].Title, ["b"] = st.Songs[r.B].Title, ["z"] = J(r.Zc), ["bass_z"] = J(r.Z[(int)Channel.Bass]),
                    ["riff_z"] = J(r.RiffZ), ["rejected"] = r.BassAloneRejected,
                }).ToArray()),
            ["operating_curve"] = curve,
            ["hubness_check"] = p.HubZ > 0 ? $"on, min(zA, zB) >= {p.HubZ}" : "off",
            ["hubness_evaluation"] = hub,
            // The top of the null sample: what random time-ordered pairs reach the threshold with (a diagnostic for the tail).
            ["null_sample_top"] = new JsonArray(st.Stored.Where(r => r.InSample && !double.IsNaN(r.Zc)).OrderByDescending(r => r.Zc).ThenBy(r => r.B).ThenBy(r => r.A).Take(30)
                .Select(r => (JsonNode)new JsonObject
                {
                    ["a"] = st.Songs[r.A].WorkId, ["b"] = st.Songs[r.B].WorkId, ["a_title"] = st.Songs[r.A].Title, ["b_title"] = st.Songs[r.B].Title,
                    ["z"] = J(r.Zc), ["shift"] = r.Shift,
                    ["counting"] = string.Join(",", Enumerable.Range(0, Channels.Count).Where(c => r.Counting[c]).Select(c => Channels.Names[c])),
                    ["z_channels"] = new JsonObject(Enumerable.Range(0, Channels.Count).Where(c => r.Avail[c])
                        .Select(c => KeyValuePair.Create(Channels.Names[c], J(r.Z[c])))),
                }).ToArray()),
        };
    }

    /// <summary>Index of the first element greater than <paramref name="v"/> in an ascending list.</summary>
    private static int UpperBound(List<double> sorted, double v)
    {
        int lo = 0, hi = sorted.Count;
        while (lo < hi)
        {
            int mid = (lo + hi) >>> 1;
            if (sorted[mid] <= v) lo = mid + 1; else hi = mid;
        }
        return lo;
    }

    public static void SetTimings(JsonObject rep, Dictionary<string, double> t)
    {
        var o = new JsonObject();
        foreach (var kv in t) o[kv.Key] = J(kv.Value);
        rep["timings_s"] = o;
    }

    private static JsonObject Obj(Dictionary<string, int> d)
    {
        var o = new JsonObject();
        foreach (var k in d.Keys.Order(StringComparer.Ordinal)) o[k] = d[k];
        return o;
    }

    private static JsonObject Histogram(IEnumerable<int> values)
    {
        var o = new JsonObject();
        foreach (var grp in values.GroupBy(v => v).OrderBy(x => x.Key)) o[grp.Key.ToString(System.Globalization.CultureInfo.InvariantCulture)] = grp.Count();
        return o;
    }

    private static JsonObject Quantiles(IEnumerable<double> values)
    {
        var v = values.Where(x => !double.IsNaN(x)).OrderBy(x => x).ToArray();
        var o = new JsonObject { ["n"] = v.Length };
        if (v.Length == 0) return o;
        foreach (var (name, q) in new[] { ("p50", 0.5), ("p90", 0.9), ("p99", 0.99) })
            o[name] = J(v[Math.Min(v.Length - 1, (int)Math.Floor(q * (v.Length - 1)))]);
        o["max"] = J(v[^1]);
        return o;
    }

    /// <summary>The result of a pair for the report: the stored one, else built from the engine's record.</summary>
    public static PairResult ResultOf(RunState st, int a, int b)
    {
        if (st.StoredResult(a, b) is { } r) return r;
        var pr = st.Details.From(st.Recs[b][a], a, b);
        pr.P = Threshold.Tail(st.Sample, pr.Zc);
        pr.Contemporaneous = !st.Earlier(a, b);
        pr.Relation = pr.Contemporaneous ? "contemporaneous" : "none";
        if (!double.IsNaN(pr.Zc)) Runner.SetCounting(st, pr);
        var (za, zb) = st.Hub(a, b, double.IsNaN(pr.Zc) ? 0 : pr.Zc, st.HubFloor);
        pr.HubZA = za;
        pr.HubZB = zb;
        return pr;
    }

    private static JsonObject Validation(RunState st, List<KnownInfluence> known, Dictionary<(int, int), string> edgeKind, Params p, TextWriter log)
    {
        var songs = st.Songs;
        var idx = songs.ToDictionary(s => s.WorkId, s => s.Index, StringComparer.Ordinal);
        var tree = st.Tree!;
        var rows = new JsonArray();
        var sigByB = st.Stored.Where(r => r.Significant).GroupBy(r => r.B).ToDictionary(g => g.Key, g => g.OrderByDescending(r => r.S).ThenBy(r => r.A).ToList());
        var summary = new Dictionary<string, Dictionary<string, int>>();
        var byPlant = new Dictionary<string, Dictionary<string, int>>();
        bool fixture = false;
        var planted = new HashSet<int>();

        void Count(Dictionary<string, Dictionary<string, int>> d, string group, string key)
        {
            if (!d.TryGetValue(group, out var m)) d[group] = m = [];
            m[key] = m.GetValueOrDefault(key) + 1;
        }

        foreach (var k in known)
        {
            string group = k.Kind switch
            {
                "control_negative" => "negative",
                "control_version" => "version",
                _ => "positive",
            };
            string? plant = null, expect = null;
            if (k.Note != null && k.Note.StartsWith('{'))
            {
                try
                {
                    using var doc = JsonDocument.Parse(k.Note);
                    if (doc.RootElement.TryGetProperty("plant", out var pl)) plant = pl.GetString();
                    if (doc.RootElement.TryGetProperty("expect", out var ex)) expect = ex.GetString();
                    if (doc.RootElement.TryGetProperty("fixture", out var fx) && fx.ValueKind == JsonValueKind.True) fixture = true;
                }
                catch (JsonException)
                {
                    // Free-form notes are fine.
                }
            }
            var row = new JsonObject { ["kind"] = k.Kind, ["src"] = k.Src, ["dst"] = k.Dst, ["plant"] = plant };
            rows.Add(row);
            Count(summary, group, "rows");
            if (k.Src == k.Dst)
            {
                row["present"] = idx.ContainsKey(k.Src);
                row["merged"] = true;
                Count(summary, group, "merged_same_work");
                continue;
            }
            if (!idx.TryGetValue(k.Src, out int a) || !idx.TryGetValue(k.Dst, out int b))
            {
                row["present"] = false;
                continue;
            }
            if (plant != null && group != "negative")
            {
                planted.Add(a);
                planted.Add(b);
            }
            Count(summary, group, "present");
            row["present"] = true;
            row["src_title"] = songs[a].Title;
            row["dst_title"] = songs[b].Title;
            row["src_year"] = songs[a].Year;
            row["dst_year"] = songs[b].Year;
            string order = DateOrder.Earlier(songs[a].Date, songs[b].Date) ? "earlier"
                : DateOrder.Earlier(songs[b].Date, songs[a].Date) ? "reversed" : "contemporaneous";
            row["order"] = order;
            // Look the pair up in the direction the dates allow (same-time pairs: index order).
            (int x, int y) = order == "reversed" ? (b, a) : (a, b);
            if (x > y) (x, y) = (y, x);
            var r = ResultOf(st, x, y);
            if (group == "version" && double.IsNaN(r.ChordId) && !r.Version) st.Details.VersionMeasures(r);
            string edge = edgeKind.GetValueOrDefault((x, y)) ?? "none";
            row["edge"] = edge;
            row["tested"] = order != "contemporaneous";
            row["significant"] = r.Significant;
            row["relation"] = r.Relation;
            row["z"] = J(r.Zc);
            row["above_threshold"] = r.Zc > st.Fused.Value;
            row["tail_probability"] = J(r.P);
            row["s_bits"] = J(r.S);
            row["pmi"] = J(r.Pmi);
            row["chord_identity"] = J(r.ChordId);
            row["shift"] = r.Shift;
            row["window_beats"] = double.IsNaN(r.WinStart) ? null : new JsonArray(J(r.WinStart), J(r.WinEnd));
            row["riff_z"] = J(r.RiffZ);
            row["bass_alone"] = Runner.IsBassAlone(r);
            row["bass_alone_rejected"] = r.BassAloneRejected;
            row["hub_z"] = J(Math.Min(r.HubZA, r.HubZB));
            var ch = new JsonObject();
            for (int c = 0; c < Channels.Count; c++)
                if (r.Avail[c] || !double.IsNaN(r.ZMax[c]))
                    ch[Channels.Names[c]] = new JsonObject
                    {
                        ["s"] = r.Avail[c] ? J(r.E[c]) : null, ["mu"] = r.Avail[c] ? J(r.Mu[c]) : null, ["z"] = J(r.Z[c]),
                        ["z_window_max"] = J(r.ZMax[c]), ["counts"] = r.Counting[c],
                    };
            row["channels"] = ch;
            if (sigByB.TryGetValue(y, out var sigs))
            {
                int rk = sigs.FindIndex(t => t.A == x) + 1;
                row["rank_among_significant"] = rk > 0 ? rk : null;
                row["significant_influencers"] = sigs.Count;
            }
            row["parent_is_src"] = tree.Parent[y] == x;
            bool significant = r.Significant;
            switch (group)
            {
                case "positive":
                    if (order != "earlier") Count(summary, group, "not_orderable_" + order);
                    if (r.Zc > st.Fused.Value) Count(summary, group, "above_threshold");
                    if (significant) Count(summary, group, "significant");
                    if (edge != "none") Count(summary, group, "edge");
                    if (tree.Parent[y] == x) Count(summary, group, "parent");
                    if (sigByB.TryGetValue(y, out var s3) && s3.Take(3).Any(t => t.A == x)) Count(summary, group, "top3");
                    break;
                case "negative":
                    if (edge != "none") Count(summary, group, "with_edge");
                    if (significant) Count(summary, group, "significant");
                    if (r.Zc > st.Fused.Value) Count(summary, group, "above_threshold");
                    break;
                default:
                    if (r.Version) Count(summary, group, "classified_version");
                    if (edge != "none") Count(summary, group, "with_edge");
                    break;
            }
            if (plant != null)
            {
                string key = plant + (expect == "none" ? "_expect_none" : "");
                Count(byPlant, key, "n");
                if (significant) Count(byPlant, key, "significant");
                if (edge != "none") Count(byPlant, key, "edge");
                if (tree.Parent[y] == x) Count(byPlant, key, "parent");
                if (r.Version) Count(byPlant, key, "version");
                if (r.Relation == "contemporaneous" || order == "contemporaneous") Count(byPlant, key, "contemporaneous");
            }
        }
        var outp = new JsonObject();
        var sum = new JsonObject();
        foreach (var kv in summary.OrderBy(k => k.Key, StringComparer.Ordinal))
        {
            var o = new JsonObject();
            foreach (var kk in kv.Value.OrderBy(k => k.Key, StringComparer.Ordinal)) o[kk.Key] = kk.Value;
            sum[kv.Key] = o;
        }
        outp["summary"] = sum;
        if (byPlant.Count > 0)
        {
            var bp = new JsonObject();
            foreach (var kv in byPlant.OrderBy(k => k.Key, StringComparer.Ordinal))
            {
                var o = new JsonObject();
                foreach (var kk in kv.Value.OrderBy(k => k.Key, StringComparer.Ordinal)) o[kk.Key] = kk.Value;
                bp[kv.Key] = o;
            }
            outp["by_plant"] = bp;
        }
        if (fixture)
        {
            // Time-ordered pairs of songs untouched by any plant: every significant one is a false positive.
            long tested = 0, significant = 0, edges = 0;
            for (int b = 0; b < songs.Length; b++)
                for (int a = 0; a < b; a++)
                {
                    if (planted.Contains(a) || planted.Contains(b) || !st.Earlier(a, b)) continue;
                    tested++;
                    if (st.StoredResult(a, b) is { Significant: true }) significant++;
                    if (edgeKind.ContainsKey((a, b))) edges++;
                }
            outp["background"] = new JsonObject { ["tested"] = tested, ["significant"] = significant, ["edges"] = edges };
        }
        outp["pairs"] = rows;
        log.WriteLine($"validation: {known.Count} known rows");
        return outp;
    }
}
