using System.Text.Json;
using System.Text.Json.Nodes;

namespace MusicHistory.Influence;

/// <summary>
/// <c>data/graph/influence_report.json</c>: counts, degree distributions, root count, depth
/// histogram, channel mix, timings, and validation against <c>known_influence</c> (DESIGN.md §8.12):
/// positives found and their rank, commonplace negatives edge-free, versions classified.
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
        var tested = st.Tested;
        var sig = tested.Where(r => r.Significant).ToList();
        var byPair = new Dictionary<(int, int), PairResult>();
        foreach (var r in tested) byPair[(r.A, r.B)] = r;
        foreach (var r in st.Same) byPair.TryAdd((r.A, r.B), r);
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
                ["no_chords"] = st.Load.NoChords, ["no_loops"] = st.Load.NoLoops,
                ["key_ambiguous_fifth"] = songs.Count(s => s.Fifth),
                ["melody_gated_half"] = songs.Count(s => s.IntervalEntropy < p.EntropyHalf && s.IntervalEntropy >= p.EntropyDrop),
                ["melody_gated_drop"] = songs.Count(s => s.IntervalEntropy < p.EntropyDrop),
            },
            ["ngrams"] = new JsonObject
            {
                ["distinct"] = st.Corpus.DistinctCount, ["song_postings"] = st.Corpus.Postings, ["stop_df_above"] = st.Corpus.StopDf,
            },
        };
        var candCounts = st.Candidates.Select(c => c?.Count ?? 0).ToArray();
        rep["pairs"] = new JsonObject
        {
            ["candidates"] = candCounts.Sum(),
            ["candidates_per_song_mean"] = J(candCounts.Average()),
            ["candidates_per_song_max"] = candCounts.Max(),
            ["songs_at_candidate_cap"] = candCounts.Count(c => c >= p.CandMax),
            ["tested"] = tested.Count,
            ["confirmed_k100"] = st.Confirmed,
            ["significant"] = sig.Count,
            ["versions"] = tested.Count(r => r.Version) + st.Same.Count(r => r.Version),
            ["same_time_checked"] = st.Same.Count,
            ["fifth_shift_used"] = tested.Count(r => r.Shift != 0),
            ["alignment_gcells"] = J(st.Cells / 1e9),
            ["surrogates"] = st.Surrogates,
            ["surrogates_aligned"] = st.SurrogatesAligned,
            ["z_quantiles"] = Quantiles(tested.Select(r => r.Zc)),
            ["s_bits_quantiles_significant"] = Quantiles(sig.Select(r => r.S)),
        };
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
            // Where graph_meta's normalization / target_bpm came from; warnings when not from the pipeline DB.
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
        };
        rep["validation"] = Validation(st, known, byPair, edgeKind, p, log);
        rep["params"] = p.ToJson();
        return rep;
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

    private static JsonObject Validation(RunState st, List<KnownInfluence> known, Dictionary<(int, int), PairResult> byPair,
        Dictionary<(int, int), string> edgeKind, Params p, TextWriter log)
    {
        var songs = st.Songs;
        var idx = songs.ToDictionary(s => s.WorkId, s => s.Index, StringComparer.Ordinal);
        var tree = st.Tree!;
        var rows = new JsonArray();
        var forcedWorker = new Worker(songs.Length, p);
        int forced = 0;
        var sigByB = st.Tested.Where(r => r.Significant).GroupBy(r => r.B).ToDictionary(g => g.Key, g => g.OrderByDescending(r => r.S).ThenBy(r => r.A).ToList());
        var testedByB = st.Tested.GroupBy(r => r.B).ToDictionary(g => g.Key, g => g.OrderByDescending(r => r.S).ThenBy(r => r.A).ToList());
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
            // Look the pair up in the direction the dates allow.
            (int x, int y) = order == "reversed" ? (b, a) : (a, b);
            if (order == "contemporaneous" && x > y) (x, y) = (y, x);
            byPair.TryGetValue((x, y), out var r);
            bool isCandidate = r != null && !r.Contemporaneous;
            row["candidate"] = isCandidate;
            if (r != null)
            {
                row["candidate_rank"] = r.CandRank;
                row["candidate_bits"] = J(r.CandBits);
            }
            else
            {
                row["candidate_rank"] = st.Candidates[y]?.Where(c => c.A == x).Select(c => (int?)c.Rank).FirstOrDefault();
            }
            if (r == null && order != "contemporaneous" && group != "negative" && forced < 2000)
            {
                r = st.Scorer.Score(songs[x], songs[y], forcedWorker);
                forced++;
                row["forced"] = true;
            }
            string edge = edgeKind.GetValueOrDefault((x, y)) ?? "none";
            row["edge"] = edge;
            if (r != null)
            {
                row["tested"] = isCandidate && r.Tested;
                row["significant"] = r.Significant;
                row["relation"] = r.Relation;
                row["z"] = J(r.Zc);
                row["q"] = J(r.Q);
                row["s_bits"] = J(r.S);
                row["pmi"] = J(r.Pmi);
                row["chord_identity"] = J(r.ChordId);
                row["shift"] = r.Shift;
                var ch = new JsonObject();
                for (int c = 0; c < Channels.Count; c++)
                    if (r.Avail[c]) ch[Channels.Names[c]] = new JsonObject { ["e"] = J(r.E[c]), ["mu"] = J(r.Mu[c]), ["z"] = J(r.Z[c]) };
                row["channels"] = ch;
                if (isCandidate && testedByB.TryGetValue(y, out var all)) row["rank_by_s"] = all.FindIndex(t => t.A == x) + 1;
                if (sigByB.TryGetValue(y, out var sigs))
                {
                    int rk = sigs.FindIndex(t => t.A == x) + 1;
                    row["rank_among_significant"] = rk > 0 ? rk : null;
                    row["significant_influencers"] = sigs.Count;
                }
            }
            row["parent_is_src"] = tree.Parent[y] == x;
            bool significant = r?.Significant == true;
            switch (group)
            {
                case "positive":
                    if (order != "earlier") Count(summary, group, "not_orderable_" + order);
                    if (significant) Count(summary, group, "significant");
                    if (edge != "none") Count(summary, group, "edge");
                    if (tree.Parent[y] == x) Count(summary, group, "parent");
                    if (r != null && sigByB.TryGetValue(y, out var s3) && s3.Take(3).Any(t => t.A == x)) Count(summary, group, "top3");
                    break;
                case "negative":
                    if (edge != "none") Count(summary, group, "with_edge");
                    if (significant) Count(summary, group, "significant");
                    break;
                default:
                    if (r?.Version == true) Count(summary, group, "classified_version");
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
                if (r?.Version == true) Count(byPlant, key, "version");
                if (r?.Relation == "contemporaneous" || order == "contemporaneous") Count(byPlant, key, "contemporaneous");
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
            // Pairs of songs untouched by any plant: every significant one is a false positive.
            var background = st.Tested.Where(r => !planted.Contains(r.A) && !planted.Contains(r.B)).ToList();
            outp["background"] = new JsonObject
            {
                ["tested"] = background.Count,
                ["significant"] = background.Count(r => r.Significant),
                ["edges"] = background.Count(r => edgeKind.ContainsKey((r.A, r.B))),
            };
        }
        outp["forced_diagnostics"] = forced;
        outp["pairs"] = rows;
        log.WriteLine($"validation: {known.Count} known rows ({forced} force-scored for diagnostics)");
        return outp;
    }
}
