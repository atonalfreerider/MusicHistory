#nullable enable
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using Mono.Data.Sqlite;
using MusicHistory.Playback;
using MusicHistory.Viewer;
using MusicHistory.Walkthrough;
using UnityEngine;
using Object = UnityEngine.Object;

namespace MusicHistory.EditorTools
{
    /// <summary>
    /// Identity lineages (docs/DESIGN.md §8b) in the viewer: graph_meta.edge_semantics and the
    /// optional identity_family / song_family tables, the "Shares: …" HUD and edge cards (never
    /// bits), the legend, and the family walkthrough (time order of song_family, morph from the
    /// song heard before). Runs on data/graph/music_graph.db when it is an identity-lineage graph
    /// (or -validationLineageDb &lt;path&gt;), plus a copy of it stripped to an older, strict-evidence
    /// database. Writes lineage_overview.png, lineage_walkthrough.png, lineage_edge_card.png and
    /// family_tour.png.
    /// </summary>
    public static partial class Validation
    {
        /// <summary>The HUD says what an edge is: bits and z for strict evidence, "Shares: …" for identity lineages.</summary>
        static void CheckHudSemantics(Report report, SongGraphLoader loader, SongNode focus, string view)
        {
            SongGraphData data = loader.Data!;
            string info = loader.Hud.InfoText;
            string legend = loader.Hud.LegendText;
            if (data.IsIdentityLineage)
            {
                report.Check($"{view} (identity lineage): HUD says 'Shares:' and never 'bits'",
                    focus.TreeEdge == null ? !info.Contains("bits") : info.Contains("Shares:") && !info.Contains("bits"), FirstLines(info, 9));
                report.Check($"{view} (identity lineage): legend explains shared identities, not proven copying",
                    legend.Contains("share a musical identity") && legend.Contains("not proven copying") && legend.Contains("strong match") && !legend.Contains("influencer"));
            }
            else
            {
                report.Check($"{view} (strict evidence): HUD shows the tree edge's bits and z, no 'Shares:'",
                    focus.TreeEdge != null && info.Contains(" bits · z ") && !info.Contains("Shares:"), FirstLines(info, 9));
                report.Check($"{view} (strict evidence): legend keeps the influence wording",
                    legend.Contains(" influences (") && legend.Contains("wide end = influencer") && !legend.Contains("not proven copying"));
            }
        }

        static void ValidateLineage(Report report, SongGraphLoader loader, Camera cam, string outDir, int width, int height)
        {
            // Pure checks first: semantics parsing and the M key cycle.
            SongGraphData probe = new();
            bool missing = probe.EdgeSemantics == SongGraphData.StrictEvidenceSemantics && !probe.IsIdentityLineage;
            probe.Meta["edge_semantics"] = "strict_evidence";
            bool strict = !probe.IsIdentityLineage;
            probe.Meta["edge_semantics"] = " Identity_Lineage ";
            bool lineage = probe.IsIdentityLineage;
            report.Check("edge_semantics: missing = strict_evidence, 'strict_evidence', 'identity_lineage' (trimmed, any case)", missing && strict && lineage);
            report.Check("M cycles lineage → subtree → chronological → family → lineage (family only with families)",
                TourPlanner.NextMode(TourMode.Lineage, true) == TourMode.Subtree && TourPlanner.NextMode(TourMode.Subtree, true) == TourMode.Chronological &&
                TourPlanner.NextMode(TourMode.Chronological, true) == TourMode.Family && TourPlanner.NextMode(TourMode.Family, true) == TourMode.Lineage &&
                TourPlanner.NextMode(TourMode.Chronological, false) == TourMode.Lineage);

            string? explicitDb = PathArg("-validationLineageDb");
            string dbPath = explicitDb ?? Path.Combine(SongGraphLoader.RepoRoot(), "data", "graph", "music_graph.db");
            string? semantics = File.Exists(dbPath) ? MetaText(dbPath, "edge_semantics") : null;
            if (semantics != SongGraphData.IdentityLineageSemantics)
            {
                if (explicitDb != null) report.Check("-validationLineageDb is an identity-lineage graph", false, $"{dbPath}: edge_semantics {semantics ?? "missing"}");
                else report.Text("lineage_db", $"skipped: {dbPath} has edge_semantics {semantics ?? "missing"}");
                return;
            }
            report.Text("lineage_db", dbPath);
            loader.Build(dbPath);
            SongGraphData data = loader.Data!;
            report.Check("lineage: reads graph_meta.edge_semantics = identity_lineage", data.IsIdentityLineage && data.EdgeSemantics == SongGraphData.IdentityLineageSemantics);
            report.Check("lineage: §10 and family invariants hold", data.Problems.Count == 0, string.Join(" | ", data.Problems.Take(5)));

            using SqliteConnection conn = new("URI=file:" + Path.GetFullPath(dbPath));
            conn.Open();
            int dbFamilies = Count(conn, "SELECT COUNT(*) FROM identity_family");
            int dbRows = Count(conn, "SELECT COUNT(*) FROM song_family");
            int readRows = data.SongFamilies.Sum(l => l.Count);
            report.Number("lineage_families", data.Families.Count, "0");
            report.Number("lineage_song_family_rows", readRows, "0");
            report.Check("lineage: identity_family and song_family read in full", data.Families.Count == dbFamilies && readRows == dbRows &&
                data.Families.Values.All(f => f.Members.Count == f.Size), $"{data.Families.Count}/{dbFamilies} families, {readRows}/{dbRows} rows");

            // Edge → family, independently in SQL: the family both songs share whose label is the evidence.
            Dictionary<int, (int count, int family)> sqlFamily = new();
            using (SqliteCommand cmd = conn.CreateCommand())
            {
                cmd.CommandText =
                    "SELECT e.id, COUNT(f.family_id), MIN(f.family_id) FROM influence_edges e " +
                    "JOIN song_family a ON a.node_id = e.source_node " +
                    "JOIN song_family b ON b.node_id = e.target_node AND b.family_id = a.family_id " +
                    "JOIN identity_family f ON f.family_id = a.family_id AND f.label = e.evidence GROUP BY e.id";
                using SqliteDataReader r = cmd.ExecuteReader();
                while (r.Read()) sqlFamily[Convert.ToInt32(r.GetValue(0))] = (Convert.ToInt32(r.GetValue(1)), Convert.ToInt32(r.GetValue(2)));
            }
            int resolved = data.Edges.Count(e => e.FamilyId != null);
            bool sameAsSql = data.Edges.All(e => sqlFamily.TryGetValue(e.Id, out var s) && s.count == 1 && s.family == e.FamilyId);
            report.Check("lineage: every edge's family = the one family both songs share named by its evidence (SQL)",
                resolved == data.Edges.Count && sameAsSql, $"{resolved}/{data.Edges.Count} edges resolved");
            int strongEdges = data.Edges.Count(e => e.IsStrongMatch);
            int positiveZ = Count(conn, "SELECT COUNT(*) FROM influence_edges WHERE z > 0");
            report.Number("lineage_strong_match_edges", strongEdges, "0");
            report.Check("lineage: strong matches = edges with z > 0 = edges of 'strong' families", strongEdges == positiveZ &&
                data.Edges.All(e => e.IsStrongMatch == (e.Z > 0) && e.IsStrongMatch == (data.Family(e.FamilyId)?.IsStrong ?? false)),
                $"{strongEdges} strong, {positiveZ} with z > 0");
            report.Check("lineage: strong-match tree edges drawn in the bright Strong tier", loader.Edges.Where(e => e.IsTree)
                .All(e => (e.Tier == EdgeTier.Strong) == e.Record.IsStrongMatch));

            ValidateFamilyPlanning(report, data, conn);
            ValidateFamilyWindows(report, data);

            // Overview.
            loader.Highlighter.Select(null);
            loader.Highlighter.ApplyFocus(null, force: true);
            loader.FrameOverview(cam);
            string overview = Path.Combine(outDir, "lineage_overview.png");
            Capture(cam, loader, overview, width, height, out _);
            report.Check("lineage_overview.png written", File.Exists(overview) && new FileInfo(overview).Length > 10000, overview);
            CheckLabels(report, loader, "lineage overview");
            string legend = loader.Hud.LegendText;
            report.Check("lineage legend: edges are shared musical identities, not proven copying; strong matches highlighted",
                legend.Contains("share a musical identity") && legend.Contains("not proven copying") &&
                legend.Contains("strong match") && legend.Contains($"({strongEdges} edges)") && legend.Contains($"{data.Families.Count} families") &&
                !legend.Contains("influencer") && !legend.Contains(" influences ("), FirstLines(legend, 8));

            ValidateLineageHud(report, loader, cam, outDir, width, height);
            ValidateLineageTour(report, loader, cam, outDir, width, height);
            ValidateFamilyTour(report, loader, cam, outDir, width, height);
            conn.Close();
            ValidateStrippedLineage(report, loader, dbPath);
        }

        /// <summary>Family choice (edge, strongest, largest) and tour order against SQL.</summary>
        static void ValidateFamilyPlanning(Report report, SongGraphData data, SqliteConnection conn)
        {
            // Tour order = time order of song_family members, for every family.
            Dictionary<int, List<int>> sqlOrder = new();
            using (SqliteCommand cmd = conn.CreateCommand())
            {
                cmd.CommandText = "SELECT sf.family_id, sf.node_id FROM song_family sf JOIN song_node s ON s.node_id = sf.node_id " +
                                  "ORDER BY sf.family_id, s.time_value, s.work_id";
                using SqliteDataReader r = cmd.ExecuteReader();
                while (r.Read())
                {
                    int f = Convert.ToInt32(r.GetValue(0));
                    if (!sqlOrder.TryGetValue(f, out List<int>? list)) sqlOrder[f] = list = new List<int>();
                    list.Add(Convert.ToInt32(r.GetValue(1)));
                }
            }
            int mismatched = 0, unsorted = 0;
            foreach (IdentityFamily f in data.Families.Values)
            {
                List<int> plan = TourPlanner.Family(data, f.FamilyId);
                if (!sqlOrder.TryGetValue(f.FamilyId, out List<int>? expected) || !plan.SequenceEqual(expected)) mismatched++;
                for (int i = 1; i < plan.Count; i++)
                    if (data.Song(plan[i]).TimeValue < data.Song(plan[i - 1]).TimeValue) { unsorted++; break; }
            }
            report.Check("family tour order = time order of song_family members (every family, SQL ORDER BY time_value, work_id)",
                mismatched == 0 && unsorted == 0, $"{data.Families.Count} families, {mismatched} differ, {unsorted} out of time order");

            // The selected song's strongest family, independently in SQL.
            int checkedSongs = 0, wrong = 0;
            using (SqliteCommand cmd = conn.CreateCommand())
            {
                cmd.CommandText = "SELECT sf.family_id FROM song_family sf JOIN identity_family f ON f.family_id = sf.family_id " +
                                  "WHERE sf.node_id = @n ORDER BY (f.size >= 2) DESC, sf.strength DESC, f.size ASC, sf.family_id ASC LIMIT 1";
                SqliteParameter node = cmd.CreateParameter();
                node.ParameterName = "@n";
                cmd.Parameters.Add(node);
                foreach (SongRecord s in data.Songs)
                {
                    node.Value = s.NodeId;
                    object? v = cmd.ExecuteScalar();
                    int? expected = v == null || v is DBNull ? null : Convert.ToInt32(v, CultureInfo.InvariantCulture);
                    if (TourPlanner.StrongestFamily(data, s.NodeId) != expected) wrong++;
                    if (expected != null) checkedSongs++;
                }
            }
            report.Check("family choice without an edge: the song's strongest shared family (SQL: strength, then smaller, then id)", wrong == 0,
                $"{checkedSongs} songs with families, {wrong} differ");
            int largest = Count(conn, "SELECT family_id FROM identity_family ORDER BY size DESC, family_id ASC LIMIT 1");
            EdgeRecord anyEdge = data.Edges.First(e => e.FamilyId != null && data.Family(e.FamilyId)!.Members.Count >= 3);
            SongRecord? orphan = data.Songs.FirstOrDefault(s => data.FamiliesOf(s.NodeId).Count == 0);
            report.Check("family choice: the selected edge's family first, else the song's strongest, else the largest",
                TourPlanner.FamilyFor(data, anyEdge.Target, anyEdge) == anyEdge.FamilyId &&
                TourPlanner.FamilyFor(data, anyEdge.Target, null) == TourPlanner.StrongestFamily(data, anyEdge.Target) &&
                TourPlanner.FamilyFor(data, null, null) == largest &&
                (orphan == null || (TourPlanner.FamilyFor(data, orphan.NodeId, null) == null && TourPlanner.Plan(data, TourMode.Family, orphan.NodeId).Count == 0)) &&
                TourPlanner.Plan(data, TourMode.Family, null).SequenceEqual(TourPlanner.Family(data, largest)),
                $"edge {anyEdge.Id} → family {anyEdge.FamilyId}; largest {largest}; song without families: {orphan?.NodeId.ToString(CultureInfo.InvariantCulture) ?? "none"}");
        }

        /// <summary>Every family member's window: bar aligned, non-empty, containing the identity's first visit.</summary>
        static void ValidateFamilyWindows(Report report, SongGraphData data)
        {
            Dictionary<string, int> sources = new();
            int bad = 0, total = 0;
            string firstBad = "";
            foreach (IdentityFamily f in data.Families.Values)
            {
                if (f.Members.Count < 2) continue;
                foreach (FamilyMember m in f.Members)
                {
                    total++;
                    SongRecord s = data.Song(m.NodeId);
                    FamilyWindow w = TourPlanner.Window(data, f.FamilyId, m.NodeId);
                    sources[w.Source] = sources.TryGetValue(w.Source, out int c) ? c + 1 : 1;
                    double bpb = s.BeatsPerBar > 0 ? s.BeatsPerBar : 4;
                    double bars = (w.Start - s.FirstDownbeat) / bpb;
                    // Positions are single precision as read (TourPlanner.BeatTolerance). Windows never start
                    // before the first downbeat, so an identity heard in a pickup counts from there.
                    bool ok = w.End > w.Start && Math.Abs(bars - Math.Round(bars)) * bpb < TourPlanner.BeatTolerance &&
                              (m.FirstBeat is not double first ||
                               (w.Start <= Math.Max(first, s.FirstDownbeat) + TourPlanner.BeatTolerance && first < w.End));
                    SongClip clip = TourPlanner.FamilyClip(data, f.FamilyId, m.NodeId);
                    bool own = w.Source == FamilyWindow.OwnExcerpt;
                    ok &= clip.ExcerptStartBeat == w.Start && clip.ExcerptEndBeat == w.End &&
                          (own ? clip.EntryTonicPc == s.EntryTonicPc && clip.ExitTonicPc == s.ExitTonicPc
                               : clip.EntryTonicPc == null && clip.ExitTonicPc == null && clip.EntryMinor == null && clip.ExitMinor == null);
                    if (w.Source == FamilyWindow.FirstVisit) ok &= Math.Abs(w.End - w.Start - TourPlanner.FirstVisitBars * bpb) < 1e-9;
                    if (!ok && bad++ == 0) firstBad = $"family {f.FamilyId} node {m.NodeId}: {w.Start}–{w.End} ({w.Source}), first {m.FirstBeat}";
                }
            }
            foreach (KeyValuePair<string, int> kv in sources) report.Number($"family_windows_{kv.Key.Replace(' ', '_')}", kv.Value, "0");
            report.Check("family windows: bar aligned, contain the identity's first visit; own excerpt keeps its keys, other windows the home key",
                bad == 0 && total > 0, $"{total} memberships in shared families; {bad} bad {firstBad}");
        }

        /// <summary>Hover and edge cards on an identity-lineage graph.</summary>
        static void ValidateLineageHud(Report report, SongGraphLoader loader, Camera cam, string outDir, int width, int height)
        {
            SongGraphData data = loader.Data!;
            SongNode family = loader.Nodes.First(n => n.TreeEdge != null && !n.TreeEdge.Record.IsStrongMatch);
            IdentityFamily f = data.Family(family.TreeEdge!.Record.FamilyId)!;
            loader.Highlighter.ApplyFocus(family, force: true);
            string info = loader.Hud.InfoText;
            report.Check("lineage hover: 'Shares: <identity>' with the family size, no bits",
                info.Contains("Shares:") && info.Contains(GraphHud.Esc(f.Label)) && info.Contains($"family of {f.Members.Count} songs") &&
                !info.Contains("bits") && !info.Contains("Influenced by"), FirstLines(info, 9));
            CheckHudSemantics(report, loader, family, "lineage hover");

            SongNode strong = loader.Nodes.First(n => n.TreeEdge != null && n.TreeEdge.Record.IsStrongMatch);
            EdgeRecord se = strong.TreeEdge!.Record;
            loader.Highlighter.ApplyFocus(strong, force: true);
            info = loader.Hud.InfoText;
            string z = $"strong match</b> (z {se.Z.ToString("0.0", CultureInfo.InvariantCulture)})";
            report.Check("lineage hover on a strong match: 'strong match (z …)', no bits", info.Contains("Shares:") && info.Contains(z) &&
                info.Contains(GraphHud.Esc(data.Family(se.FamilyId)!.Label)) && !info.Contains("bits"), $"node {strong.NodeId}: {FirstLines(info, 9)}");

            SongNode? root = loader.Nodes.FirstOrDefault(n => n.TreeEdge == null && data.FamiliesOf(n.NodeId).Any(m => data.Families[m.FamilyId].Size >= 2));
            if (root != null)
            {
                loader.Highlighter.ApplyFocus(root, force: true);
                report.Check("lineage hover on a root: says it roots a lineage tree, no bits",
                    loader.Hud.InfoText.Contains("Root of its lineage tree") && !loader.Hud.InfoText.Contains("bits"));
            }
            loader.Highlighter.ApplyFocus(null, force: true);

            // Edge card: click (pick) an edge, then its card and highlight.
            InfluenceEdge familyEdge = family.TreeEdge!;
            List<(Vector3, float)> items = new() { (familyEdge.Source.transform.position, familyEdge.Source.Radius), (familyEdge.Target.transform.position, familyEdge.Target.Radius) };
            (Vector3 pos, Quaternion rot) = CameraFraming.Frame(cam, items, loader.Frame.ViewForward, 1.3f, 6f, new Rect(.05f, .15f, .6f, .6f));
            cam.transform.SetPositionAndRotation(pos, rot);
            Vector3 a = cam.WorldToScreenPoint(familyEdge.Source.transform.position), b = cam.WorldToScreenPoint(familyEdge.Target.transform.position);
            Vector2 mid = (a + b) * .5f;
            InfluenceEdge? picked = loader.Highlighter.PickEdge(cam, mid, loader.Highlighter.EdgePickPixels, out float pixels);
            report.Check("edge click: the pick finds a drawn edge through the clicked point", picked != null && pixels <= 1f,
                $"{(picked != null ? picked.name : "none")} at {pixels:0.00} px");

            loader.Highlighter.SelectEdge(familyEdge);
            info = loader.Hud.InfoText;
            HashSet<int> members = new(f.Members.Select(m => m.NodeId));
            report.Check("edge card: 'Shares: <identity>', family size, not proven copying, no bits",
                loader.Highlighter.FocusEdge == familyEdge && info.Contains("Shares:") && info.Contains(GraphHud.Esc(f.Label)) &&
                info.Contains($"family of {f.Members.Count} songs") && info.Contains("not proven copying") && !info.Contains("bits"), FirstLines(info, 9));
            report.Check("edge card: the edge lights up, its songs glow, the family stays undimmed, the rest dims",
                familyEdge.State == EdgeState.Highlight && familyEdge.Target.State == BubbleState.Focus && familyEdge.Source.State == BubbleState.Related &&
                loader.Nodes.All(n => n == familyEdge.Source || n == familyEdge.Target || (n.State == BubbleState.Normal) == members.Contains(n.NodeId)) &&
                loader.Edges.Where(e => e.Record.FamilyId == f.FamilyId).All(e => e.Shown) &&
                loader.Edges.Where(e => e != familyEdge).All(e => (e.State == EdgeState.Normal) == (e.Record.FamilyId == f.FamilyId)),
                $"family {f.FamilyId} '{f.Label}', {members.Count} songs");

            InfluenceEdge strongEdge = strong.TreeEdge!;
            loader.Highlighter.SelectEdge(strongEdge);
            info = loader.Hud.InfoText;
            report.Check("edge card on a strong match: 'strong match (z …)', exact shared passage, no bits",
                info.Contains(z) && info.Contains("exact shared passage") && !info.Contains("bits"), FirstLines(info, 9));
            items = new() { (strongEdge.Source.transform.position, strongEdge.Source.Radius * 1.4f), (strongEdge.Target.transform.position, strongEdge.Target.Radius * 1.4f) };
            (pos, rot) = CameraFraming.Frame(cam, items, loader.Frame.ViewForward, 1.35f, 7f, new Rect(.14f, .24f, .56f, .46f));
            cam.transform.SetPositionAndRotation(pos, rot);
            string card = Path.Combine(outDir, "lineage_edge_card.png");
            loader.Director.Mode = TourMode.Family;
            loader.Director.Tick(0f); // the idle panel follows the selection each frame
            report.Check("edge card: the idle walkthrough panel offers the edge's family", loader.Director.HudText.Contains("the selected edge's identity"),
                FirstLines(loader.Director.HudText, 2));
            Capture(cam, loader, card, width, height, out _);
            loader.Director.Mode = TourMode.Lineage;
            report.Check("lineage_edge_card.png written", File.Exists(card));
            CheckLabels(report, loader, "lineage edge card");
            loader.Highlighter.Select(null);
            report.Check("edge card cleared by selecting nothing", loader.Highlighter.FocusEdge == null && loader.Highlighter.SelectedEdge == null &&
                loader.Edges.Where(e => !e.IsTree).All(e => !e.Shown) && !loader.Hud.InfoVisible);
        }

        /// <summary>A lineage-mode tour on an identity-lineage graph: its panel names the shared identity.</summary>
        static void ValidateLineageTour(Report report, SongGraphLoader loader, Camera cam, string outDir, int width, int height)
        {
            SongGraphData data = loader.Data!;
            WalkthroughDirector director = loader.Director;
            int deepest = TourPlanner.DefaultStart(data, TourMode.Lineage);
            report.Check("lineage tour starts", director.StartTour(TourMode.Lineage, loader.NodeById(deepest)));
            if (director.Steps.Count < 2) return;
            director.GoTo(director.Steps.Count - 2);
            director.Next();
            InfluenceEdge? edge = director.StepEdge;
            IdentityFamily? f = data.Family(edge?.Record.FamilyId);
            report.Check("lineage tour: morphs from the song heard before", director.PreviousClip?.NodeId == director.Steps[^2] &&
                director.CurrentPlan.MorphBeats > 0);
            string hud = director.HudText;
            report.Check("lineage tour: panel says 'Shares: <identity> · family of N songs', never bits", edge != null && f != null &&
                hud.Contains("Shares:") && hud.Contains(GraphHud.Esc(f.Label)) && hud.Contains($"family of {f.Members.Count} songs") &&
                (!edge.Record.IsStrongMatch || hud.Contains("strong match")) && !hud.Contains("bits"), FirstLines(hud, 4));
            for (int i = 0; i < 45; i++) TickDirector(director, .1f);
            string path = Path.Combine(outDir, "lineage_walkthrough.png");
            Capture(cam, loader, path, width, height, out _);
            report.Check("lineage_walkthrough.png written", File.Exists(path), $"step {director.StepIndex + 1}/{director.Steps.Count}: {FirstLines(hud, 3)}");
            CheckLabels(report, loader, "lineage walkthrough");
            director.Exit();
        }

        /// <summary>Family mode: the tour, its morphs, its panel and a frame.</summary>
        static void ValidateFamilyTour(Report report, SongGraphLoader loader, Camera cam, string outDir, int width, int height)
        {
            SongGraphData data = loader.Data!;
            WalkthroughDirector director = loader.Director;
            // A family whose second song plays outside its own excerpt, so the window/key rule is exercised.
            IdentityFamily fam = data.Families.Values.Where(x => x.Members.Count >= 4)
                .OrderByDescending(x => x.Members.Count).ThenBy(x => x.FamilyId)
                .FirstOrDefault(x => TourPlanner.Window(data, x.FamilyId, x.Members[1].NodeId).Source != FamilyWindow.OwnExcerpt)
                ?? data.Family(TourPlanner.LargestFamily(data))!;
            EdgeRecord record = data.EdgesOfFamily(fam.FamilyId).OrderByDescending(e => e.IsTree).ThenBy(e => e.Id).First();
            InfluenceEdge edge = loader.Edges.First(e => ReferenceEquals(e.Record, record));
            List<int> expected = TourPlanner.Family(data, fam.FamilyId);
            report.Text("family_tour", $"family {fam.FamilyId} '{fam.Label}' ({fam.Kind}), {expected.Count} songs, from edge {record.Id}");

            // Idle panel in family mode names what Enter will play.
            loader.Highlighter.SelectEdge(edge);
            director.Mode = TourMode.Family;
            director.Tick(0f);
            report.Check("family mode idle panel: Enter plays the selected edge's family (label, size)",
                director.HudText.Contains("family") && director.HudText.Contains(GraphHud.Esc(fam.Label)) &&
                director.HudText.Contains($"({expected.Count} songs; the selected edge's identity)"), FirstLines(director.HudText, 2));

            report.Check("family tour starts from the selected edge", director.StartTour(TourMode.Family, null, edge) &&
                director.FamilyId == fam.FamilyId && director.Steps.SequenceEqual(expected), $"{director.Steps.Count} steps");
            SongClip first = director.CurrentClip!;
            FamilyWindow w0 = TourPlanner.Window(data, fam.FamilyId, expected[0]);
            report.Check("family tour: step 1 plays natively, at the family's window", director.PreviousClip == null &&
                director.CurrentPlan.MorphBeats == 0 && first.NodeId == expected[0] && first.ExcerptStartBeat == w0.Start && first.ExcerptEndBeat == w0.End);

            director.Next();
            SongClip clip = director.CurrentClip!, prev = director.PreviousClip!;
            FamilyWindow w1 = TourPlanner.Window(data, fam.FamilyId, expected[1]);
            MorphPlan playing = director.ActivePlayer!.CurrentPlan;
            MorphPlan silentPlan = Morph.Plan(prev, clip, director.MorphBars);
            bool keyOk = Math.Abs(playing.StartSemitones - Morph.Wrap(prev.ExitTonic - clip.EntryTonic)) < 1e-9 &&
                         Math.Abs(playing.MorphBeats - director.MorphBars * clip.BeatsPerBar) < 1e-9;
            report.Check("family tour: step 2 morphs from the song heard before (key and glide from the player's plan)",
                prev.NodeId == expected[0] && clip.NodeId == expected[1] && keyOk && playing.MorphBeats > 0 &&
                (director.ActivePlayer is not SilentSongPlayer || SamePlan(playing, silentPlan)),
                $"{prev.NodeId} → {clip.NodeId}: {playing.StartSemitones:+0;-0;0} st, tempo ×{playing.StartTempoRatio:0.000}, {playing.MorphBeats} beats ({director.ActivePlayer.GetType().Name})");
            report.Check("family tour: step 2 plays the family's window (outside its own excerpt: home key)",
                clip.ExcerptStartBeat == w1.Start && clip.ExcerptEndBeat == w1.End && director.CurrentWindow?.Source == w1.Source &&
                (w1.Source == FamilyWindow.OwnExcerpt || (clip.EntryTonicPc == null && clip.ExitTonicPc == null)),
                $"{w1.Source}: beats {w1.Start}–{w1.End}");
            string hud = director.HudText;
            report.Check("family tour panel: family mode, step, 'Shares: <identity> · family of N songs', no bits",
                hud.Contains($"family · step 2/{expected.Count}") && hud.Contains("Shares:") && hud.Contains(GraphHud.Esc(fam.Label)) &&
                hud.Contains($"family of {expected.Count} songs") && !hud.Contains("bits"), FirstLines(hud, 4));
            SongNode child = loader.NodeById(clip.NodeId);
            report.Check("family tour: frames the family's previous song; lights this family's edge into the song",
                director.StepPartner == loader.NodeById(expected[0]) &&
                (director.StepEdge == null || (director.StepEdge.Record.FamilyId == fam.FamilyId && director.StepEdge.Target == child)) &&
                child.State == BubbleState.Focus && director.StepPartner.State == BubbleState.Related &&
                loader.Nodes.All(n => n == child || n == director.StepPartner || (n.State == BubbleState.Normal) == expected.Contains(n.NodeId)),
                $"edge {(director.StepEdge != null ? director.StepEdge.name : "none")}");

            director.Next();
            for (int i = 0; i < 12; i++) TickDirector(director, .1f);
            bool flying = director.Flying;
            for (int i = 0; i < 33; i++) TickDirector(director, .1f);
            SongNode third = loader.NodeById(director.CurrentClip!.NodeId);
            Vector3 v = cam.WorldToViewportPoint(third.transform.position);
            Vector3 pv = cam.WorldToViewportPoint(director.StepPartner!.transform.position);
            report.Check("family tour: the camera flies to the song and its family predecessor, both on screen", flying && !director.Flying &&
                v.z > 0 && v.x > 0 && v.x < 1 && v.y > 0 && v.y < 1 && pv.z > 0 && pv.x > 0 && pv.x < 1 && pv.y > 0 && pv.y < 1 &&
                (director.StepEdge == null || director.StepEdge.VisibleFraction > .999f), $"viewport {v.x:0.00},{v.y:0.00} and {pv.x:0.00},{pv.y:0.00}");
            string path = Path.Combine(outDir, "family_tour.png");
            Capture(cam, loader, path, width, height, out _);
            report.Check("family_tour.png written", File.Exists(path), $"step {director.StepIndex + 1}/{director.Steps.Count}: {FirstLines(director.HudText, 3)}");
            CheckLabels(report, loader, "family tour");

            int leaving = director.CurrentClip!.NodeId;
            director.Previous();
            report.Check("family tour: B/← morphs from the song just heard (step 3), not the step before",
                director.StepIndex == 1 && director.PreviousClip?.NodeId == leaving);

            int firstVisit = expected.FindIndex(n => TourPlanner.Window(data, fam.FamilyId, n).Source == FamilyWindow.FirstVisit);
            if (firstVisit >= 0)
            {
                int heard = director.CurrentClip!.NodeId;
                director.GoTo(firstVisit);
                SongClip c = director.CurrentClip!;
                SongRecord s = data.Song(c.NodeId);
                double bpb = s.BeatsPerBar > 0 ? s.BeatsPerBar : 4;
                MorphPlan plan = director.ActivePlayer!.CurrentPlan;
                report.Check("family tour: a first-visit window plays 8 bars from the identity's first bar, handing off in the home key",
                    director.PreviousClip?.NodeId == heard && Math.Abs(c.ExcerptEndBeat - c.ExcerptStartBeat - TourPlanner.FirstVisitBars * bpb) < 1e-9 &&
                    c.EntryTonic == s.TonicPc && Math.Abs(plan.StartSemitones - Morph.Wrap(director.PreviousClip!.ExitTonic - s.TonicPc)) < 1e-9,
                    $"step {firstVisit + 1}: node {c.NodeId}, beats {c.ExcerptStartBeat}–{c.ExcerptEndBeat}");
            }
            director.Exit();
            report.Check("family tour: Esc brings back the selected edge's card and free camera",
                loader.Highlighter.FocusEdge == edge && loader.Hud.InfoVisible && loader.Hud.InfoText.Contains(GraphHud.Esc(fam.Label)) &&
                cam.GetComponent<CameraControl>().InputEnabled && !loader.Highlighter.Suspended);
            loader.Highlighter.Select(null);

            // The player protocol on a family tour, with the probe standing in for the synth.
            ISongPlayer? original = director.Player;
            string originalDescription = director.PlayerDescription;
            ProbePlayer probe = new();
            director.UsePlayer(probe, "validation probe");
            try
            {
                director.StartFamilyTour(fam.FamilyId);
                bool native = probe.Calls.SequenceEqual(new[] { $"play {expected[0]} after none" });
                probe.Finish();
                director.Tick(.05f);
                bool second = director.StepIndex == 1 && probe.Previous?.NodeId == expected[0] && probe.Clip?.NodeId == expected[1] &&
                              probe.Clip.ExcerptStartBeat == w1.Start && probe.Stops == 0;
                probe.Finish();
                director.Tick(.05f);
                bool third2 = director.StepIndex == 2 && probe.Previous?.NodeId == expected[1] && probe.Clip?.NodeId == expected[2] && probe.Stops == 0;
                report.Check("family tour (probe): natural advances play each song after the one before, without Stop",
                    native && second && third2, string.Join("; ", probe.Calls));
            }
            finally
            {
                director.Exit();
                if (original != null) director.UsePlayer(original, originalDescription);
            }
        }

        /// <summary>The same graph with edge_semantics and the family tables removed reads as an older strict-evidence DB.</summary>
        static void ValidateStrippedLineage(Report report, SongGraphLoader loader, string dbPath)
        {
            string copy = Path.Combine(Application.temporaryCachePath, "lineage_stripped_for_validation.db");
            File.Copy(dbPath, copy, true);
            try
            {
                using (SqliteConnection conn = new("URI=file:" + copy))
                {
                    conn.Open();
                    using SqliteCommand cmd = conn.CreateCommand();
                    cmd.CommandText = "DELETE FROM graph_meta WHERE key IN ('edge_semantics', 'family_count'); DROP TABLE song_family; DROP TABLE identity_family;";
                    cmd.ExecuteNonQuery();
                    conn.Close();
                }
                loader.Build(copy);
                SongGraphData data = loader.Data!;
                report.Check("older DB (no edge_semantics, no family tables): reads as strict evidence without problems",
                    !data.IsIdentityLineage && !data.HasFamilies && data.Problems.Count == 0 && data.Edges.All(e => e.FamilyId == null && !e.IsStrongMatch),
                    string.Join(" | ", data.Problems.Take(3)));
                SongNode focus = loader.Nodes.First(n => n.TreeEdge != null);
                loader.Highlighter.ApplyFocus(focus, force: true);
                CheckHudSemantics(report, loader, focus, "older DB hover");
                WalkthroughDirector director = loader.Director;
                director.Mode = TourMode.Family;
                director.Tick(0f);
                report.Check("older DB: family mode has nothing to play and says why", TourPlanner.Plan(data, TourMode.Family, focus.NodeId).Count == 0 &&
                    !director.StartTour(TourMode.Family, focus) && !director.IsTouring && director.HudText.Contains("needs an identity-lineage graph"));
                director.Mode = TourMode.Lineage;
                loader.Highlighter.ApplyFocus(null, force: true);
            }
            finally
            {
                loader.Clear();
                SqliteConnection.ClearAllPools();
                GC.Collect();
                GC.WaitForPendingFinalizers();
                try { File.Delete(copy); }
                catch (IOException) { /* a pooled handle; the temp folder is Unity's own */ }
            }
        }

        static string? MetaText(string dbPath, string key)
        {
            using SqliteConnection conn = new("URI=file:" + Path.GetFullPath(dbPath));
            conn.Open();
            using SqliteCommand cmd = conn.CreateCommand();
            cmd.CommandText = "SELECT value FROM graph_meta WHERE key = @k";
            SqliteParameter p = cmd.CreateParameter();
            p.ParameterName = "@k";
            p.Value = key;
            cmd.Parameters.Add(p);
            object? v = cmd.ExecuteScalar();
            conn.Close();
            return v == null || v is DBNull ? null : Convert.ToString(v, CultureInfo.InvariantCulture)?.Trim().ToLowerInvariant();
        }
    }
}
