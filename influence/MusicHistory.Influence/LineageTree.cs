namespace MusicHistory.Influence;

/// <summary>What an earlier song A and a later song B share: the score and each family's contribution.</summary>
internal sealed class LinPair
{
    public int A, B;
    public double Score;
    public Family Top = null!;        // the family with the largest contribution (the credited identity of an edge)
    public double TopC = double.NegativeInfinity;
    public double Close;              // finer agreement of the top family, 0..1 (closest version)
    public readonly List<(Family F, double C)> Parts = [];
    public bool Strong => Parts.Any(p => p.F.Kind == FamilyKind.Strong);
}

/// <summary>An edge of the lineage graph: the pair, its kind, its credited family and where it sounds in both songs.</summary>
internal sealed class LinEdge
{
    public required LinPair Pair;
    public required string Kind;      // 'tree' | 'secondary'
    public bool Credited;             // B credits A
    public double AStart, AEnd, BStart, BEnd;
    public Family Family => Pair.Top;
    public string Evidence => Pair.Top.Label;
    public string Primary => Pair.Top.Channel;

    /// <summary>Graph channel names of every family the pair shares (melody, bass, chord, loop order).</summary>
    public string ChannelsCsv
    {
        get
        {
            var set = new HashSet<string>(StringComparer.Ordinal);
            foreach (var (f, _) in Pair.Parts)
                foreach (var c in f.Channels.Split(',', StringSplitOptions.RemoveEmptyEntries)) set.Add(c);
            string[] order = ["melody", "bass", "chord", "loop"];
            return string.Join(",", order.Where(set.Contains));
        }
    }
}

/// <summary>
/// How the tree is built (the report compares the plain rules with the final one): the design's agreement factor, the
/// strength factor, exact credit ties to the closest version, and the finer agreement (closeness) inside the score.
/// </summary>
internal readonly record struct TreeRule(bool Agreement, bool Strength, bool Closeness, bool Fine)
{
    public static readonly TreeRule Final = new(true, true, true, true);
}

/// <summary>Credit, ref counts, parents, secondary edges and excerpts of the identity lineages (DESIGN.md §8b).</summary>
internal sealed class LineageResult
{
    public required Song[] Songs;
    public required List<Family> Families;
    public required Dictionary<int, LinPair>[] ByB;      // B -> (A -> pair), earlier A only
    public required int[] Credit;                        // credited A per B, -1 none
    public required int[] Parent, Root, Depth, RefCount, Descendants;
    public required double?[] RefNorm;
    public required double[] Katz;
    public required List<LinEdge> Edges;                 // by B, tree first, then score descending
    public required (double Start, double End)[] Excerpt;
    public required string[] ExcerptSource;
    public int Roots => Parent.Count(x => x < 0);
    public int[] Children
    {
        get
        {
            var c = new int[Songs.Length];
            foreach (int p in Parent)
                if (p >= 0) c[p]++;
            return c;
        }
    }
}

internal static class LineageTree
{
    /// <summary>
    /// Agreement of two members of a family (DESIGN §8b) and their closeness (finer agreement, 0..1). Loops: 1 for the
    /// same phase and rhythm signature, 0.75 for the same phase, 0.5 otherwise (best pair of their loop rows);
    /// closeness = mean of the share of equal chord qualities, the share of equal duration classes, the strength ratio
    /// and the loop-length ratio (bars). Progressions: 1 for the same variant, else 0.75; closeness = strength ratio.
    /// Strong matches: 1, 1.
    /// </summary>
    public static (double Agree, double Close) Agreement(Family f, Member a, Member b, Song[] songs, LineageParams lp)
    {
        double sr = Ratio(a.Strength, b.Strength);
        switch (f.Kind)
        {
            case FamilyKind.Strong:
                return (1, 1);
            case FamilyKind.Progression:
            {
                // Variant "rhythm|qualities" (cadences) or a form name (blues): 1 the same, 0.75 the same rhythm or form, 0.5 otherwise;
                // closeness = strength ratio x the share of equal chord qualities.
                string va = a.Variant ?? "", vb = b.Variant ?? "";
                int ia = va.IndexOf('|'), ib = vb.IndexOf('|');
                string ra = ia >= 0 ? va[..ia] : va, rb = ib >= 0 ? vb[..ib] : vb;
                double ag = va == vb ? lp.AgreeSame : ra == rb ? lp.AgreePhase : ia >= 0 || ib >= 0 ? lp.AgreeOther : lp.AgreePhase;
                double q = 1;
                if (ia >= 0 && ib >= 0)
                {
                    var qa = va[(ia + 1)..].Split('.');
                    var qb = vb[(ib + 1)..].Split('.');
                    int nq = Math.Min(qa.Length, qb.Length);
                    q = nq > 0 ? Enumerable.Range(0, nq).Count(i => qa[i] == qb[i]) / (double)nq : 0;
                }
                return (ag, lp.ClosenessProduct != 0 ? Math.Max(0.1, sr) * Math.Max(0.1, q) : (sr + q) / 2);
            }
        }
        double bestA = -1, bestC = -1;
        double bpbA = songs[a.Song].BeatsPerBar > 0 ? songs[a.Song].BeatsPerBar : 4, bpbB = songs[b.Song].BeatsPerBar > 0 ? songs[b.Song].BeatsPerBar : 4;
        foreach (var va in a.Vars)
            foreach (var vb in b.Vars)
            {
                bool phase = va.Phase == vb.Phase;
                bool rhythm = va.Rhythm.AsSpan().SequenceEqual(vb.Rhythm);
                double ag = phase ? rhythm ? lp.AgreeSame : lp.AgreePhase : lp.AgreeOther;
                int n = Math.Min(va.Tokens.Length, vb.Tokens.Length);
                double q = n > 0 ? Enumerable.Range(0, n).Count(i => va.Tokens[i] == vb.Tokens[i]) / (double)n : 0;
                int nr = Math.Min(va.Rhythm.Length, vb.Rhythm.Length);
                double r = nr > 0 ? Enumerable.Range(0, nr).Count(i => va.Rhythm[i] == vb.Rhythm[i]) / (double)nr : 0;
                double len = Ratio(va.LoopBeats / bpbA, vb.LoopBeats / bpbB);
                // Each part floored at 0.1: a member stays a (distant) version, never a zero score.
                double c = lp.ClosenessProduct != 0 ? Math.Max(0.1, q) * Math.Max(0.1, r) * Math.Max(0.1, sr) * Math.Max(0.1, len) : (q + r + sr + len) / 4;
                if (ag > bestA || ag == bestA && c > bestC)
                {
                    bestA = ag;
                    bestC = c;
                }
            }
        return bestA < 0 ? (lp.AgreeOther, sr) : (bestA, bestC);
    }

    private static double Ratio(double x, double y) => x <= 0 || y <= 0 ? 0 : Math.Min(x, y) / Math.Max(x, y);

    /// <summary>
    /// score(A -> B) = sum over the families both songs belong to of specificity x agreement x min(strength)^0.5, plus
    /// StrongWeight x z for a strong match; only A strictly earlier than B (§8.1 order and same-year rule).
    /// </summary>
    public static Dictionary<int, LinPair>[] Scores(Song[] songs, IReadOnlyList<Family> families, LineageParams lp, TreeRule rule)
    {
        int n = songs.Length;
        var byB = new Dictionary<int, LinPair>[n];
        for (int i = 0; i < n; i++) byB[i] = [];
        foreach (var f in families)
        {
            var mem = f.Members;
            for (int j = 1; j < mem.Count; j++)
            {
                var mb = mem[j];
                for (int i = 0; i < j; i++)
                {
                    var ma = mem[i];
                    if (ma.Song >= mb.Song || !DateOrder.Earlier(songs[ma.Song].Date, songs[mb.Song].Date)) continue;
                    var (ag, close) = Agreement(f, ma, mb, songs, lp);
                    if (!rule.Agreement) ag = 1;
                    else if (rule.Fine && lp.FineAgreement != 0) ag *= lp.ClosenessPower == 1 ? close : Math.Pow(close, lp.ClosenessPower);
                    double st = rule.Strength ? Math.Sqrt(Math.Min(ma.Strength, mb.Strength)) : 1;
                    double c = f.Specificity * ag * st + (f.Kind == FamilyKind.Strong ? lp.StrongWeight * f.Z : 0);
                    if (!byB[mb.Song].TryGetValue(ma.Song, out var pr)) byB[mb.Song][ma.Song] = pr = new LinPair { A = ma.Song, B = mb.Song };
                    pr.Score += c;
                    pr.Parts.Add((f, c));
                    if (c > pr.TopC + 1e-12 || Math.Abs(c - pr.TopC) <= 1e-12 && Better(f, pr.Top))
                    {
                        pr.TopC = c;
                        pr.Top = f;
                        pr.Close = close;
                    }
                }
            }
        }
        return byB;
    }

    /// <summary>Between equal contributions: a strong match, then the smaller (more specific) family, then the lower id.</summary>
    private static bool Better(Family f, Family? cur)
    {
        if (cur == null) return true;
        bool fs = f.Kind == FamilyKind.Strong, cs = cur.Kind == FamilyKind.Strong;
        if (fs != cs) return fs;
        return f.Size != cur.Size ? f.Size < cur.Size : f.Id < cur.Id;
    }

    private static bool Tied(double s, double best) => s >= best - 1e-9 * Math.Max(1, Math.Abs(best));

    /// <summary>
    /// The user's rule (DESIGN §8b): each later song credits the earlier song with the highest score (an exact tie goes
    /// to the closest version, then the earliest; the plain rule: the earliest); ref_count = songs crediting a song;
    /// strong influencers = score >= ParentFraction x max; parent = the most referenced of them (ties: score, closeness,
    /// earlier); up to MaxSecondary other strong influencers become secondary edges. Songs with no earlier family member
    /// are roots. Excerpts: an edge's spans are the first visit of its credited family in both songs, snapped to bars
    /// (8..24); a song's excerpt is its tree edge's; a root's is the first visit of the family most of its children
    /// share, else of its most-covering loop, else its first 16 bars.
    /// </summary>
    public static LineageResult Build(Song[] songs, List<Family> families, Params p, LineageParams lp, TreeRule? ruleOpt = null)
    {
        var rule = ruleOpt ?? TreeRule.Final;
        int n = songs.Length;
        var byB = Scores(songs, families, lp, rule);
        var credit = new int[n];
        Array.Fill(credit, -1);
        var refCount = new int[n];
        var creditors = new List<int>[n];
        for (int b = 0; b < n; b++)
        {
            if (byB[b].Count == 0) continue;
            var pairs = byB[b].Values;
            double best = pairs.Max(x => x.Score);
            var tied = pairs.Where(x => Tied(x.Score, best));
            var pick = rule.Closeness && lp.CreditCloseness != 0
                ? tied.OrderByDescending(x => x.Close).ThenBy(x => x.A).First()
                : tied.OrderBy(x => x.A).First();
            credit[b] = pick.A;
            refCount[pick.A]++;
            (creditors[pick.A] ??= []).Add(b);
        }
        var refNorm = new double?[n];
        for (int a = 0; a < n; a++)
        {
            int after = 0;
            for (int b = a + 1; b < n; b++)
                if (DateOrder.Earlier(songs[a].Date, songs[b].Date)) after++;
            refNorm[a] = after > 0 ? (double)refCount[a] / after : null;
        }
        var katz = new double[n];
        for (int a = n - 1; a >= 0; a--)
            if (creditors[a] != null)
                foreach (int b in creditors[a]) katz[a] += 1 + p.KatzAlpha * katz[b];

        var parent = new int[n];
        Array.Fill(parent, -1);
        var edges = new List<LinEdge>();
        for (int b = 0; b < n; b++)
        {
            if (byB[b].Count == 0) continue;
            var pairs = byB[b].Values.ToList();
            double max = pairs.Max(x => x.Score);
            var strong = pairs.Where(x => x.Score >= p.ParentFraction * max - 1e-9 * Math.Max(1, max)).ToList();
            bool close = rule.Closeness && lp.CreditCloseness != 0;
            var best = strong.OrderByDescending(x => refCount[x.A]).ThenByDescending(x => x.Score)
                .ThenByDescending(x => close ? x.Close : 0).ThenBy(x => x.A).First();
            parent[b] = best.A;
            edges.Add(new LinEdge { Pair = best, Kind = "tree", Credited = credit[b] == best.A });
            foreach (var r in strong.Where(x => !ReferenceEquals(x, best)).OrderByDescending(x => x.Score).ThenByDescending(x => x.Close).ThenBy(x => x.A)
                         .Take(p.MaxSecondary))
                edges.Add(new LinEdge { Pair = r, Kind = "secondary", Credited = credit[b] == r.A });
        }
        var root = new int[n];
        var depth = new int[n];
        for (int b = 0; b < n; b++)
        {
            root[b] = parent[b] < 0 ? b : root[parent[b]];
            depth[b] = parent[b] < 0 ? 0 : depth[parent[b]] + 1;
        }
        var desc = new int[n];
        for (int b = n - 1; b >= 0; b--)
            if (parent[b] >= 0) desc[parent[b]] += desc[b] + 1;

        // Edge spans: the credited family's first visit in both songs.
        foreach (var e in edges)
        {
            var f = e.Pair.Top;
            var ma = f.Of(e.Pair.A)!;
            var mb = f.Of(e.Pair.B)!;
            (e.AStart, e.AEnd) = Excerpts.Snap(songs[e.Pair.A], ma.First, ma.FirstEnd, p);
            (e.BStart, e.BEnd) = Excerpts.Snap(songs[e.Pair.B], mb.First, mb.FirstEnd, p);
        }
        var (excerpt, source) = SongExcerpts(songs, families, parent, edges, p);
        return new LineageResult
        {
            Songs = songs, Families = families, ByB = byB, Credit = credit, Parent = parent, Root = root, Depth = depth, RefCount = refCount,
            Descendants = desc, RefNorm = refNorm, Katz = katz, Edges = edges, Excerpt = excerpt, ExcerptSource = source,
        };
    }

    /// <summary>Each song's walkthrough excerpt (see <see cref="Build"/>) and where it came from.</summary>
    private static ((double, double)[] Excerpt, string[] Source) SongExcerpts(Song[] songs, List<Family> families, int[] parent, List<LinEdge> edges, Params p)
    {
        int n = songs.Length;
        var outp = new (double, double)[n];
        var src = new string[n];
        var treeIn = new LinEdge?[n];
        var fromA = new List<LinEdge>[n];
        foreach (var e in edges)
        {
            if (e.Kind == "tree") treeIn[e.Pair.B] = e;
            (fromA[e.Pair.A] ??= []).Add(e);
        }
        for (int i = 0; i < n; i++)
        {
            var s = songs[i];
            if (treeIn[i] is { } te)
            {
                outp[i] = (te.BStart, te.BEnd);
                src[i] = "tree edge: " + te.Family.Label;
                continue;
            }
            if (fromA[i] is { Count: > 0 } outs)
            {
                // The family most of the root's children (tree edges first) share with it.
                var fam = outs.GroupBy(e => e.Family).OrderByDescending(g => g.Count(e => e.Kind == "tree")).ThenByDescending(g => g.Count())
                    .ThenByDescending(g => g.Key.Specificity).ThenBy(g => g.Key.Id).First().Key;
                var m = fam.Of(i)!;
                outp[i] = Excerpts.Snap(s, m.First, m.FirstEnd, p);
                src[i] = "children: " + fam.Label;
                continue;
            }
            if (s.Loops.Length > 0)
            {
                var lp = s.Loops.OrderByDescending(l => l.Coverage).ThenByDescending(l => l.Visits).ThenBy(l => l.Family).First();
                double st = lp.VisitStarts.Length > 0 ? lp.VisitStarts.Min() : s.FirstDownbeat;
                outp[i] = Excerpts.Snap(s, st, st + lp.VisitBeats, p);
                src[i] = "most-covering loop";
                continue;
            }
            outp[i] = Excerpts.Snap(s, s.FirstDownbeat, s.FirstDownbeat + p.ExcerptDefaultBars * (s.BeatsPerBar > 0 ? s.BeatsPerBar : 4), p);
            src[i] = "first bars";
        }
        return (outp, src);
    }
}
