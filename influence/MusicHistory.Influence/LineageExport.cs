using System.Globalization;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Influence;

/// <summary>What the lineage export wrote, beyond <see cref="GraphSummary"/>.</summary>
internal sealed class LineageGraphSummary
{
    public required GraphSummary Graph;
    public int Families, SongFamilyRows;
    public Dictionary<string, int> ExcerptSources = new(StringComparer.Ordinal);
}

/// <summary>
/// Writes the graph database (DESIGN.md §10) of the identity lineages (§8b) from the pipeline tables only
/// (<c>tree_node</c>, <c>lineage_*</c>, songs and key regions), plus the extra tables <c>identity_family</c> and
/// <c>song_family</c> and <c>graph_meta.edge_semantics = 'identity_lineage'</c>. Edges: <c>evidence</c> = the credited
/// family's label (the viewer's "Shares: ..."), <c>primary_channel</c> = its channel, <c>score_bits</c> = the score,
/// <c>z</c> = the strong match's fused z (0 for a family edge), <c>q</c> = its tail probability, spans = the credited
/// family's first visit in both songs.
/// </summary>
internal static class LineageExport
{
    public static LineageGraphSummary Export(SqliteConnection pipeline, string graphPath, string repoRoot, Params p, LineageParams lp,
        string generatedAt, TextWriter log, Func<string, string?>? env = null)
    {
        var songs = PipelineDb.LoadSongs(pipeline, new LoadStats());
        if (songs.Length == 0) throw new InvalidOperationException("no selected, analyzed songs with a year in the pipeline database");
        var byId = songs.ToDictionary(s => s.WorkId, s => s.Index, StringComparer.Ordinal);
        int n = songs.Length;
        var nodes = PipelineDb.LoadTree(pipeline);
        var missing = songs.Where(s => !nodes.ContainsKey(s.WorkId)).Select(s => s.WorkId).Take(5).ToList();
        if (missing.Count > 0)
            throw new InvalidOperationException($"tree_node has no row for {string.Join(", ", missing)}: run the influence stage ('run') first");
        var families = LineageStore.LoadFamilies(pipeline, songs);
        var famById = families.ToDictionary(f => f.Id);
        var excerpts = LineageStore.LoadExcerpts(pipeline);
        var parent = new int[n];
        for (int i = 0; i < n; i++)
        {
            var node = nodes[songs[i].WorkId];
            parent[i] = node.Parent != null && byId.TryGetValue(node.Parent, out int pi) ? pi : -1;
        }

        // Edges: by target, the tree edge first, then secondary edges by score.
        var stored = LineageStore.LoadEdges(pipeline)
            .Where(e => byId.ContainsKey(e.A) && byId.ContainsKey(e.B))
            .Select(e => (E: e, A: byId[e.A], B: byId[e.B]))
            .OrderBy(x => x.B).ThenBy(x => x.E.Kind == "tree" ? 0 : 1).ThenByDescending(x => x.E.Score).ThenBy(x => x.A).ToList();
        var treeIn = new int[n];
        foreach (var (e, a, b) in stored)
        {
            if (a >= b || !DateOrder.Earlier(songs[a].Date, songs[b].Date) || songs[a].TimeValue >= songs[b].TimeValue)
                throw new InvalidOperationException($"edge {e.A} -> {e.B} is not earlier -> later");
            if (!famById.ContainsKey(e.FamilyId)) throw new InvalidOperationException($"lineage_edge {e.A} -> {e.B}: unknown family {e.FamilyId}");
            if (e.Kind == "tree")
            {
                treeIn[b]++;
                if (parent[b] != a) throw new InvalidOperationException($"tree edge into {e.B} does not come from its tree parent");
            }
        }
        for (int i = 0; i < n; i++)
            if (treeIn[i] != (parent[i] >= 0 ? 1 : 0))
                throw new InvalidOperationException($"{songs[i].WorkId}: {treeIn[i]} tree edges in, parent {parent[i]}");

        var summary = new GraphSummary { Nodes = n, InDegree = new int[n], OutDegree = new int[n], Excerpts = new (double, double)[n] };
        var result = new LineageGraphSummary { Graph = summary };
        for (int i = 0; i < n; i++)
        {
            if (excerpts.TryGetValue(songs[i].WorkId, out var ex))
            {
                summary.Excerpts[i] = (ex.Start, ex.End);
                string src = ex.Source.Split(':')[0];
                result.ExcerptSources[src] = result.ExcerptSources.GetValueOrDefault(src) + 1;
            }
            else throw new InvalidOperationException($"lineage_excerpt has no row for {songs[i].WorkId}: run the influence stage ('run') first");
        }
        var regions = PipelineDb.LoadKeyRegions(pipeline, byId.Keys.ToHashSet(StringComparer.Ordinal));
        var local = songs.Select(s => KeyRegions.Local(s, regions.GetValueOrDefault(s.WorkId), summary.Excerpts[s.Index].Start, summary.Excerpts[s.Index].End)).ToArray();
        summary.ExcerptsOutsideHomeKey = local.Count(k => k.EntryTonic != null || k.ExitTonic != null);

        int id = 0;
        foreach (var (e, a, b) in stored)
        {
            var f = famById[e.FamilyId];
            double sim = 1 - Math.Pow(2, -e.Score / lp.LineageHalfBits);
            double weight = e.Kind == "tree" ? 0.5 + 0.5 * sim : sim;
            bool strong = f.Kind == FamilyKind.Strong;
            var seg = new Segment { AStart = e.AStart, AEnd = e.AEnd, BStart = e.BStart, BEnd = e.BEnd };
            summary.EdgeRows.Add(new GraphEdge(++id, a + 1, b + 1, e.Kind, e.Channels, f.Channel, e.Score, strong ? f.Z : 0,
                strong && !double.IsNaN(f.Q) ? f.Q : null, sim, weight, seg, f.Label));
            summary.InDegree[b]++;
            summary.OutDegree[a]++;
            if (e.Kind == "tree") summary.TreeEdges++; else summary.SecondaryEdges++;
        }
        summary.Edges = summary.EdgeRows.Count;
        summary.Roots = parent.Count(x => x < 0);
        summary.MinTime = songs.Min(s => s.TimeValue);
        summary.MaxTime = songs.Max(s => s.TimeValue);

        var settings = ExportSettings.Resolve(pipeline, byId.Keys, env ?? Environment.GetEnvironmentVariable);
        summary.Settings = settings;
        foreach (string w in settings.Warnings) log.WriteLine($"WARNING: graph_meta: {w}");
        string? resonance = songs.Where(s => !string.IsNullOrEmpty(s.ResonanceCommit)).GroupBy(s => s.ResonanceCommit!)
            .OrderByDescending(g => g.Count()).ThenBy(g => g.Key, StringComparer.Ordinal).Select(g => g.Key).FirstOrDefault();
        var meta = new List<(string, string?)>
        {
            ("schema_version", Schema.GraphSchemaVersion.ToString(CultureInfo.InvariantCulture)),
            ("generated_at", generatedAt),
            ("normalization", settings.Normalization),
            ("target_key", settings.TargetKey),
            ("target_bpm", ExportSettings.Fmt(settings.TargetBpm)),
            ("min_time", summary.MinTime.ToString("R", CultureInfo.InvariantCulture)),
            ("max_time", summary.MaxTime.ToString("R", CultureInfo.InvariantCulture)),
            ("song_count", n.ToString(CultureInfo.InvariantCulture)),
            ("edge_count", summary.Edges.ToString(CultureInfo.InvariantCulture)),
            ("root_count", summary.Roots.ToString(CultureInfo.InvariantCulture)),
            ("resonance_commit", resonance),
            ("pipeline_commit", RepoRoot.GitHead(repoRoot)),
            ("midi_base", "relative to this file's folder"),
            ("edge_semantics", LineageStore.Lineage),
            ("family_count", families.Count.ToString(CultureInfo.InvariantCulture)),
        };
        var exported = new HashSet<string>(songs.Select(s => s.WorkId), StringComparer.Ordinal);
        var extras = new List<string>();
        using (var cmd = pipeline.CreateCommand())
        {
            cmd.CommandText = "SELECT work_id FROM work WHERE selected = 2 ORDER BY work_id";
            using var r = cmd.ExecuteReader();
            while (r.Read())
                if (exported.Contains(r.GetString(0))) extras.Add(r.GetString(0));
        }
        meta.Add(("validation_extras", string.Join(",", extras)));
        foreach (var kv in GraphExport.InfluenceMeta(pipeline)) meta.Add(kv);

        var famRows = families.Where(f => f.Size > 0).ToList();
        var memberRows = famRows.SelectMany(f => f.Members.Select(m => new object?[] { m.Song + 1, f.Id, m.Strength, m.First }))
            .OrderBy(r => (int)r[0]!).ThenBy(r => (int)r[1]!).ToList();
        result.Families = famRows.Count;
        result.SongFamilyRows = memberRows.Count;

        string graphDir = Path.GetDirectoryName(Path.GetFullPath(graphPath))!;
        Directory.CreateDirectory(graphDir);
        string tmp = Path.GetFullPath(graphPath) + ".tmp";
        foreach (var f in new[] { tmp, tmp + "-journal" })
            if (File.Exists(f)) File.Delete(f);
        using (var g = new SqliteConnection(new SqliteConnectionStringBuilder { DataSource = tmp, Mode = SqliteOpenMode.ReadWriteCreate, Pooling = false }.ToString()))
        {
            g.Open();
            PipelineDb.Exec(g, "PRAGMA journal_mode=DELETE");
            PipelineDb.Exec(g, "PRAGMA page_size=4096");
            PipelineDb.Exec(g, Schema.Graph);
            PipelineDb.Exec(g, Schema.GraphLineage);
            using var tx = g.BeginTransaction();
            GraphExport.Insert(g, tx, "INSERT INTO graph_meta(key, value) VALUES ($1, $2)", meta.Select(m => new object?[] { m.Item1, m.Item2 }));
            GraphExport.Insert(g, tx, "INSERT INTO nodes(id, position_x, position_y, position_z) VALUES ($1, NULL, NULL, NULL)",
                songs.Select(s => new object?[] { s.Index + 1 }));
            GraphExport.Insert(g, tx, """
                INSERT INTO song_node(node_id, work_id, title, artist, year, release_date, date_precision, time_value, canon_rank,
                  tonic_pc, mode, key_name, norm_shift, native_bpm, beats_per_bar, first_downbeat, midi_path, normalized_midi_path,
                  midi_source, excerpt_start_beat, excerpt_end_beat, entry_tonic_pc, entry_mode, exit_tonic_pc, exit_mode,
                  tree_parent_node, tree_root_node, tree_depth, ref_count,
                  ref_norm, katz, descendants, in_degree, out_degree, key_confidence, melody_confidence, main_loop, summary)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22,
                  $23, $24, $25, $26, $27, $28, $29, $30, $31, $32, $33, $34, $35, $36, $37, $38)
                """,
                songs.Select(s =>
                {
                    var node = nodes[s.WorkId];
                    int root = byId.TryGetValue(node.Root, out int ri) ? ri : s.Index;
                    return new object?[]
                    {
                        s.Index + 1, s.WorkId, s.Title, s.Artist, s.Year, s.ReleaseDate, s.DatePrecision, s.TimeValue, s.CanonRank,
                        ((s.TonicPc % 12) + 12) % 12, s.Mode, Keys.Name(s.TonicPc, s.Mode), s.NormShift, s.NativeBpm, s.BeatsPerBar,
                        s.FirstDownbeat, RepoRoot.Relative(s.MidiPath, repoRoot, graphDir),
                        s.NormalizedMidiPath is { Length: > 0 } nm ? RepoRoot.Relative(nm, repoRoot, graphDir) : null,
                        s.MidiSource, summary.Excerpts[s.Index].Start, summary.Excerpts[s.Index].End,
                        local[s.Index].EntryTonic, local[s.Index].EntryMode, local[s.Index].ExitTonic, local[s.Index].ExitMode,
                        parent[s.Index] >= 0 ? parent[s.Index] + 1 : null, root + 1, node.Depth,
                        node.RefCount, node.RefNorm, node.Katz ?? 0, node.Descendants,
                        summary.InDegree[s.Index], summary.OutDegree[s.Index], s.KeyConfidence, s.MelodyConfidence, s.MainLoop,
                        GraphExport.SummaryText(s),
                    };
                }));
            GraphExport.Insert(g, tx, """
                INSERT INTO influence_edges(id, source_node, target_node, kind, channels, primary_channel, score_bits, z, q,
                  similarity, weight, src_start_beat, src_end_beat, dst_start_beat, dst_end_beat, evidence)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                """,
                summary.EdgeRows.Select(e => new object?[]
                {
                    e.Id, e.Source, e.Target, e.Kind, e.ChannelsCsv, e.Primary, e.S, e.Z, e.Q, e.Similarity, e.Weight,
                    e.Seg?.AStart, e.Seg?.AEnd, e.Seg?.BStart, e.Seg?.BEnd, e.Evidence,
                }));
            GraphExport.Insert(g, tx, "INSERT INTO identity_family(family_id, label, kind, roman, size) VALUES ($1, $2, $3, $4, $5)",
                famRows.Select(f => new object?[] { f.Id, f.Label, Family.KindName(f.Kind), f.Roman, f.Size }));
            GraphExport.Insert(g, tx, "INSERT INTO song_family(node_id, family_id, strength, first_beat) VALUES ($1, $2, $3, $4)", memberRows);
            tx.Commit();
            summary.ContentSha256 = GraphExport.ContentHash(g,
                ["SELECT * FROM identity_family ORDER BY family_id", "SELECT * FROM song_family ORDER BY node_id, family_id"]);
        }
        SqliteConnection.ClearAllPools();
        File.Move(tmp, graphPath, overwrite: true);
        log.WriteLine($"graph (identity lineages): {n} nodes, {summary.Edges} edges ({summary.TreeEdges} tree, {summary.SecondaryEdges} secondary), " +
                      $"{summary.Roots} roots, {famRows.Count} families ({memberRows.Count} song_family rows), {summary.ExcerptsOutsideHomeKey} excerpts " +
                      $"entering or leaving outside the home key; normalization {settings.Normalization} ({settings.NormalizationSource}) -> {graphPath}");
        return result;
    }
}
