using System.Globalization;
using System.Text.Json.Nodes;

namespace MusicHistory.Influence;

/// <summary>
/// <c>influence_report.json</c> of the identity lineages: the strict evidence's report (songs, n-grams, pairs, decision:
/// the strong matches) with its tree and validation moved to <c>strict_evidence_graph</c> / <c>strict_evidence_validation</c>,
/// plus <c>families</c> (sizes, kinds, top families, singletons), <c>tree</c> (roots, depth histogram, largest subtrees,
/// longest root-to-leaf chains, hubs and the degenerate-structure guard), <c>graph</c> (the exported lineage graph) and
/// <c>validation</c> (known pairs against the lineage edges).
/// </summary>
internal static class LineageReport
{
    private static JsonNode? J(double v) => double.IsNaN(v) || double.IsInfinity(v) ? null : JsonValue.Create(Math.Round(v, 6));

    public static JsonObject Build(RunState st, LineageResult r, List<(string Name, LineageResult R)> variants, LineageGraphSummary? g,
        List<KnownInfluence> known, Params p, LineageParams lp, RunOptions o, TextWriter log)
    {
        var rep = Report.Build(st, null, known, p, o, log);
        var strictGraph = rep["graph"];
        var strictValidation = rep["validation"];
        var strictMix = rep["channel_mix"];
        rep.Remove("graph");
        rep.Remove("validation");
        rep.Remove("channel_mix");
        rep.Remove("params");
        rep.Remove("warnings");
        var outp = new JsonObject
        {
            ["edge_semantics"] = LineageStore.Lineage,
        };
        foreach (var kv in rep.ToList())
        {
            rep.Remove(kv.Key);
            outp[kv.Key] = kv.Value;
        }
        if (g != null) outp["graph_content_sha256"] = g.Graph.ContentSha256;
        outp["families"] = FamiliesSection(r);
        outp["tree"] = TreeSection(r, variants);
        outp["graph"] = GraphSection(r, g);
        if (g != null)
            outp["graph_meta_settings"] = new JsonObject
            {
                ["normalization"] = g.Graph.Settings.Normalization, ["normalization_source"] = g.Graph.Settings.NormalizationSource,
                ["target_key"] = g.Graph.Settings.TargetKey, ["target_bpm"] = J(g.Graph.Settings.TargetBpm),
                ["target_bpm_source"] = g.Graph.Settings.TargetBpmSource,
            };
        outp["warnings"] = new JsonArray((g?.Graph.Settings.Warnings ?? []).Select(w => (JsonNode?)JsonValue.Create("graph_meta: " + w)).ToArray());
        outp["validation"] = Validation(r, known);
        outp["strict_evidence_graph"] = strictGraph;
        outp["strict_evidence_channel_mix"] = strictMix;
        outp["strict_evidence_validation"] = strictValidation;
        outp["params"] = p.ToJson();
        outp["lineage_params"] = lp.ToJson();
        return outp;
    }

    private static JsonObject Song(Song s) => new() { ["work_id"] = s.WorkId, ["title"] = s.Title, ["artist"] = s.Artist, ["year"] = s.Year };

    private static JsonObject Hist(IEnumerable<int> values)
    {
        var o = new JsonObject();
        foreach (var grp in values.GroupBy(v => v).OrderBy(x => x.Key)) o[grp.Key.ToString(CultureInfo.InvariantCulture)] = grp.Count();
        return o;
    }

    private static JsonObject ByKind(IEnumerable<Family> fams)
    {
        var o = new JsonObject();
        foreach (var k in Enum.GetValues<FamilyKind>()) o[Family.KindName(k)] = fams.Count(f => f.Kind == k);
        return o;
    }

    private static string SizeBucket(int s) => s switch
    {
        1 => "1", 2 => "2", <= 4 => "3-4", <= 8 => "5-8", <= 16 => "9-16", <= 32 => "17-32", <= 64 => "33-64", <= 128 => "65-128", _ => "129+",
    };

    public static JsonObject FamiliesSection(LineageResult r)
    {
        var fams = r.Families;
        var songs = r.Songs;
        var treeEdgesByFam = r.Edges.Where(e => e.Kind == "tree").GroupBy(e => e.Family.Id).ToDictionary(x => x.Key, x => x.ToList());
        var treeByFam = treeEdgesByFam.ToDictionary(x => x.Key, x => x.Value.Count);
        // Is a family a tree of versions or a star? Its tree edges' distinct parents, the largest parent's share, and the
        // longest chain of its own edges.
        JsonObject Shape(Family f)
        {
            if (!treeEdgesByFam.TryGetValue(f.Id, out var es) || es.Count == 0) return new JsonObject { ["tree_edges"] = 0 };
            var byParent = es.GroupBy(e => e.Pair.A).Select(g => g.Count()).ToList();
            var inFam = es.ToDictionary(e => e.Pair.B, e => e.Pair.A);
            int longest = 0;
            foreach (int b in inFam.Keys)
            {
                int d = 0;
                for (int x = b; inFam.TryGetValue(x, out int a); x = a) d++;
                longest = Math.Max(longest, d);
            }
            return new JsonObject
            {
                ["tree_edges"] = es.Count, ["distinct_parents"] = byParent.Count, ["largest_parent_children"] = byParent.Max(),
                ["largest_parent_share"] = J(byParent.Max() / (double)es.Count), ["longest_chain_edges"] = longest,
            };
        }
        var perSong = new int[songs.Length];
        var perSongShared = new int[songs.Length];
        foreach (var f in fams)
            foreach (var m in f.Members)
            {
                perSong[m.Song]++;
                if (f.Size >= 2) perSongShared[m.Song]++;
            }
        var sizes = new JsonObject();
        foreach (var grp in fams.GroupBy(f => SizeBucket(f.Size)).OrderBy(x => x.Min(f => f.Size))) sizes[grp.Key] = grp.Count();
        var sorted = perSongShared.OrderBy(x => x).ToArray();
        return new JsonObject
        {
            ["total"] = fams.Count,
            ["by_kind"] = ByKind(fams),
            ["shared_by_kind"] = ByKind(fams.Where(f => f.Size >= 2)),
            ["singletons"] = fams.Count(f => f.Size == 1),
            ["singletons_by_kind"] = ByKind(fams.Where(f => f.Size == 1)),
            ["size_histogram"] = sizes,
            ["songs_with_family"] = perSong.Count(x => x > 0),
            ["songs_with_shared_family"] = perSongShared.Count(x => x > 0),
            ["shared_families_per_song"] = new JsonObject
            {
                ["mean"] = J(perSongShared.Average()), ["p50"] = sorted[sorted.Length / 2], ["p90"] = sorted[(int)(0.9 * (sorted.Length - 1))], ["max"] = sorted[^1],
            },
            ["top"] = new JsonArray(fams.Where(f => f.Kind != FamilyKind.Strong).OrderByDescending(f => f.Size).ThenBy(f => f.Id).Take(25)
                .Select(f => (JsonNode)new JsonObject
                {
                    ["family_id"] = f.Id, ["label"] = f.Label, ["kind"] = Family.KindName(f.Kind), ["roman"] = f.Roman, ["size"] = f.Size,
                    ["specificity_bits"] = J(f.Specificity), ["root_song"] = Song(songs[f.Members[0].Song]),
                    ["tree_edges"] = treeByFam.GetValueOrDefault(f.Id), ["tree_shape"] = Shape(f),
                }).ToArray()),
            ["named"] = new JsonArray(fams.Where(f => f.Kind == FamilyKind.Schema || f.Kind == FamilyKind.Progression).OrderByDescending(f => f.Size).ThenBy(f => f.Id)
                .Select(f => (JsonNode)new JsonObject
                {
                    ["family_id"] = f.Id, ["label"] = f.Label, ["size"] = f.Size, ["root_song"] = Song(songs[f.Members[0].Song]),
                    ["tree_edges"] = treeByFam.GetValueOrDefault(f.Id),
                }).ToArray()),
        };
    }

    /// <summary>Children per parent, the biggest parents with the identities of their child edges.</summary>
    public static JsonObject Hubs(LineageResult r, int top = 10)
    {
        var ch = r.Children;
        var childEdges = r.Edges.Where(e => e.Kind == "tree").GroupBy(e => e.Pair.A).ToDictionary(x => x.Key, x => x.ToList());
        int nonRoots = r.Parent.Count(x => x >= 0);
        int maxc = ch.Max();
        // The largest star of one family: children of one parent whose tree edge names the same family.
        int star = 0;
        string? starLabel = null;
        foreach (var (a, list) in childEdges)
            foreach (var grp in list.GroupBy(e => e.Family.Id))
                if (grp.Count() > star)
                {
                    star = grp.Count();
                    starLabel = $"{grp.First().Family.Label} ({r.Songs[a].Title})";
                }
        return new JsonObject
        {
            ["children_histogram"] = Hist(ch.Where(x => x > 0)),
            ["parents"] = ch.Count(x => x > 0),
            ["max_children"] = maxc,
            ["parents_with_10plus_children"] = ch.Count(x => x >= 10),
            ["parents_with_20plus_children"] = ch.Count(x => x >= 20),
            ["parents_with_50plus_children"] = ch.Count(x => x >= 50),
            ["largest_hub_share_of_non_roots"] = J(nonRoots > 0 ? maxc / (double)nonRoots : 0),
            ["largest_one_family_star"] = star,
            ["largest_one_family_star_identity"] = starLabel,
            ["top_parents"] = new JsonArray(Enumerable.Range(0, r.Songs.Length).Where(i => ch[i] > 0).OrderByDescending(i => ch[i]).ThenBy(i => i).Take(top)
                .Select(i =>
                {
                    var o = Song(r.Songs[i]);
                    o["children"] = ch[i];
                    o["descendants"] = r.Descendants[i];
                    o["ref_count"] = r.RefCount[i];
                    var ids = new JsonObject();
                    foreach (var grp in childEdges[i].GroupBy(e => e.Family.Label).OrderByDescending(x => x.Count()).ThenBy(x => x.Key, StringComparer.Ordinal))
                        ids[grp.Key] = grp.Count();
                    o["child_identities"] = ids;
                    return (JsonNode)o;
                }).ToArray()),
        };
    }

    public static JsonObject TreeSection(LineageResult r, List<(string Name, LineageResult R)> variants)
    {
        var songs = r.Songs;
        int n = songs.Length;
        var tree = r.Edges.Where(e => e.Kind == "tree").ToList();
        var treeIn = new LinEdge?[n];
        foreach (var e in tree) treeIn[e.Pair.B] = e;
        var children = r.Children;
        // Longest root-to-leaf chains (one per root, deepest first).
        var chains = new JsonArray();
        foreach (int leaf in Enumerable.Range(0, n).Where(i => children[i] == 0 && r.Parent[i] >= 0).GroupBy(i => r.Root[i])
                     .Select(grp => grp.OrderByDescending(i => r.Depth[i]).ThenBy(i => i).First()).OrderByDescending(i => r.Depth[i]).ThenBy(i => i).Take(5))
        {
            var path = new List<int>();
            for (int x = leaf; x >= 0; x = r.Parent[x]) path.Add(x);
            path.Reverse();
            chains.Add(new JsonArray(path.Select(x =>
            {
                var o = Song(songs[x]);
                if (treeIn[x] is { } e) o["via"] = e.Family.Label;
                return (JsonNode)o;
            }).ToArray()));
        }
        var strongPairs = r.Families.Where(f => f.Kind == FamilyKind.Strong).ToList();
        var strongKeys = strongPairs.Select(f => (f.Members[0].Song, f.Members[^1].Song)).ToHashSet();
        var guard = new JsonArray();
        foreach (var (name, v) in variants)
        {
            var h = Hubs(v, 3);
            guard.Add(new JsonObject
            {
                ["rule"] = name, ["roots"] = v.Roots, ["max_depth"] = v.Depth.Max(),
                ["max_children"] = h["max_children"]!.DeepClone(), ["parents_with_10plus_children"] = h["parents_with_10plus_children"]!.DeepClone(),
                ["parents_with_20plus_children"] = h["parents_with_20plus_children"]!.DeepClone(),
                ["largest_hub_share_of_non_roots"] = h["largest_hub_share_of_non_roots"]!.DeepClone(),
                ["largest_one_family_star"] = h["largest_one_family_star"]!.DeepClone(),
                ["largest_one_family_star_identity"] = h["largest_one_family_star_identity"]?.DeepClone(),
                ["distinct_credited_songs"] = v.Credit.Where(x => x >= 0).Distinct().Count(),
                ["max_ref_count"] = v.RefCount.Max(),
                ["families_by_tree_edges"] = new JsonArray(v.Edges.Where(e => e.Kind == "tree").GroupBy(e => e.Family.Id)
                    .OrderByDescending(g => g.Count()).ThenBy(g => g.Key).Take(3).Select(g => (JsonNode)new JsonObject
                    {
                        ["label"] = g.First().Family.Label, ["tree_edges"] = g.Count(), ["distinct_parents"] = g.Select(e => e.Pair.A).Distinct().Count(),
                        ["largest_parent_children"] = g.GroupBy(e => e.Pair.A).Max(x => x.Count()),
                    }).ToArray()),
                ["top_parents"] = h["top_parents"]!.DeepClone(),
            });
        }
        var byKind = new JsonObject();
        foreach (var k in Enum.GetValues<FamilyKind>()) byKind[Family.KindName(k)] = tree.Count(e => e.Family.Kind == k);
        return new JsonObject
        {
            ["rule"] = "score = sum over shared families of specificity x agreement x closeness x min(strength)^0.5 (+ StrongWeight x z); " +
                       "each song credits its highest-scoring earlier song (exact ties: the closest version, then the earliest); " +
                       "parent = the most referenced of its strong influencers (score >= ParentFraction x max), ties by score, closeness, earlier",
            ["tree_edges"] = tree.Count,
            ["secondary_edges"] = r.Edges.Count(e => e.Kind == "secondary"),
            ["tree_edges_credited"] = tree.Count(e => e.Credited),
            ["roots"] = r.Roots,
            ["roots_with_children"] = Enumerable.Range(0, n).Count(i => r.Parent[i] < 0 && children[i] > 0),
            ["isolated_roots"] = Enumerable.Range(0, n).Count(i => r.Parent[i] < 0 && children[i] == 0),
            ["depth_histogram"] = Hist(r.Depth),
            ["max_depth"] = r.Depth.Max(),
            ["max_chain_songs"] = r.Depth.Max() + 1,
            ["tree_edges_by_family_kind"] = byKind,
            ["tree_edges_by_primary_channel"] = new JsonObject(tree.GroupBy(e => e.Primary).OrderBy(x => x.Key, StringComparer.Ordinal)
                .Select(x => KeyValuePair.Create(x.Key, (JsonNode?)x.Count()))),
            ["strong_matches"] = new JsonObject
            {
                ["significant_pairs"] = strongPairs.Count,
                ["tree_edges"] = tree.Count(e => e.Family.Kind == FamilyKind.Strong),
                ["secondary_edges"] = r.Edges.Count(e => e.Kind == "secondary" && e.Family.Kind == FamilyKind.Strong),
                ["kept_as_edges"] = r.Edges.Count(e => strongKeys.Contains((e.Pair.A, e.Pair.B))),
            },
            ["largest_subtrees"] = new JsonArray(Enumerable.Range(0, n).Where(i => r.Parent[i] < 0).OrderByDescending(i => r.Descendants[i]).ThenBy(i => i).Take(10)
                .Select(i =>
                {
                    var o = Song(songs[i]);
                    o["descendants"] = r.Descendants[i];
                    o["children"] = children[i];
                    o["max_depth_below"] = Enumerable.Range(0, n).Where(x => r.Root[x] == i).Max(x => r.Depth[x]);
                    return (JsonNode)o;
                }).ToArray()),
            ["longest_chains"] = chains,
            ["hubs"] = Hubs(r),
            ["guard"] = guard,
        };
    }

    private static JsonObject GraphSection(LineageResult r, LineageGraphSummary? g)
    {
        int n = r.Songs.Length;
        var o = new JsonObject
        {
            ["nodes"] = n,
            ["tree_edges"] = r.Edges.Count(e => e.Kind == "tree"),
            ["secondary_edges"] = r.Edges.Count(e => e.Kind == "secondary"),
            ["roots"] = r.Roots,
            ["depth_histogram"] = Hist(r.Depth),
            ["ref_count_histogram"] = Hist(r.RefCount),
            ["most_referenced"] = new JsonArray(Enumerable.Range(0, n).Where(i => r.RefCount[i] > 0).OrderByDescending(i => r.RefCount[i]).ThenByDescending(i => r.Katz[i])
                .ThenBy(i => i).Take(15).Select(i =>
                {
                    var s = Song(r.Songs[i]);
                    s["ref_count"] = r.RefCount[i];
                    s["ref_norm"] = r.RefNorm[i] is double rn ? J(rn) : null;
                    s["katz"] = J(r.Katz[i]);
                    s["descendants"] = r.Descendants[i];
                    return (JsonNode)s;
                }).ToArray()),
        };
        if (g != null)
        {
            o["in_degree_histogram"] = Hist(g.Graph.InDegree);
            o["out_degree_histogram"] = Hist(g.Graph.OutDegree);
            o["excerpts_outside_home_key"] = g.Graph.ExcerptsOutsideHomeKey;
            o["excerpt_sources"] = new JsonObject(g.ExcerptSources.OrderBy(x => x.Key, StringComparer.Ordinal).Select(x => KeyValuePair.Create(x.Key, (JsonNode?)x.Value)));
            o["identity_family_rows"] = g.Families;
            o["song_family_rows"] = g.SongFamilyRows;
        }
        return o;
    }

    /// <summary>Known pairs (canon controls, Wikidata links) against the lineage graph: an edge, the parent, the shared families.</summary>
    private static JsonObject Validation(LineageResult r, List<KnownInfluence> known)
    {
        var songs = r.Songs;
        var idx = songs.ToDictionary(s => s.WorkId, s => s.Index, StringComparer.Ordinal);
        var edge = r.Edges.ToDictionary(e => (e.Pair.A, e.Pair.B), e => e.Kind);
        var summary = new Dictionary<string, Dictionary<string, int>>(StringComparer.Ordinal);
        void Count(string grp, string key)
        {
            if (!summary.TryGetValue(grp, out var m)) summary[grp] = m = new Dictionary<string, int>(StringComparer.Ordinal);
            m[key] = m.GetValueOrDefault(key) + 1;
        }
        var rows = new JsonArray();
        foreach (var k in known)
        {
            string grp = k.Kind switch { "control_negative" => "negative", "control_version" => "version", _ => "positive" };
            Count(grp, "rows");
            if (k.Src == k.Dst || !idx.TryGetValue(k.Src, out int a) || !idx.TryGetValue(k.Dst, out int b)) continue;
            Count(grp, "present");
            (int x, int y) = a < b ? (a, b) : (b, a);
            var pr = r.ByB[y].GetValueOrDefault(x);
            string kind = edge.GetValueOrDefault((x, y)) ?? "none";
            if (pr != null) Count(grp, "share_a_family");
            if (kind != "none") Count(grp, "edge");
            if (r.Parent[y] == x) Count(grp, "parent");
            rows.Add(new JsonObject
            {
                ["kind"] = k.Kind, ["src"] = k.Src, ["dst"] = k.Dst, ["src_title"] = songs[a].Title, ["dst_title"] = songs[b].Title,
                ["edge"] = kind, ["parent_is_src"] = r.Parent[y] == x, ["score"] = pr != null ? J(pr.Score) : null,
                ["shared"] = pr == null ? null : new JsonArray(pr.Parts.OrderByDescending(t => t.C).Select(t => (JsonNode?)JsonValue.Create(t.F.Label)).ToArray()),
            });
        }
        var sum = new JsonObject();
        foreach (var kv in summary.OrderBy(x => x.Key, StringComparer.Ordinal))
            sum[kv.Key] = new JsonObject(kv.Value.OrderBy(x => x.Key, StringComparer.Ordinal).Select(x => KeyValuePair.Create(x.Key, (JsonNode?)x.Value)));
        return new JsonObject { ["summary"] = sum, ["pairs"] = rows };
    }
}
