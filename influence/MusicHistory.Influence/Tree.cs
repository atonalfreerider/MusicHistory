namespace MusicHistory.Influence;

/// <summary>A span of a source song credited by one later song.</summary>
internal readonly record struct CreditedSpan(int Child, double Start, double End, double Bits);

/// <summary>Result of credit, "most referenced" and parent selection (DESIGN.md §8.10).</summary>
internal sealed class TreeResult
{
    public required int[] Parent;          // -1 = root
    public required int[] Root, Depth, RefCount, Descendants;
    public required double?[] RefNorm;
    public required double[] Katz;
    public required List<(PairResult Pair, string Kind, bool Credited)> Edges;   // by (B, kind tree first, S desc)
    public required List<CreditedSpan>[] CreditedSpans;                          // per source song (A side)
    public int Roots => Parent.Count(x => x < 0);
}

internal static class Tree
{
    /// <summary>
    /// Credit every significant passage of each B to one earlier song (highest E, the earliest
    /// within 10 %); ref_count = later songs crediting a song; ref_norm; Katz (alpha 0.2) over the
    /// credit graph; parent = the most referenced of B's strong influencers
    /// (S >= 0.5 max S), ties by S then earlier; other significant pairs become secondary edges
    /// (at most 8 per target, by S).
    /// </summary>
    public static TreeResult Build(Song[] songs, IReadOnlyList<PairResult> influence, Params p)
    {
        int n = songs.Length;
        var byB = new List<PairResult>[n];
        foreach (var r in influence) (byB[r.B] ??= []).Add(r);
        foreach (var l in byB) l?.Sort((x, y) => x.A.CompareTo(y.A));

        var creditors = new SortedSet<int>[n];          // A -> distinct B crediting it
        var credited = new HashSet<(int A, int B)>();
        var spans = new List<CreditedSpan>[n];
        for (int i = 0; i < n; i++) spans[i] = [];

        for (int b = 0; b < n; b++)
        {
            var pairs = byB[b];
            if (pairs == null) continue;
            foreach (var (a, passageSpans) in Credit(pairs, p))
            {
                (creditors[a] ??= []).Add(b);
                credited.Add((a, b));
                spans[a].AddRange(passageSpans);
            }
        }

        var refCount = new int[n];
        for (int a = 0; a < n; a++) refCount[a] = creditors[a]?.Count ?? 0;
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
        {
            double k = 0;
            if (creditors[a] != null)
                foreach (int b in creditors[a]) k += 1 + p.KatzAlpha * katz[b];
            katz[a] = k;
        }

        var parent = new int[n];
        Array.Fill(parent, -1);
        var edges = new List<(PairResult, string, bool)>();
        for (int b = 0; b < n; b++)
        {
            var pairs = byB[b];
            if (pairs == null || pairs.Count == 0) continue;
            double maxS = pairs.Max(x => x.S);
            PairResult? best = null;
            foreach (var r in pairs)
            {
                if (r.S < p.ParentFraction * maxS) continue;
                if (best == null || refCount[r.A] > refCount[best.A]
                    || refCount[r.A] == refCount[best.A] && (r.S > best.S || r.S == best.S && r.A < best.A))
                    best = r;
            }
            best ??= pairs.OrderByDescending(x => x.S).ThenBy(x => x.A).First();
            parent[b] = best.A;
            edges.Add((best, "tree", credited.Contains((best.A, b))));
            foreach (var r in pairs.Where(x => !ReferenceEquals(x, best)).OrderByDescending(x => x.S).ThenBy(x => x.A).Take(p.MaxSecondary))
                edges.Add((r, "secondary", credited.Contains((r.A, b))));
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

        return new TreeResult
        {
            Parent = parent, Root = root, Depth = depth, RefCount = refCount, Descendants = desc,
            RefNorm = refNorm, Katz = katz, Edges = edges, CreditedSpans = spans,
        };
    }

    /// <summary>
    /// Passages of B = unions of overlapping evidence segments (melody, bass, chord; loop
    /// segments only for a pair that has nothing else). Each passage goes to the earlier song
    /// whose segments cover at least half of it with the highest E; within 10 % of the best,
    /// the earliest (the originator).
    /// </summary>
    public static IEnumerable<(int A, List<CreditedSpan> Spans)> Credit(List<PairResult> pairs, Params p)
    {
        var segs = new List<(PairResult Pair, Segment Seg)>();
        foreach (var r in pairs)
        {
            var own = r.Segments.Where(s => s.Channel != Channel.Loop && s.BEnd > s.BStart).ToList();
            if (own.Count == 0) own = r.Segments.Where(s => s.BEnd > s.BStart).ToList();
            foreach (var s in MergeNear(own, p.CreditMergeBeats)) segs.Add((r, s));
        }
        if (segs.Count == 0) yield break;
        segs.Sort((x, y) => x.Seg.BStart != y.Seg.BStart ? x.Seg.BStart.CompareTo(y.Seg.BStart) : x.Pair.A.CompareTo(y.Pair.A));
        var passages = new List<(double S, double E)>();
        foreach (var (_, s) in segs)
        {
            if (passages.Count > 0 && s.BStart < passages[^1].E)
                passages[^1] = (passages[^1].S, Math.Max(passages[^1].E, s.BEnd));
            else passages.Add((s.BStart, s.BEnd));
        }
        var result = new SortedDictionary<int, List<CreditedSpan>>();
        int child = pairs[0].B;
        foreach (var (ps, pe) in passages)
        {
            double len = pe - ps;
            var stats = new List<(int A, double Cover, double E)>();
            foreach (var r in pairs)
            {
                var mine = segs.Where(x => ReferenceEquals(x.Pair, r) && x.Seg.BStart < pe && x.Seg.BEnd > ps).Select(x => x.Seg).ToList();
                if (mine.Count == 0) continue;
                stats.Add((r.A, Union(mine.Select(m => (Math.Max(ps, m.BStart), Math.Min(pe, m.BEnd)))), mine.Sum(m => m.Bits)));
            }
            if (stats.Count == 0) continue;
            var eligible = stats.Where(x => x.Cover >= p.CreditCover * len).ToList();
            if (eligible.Count == 0) eligible = stats;
            double bestE = eligible.Max(x => x.E);
            if (bestE < p.CreditMinBits) continue;   // not a significant passage: too little evidence
            int a = eligible.Where(x => x.E >= (1 - p.CreditTie) * bestE).Min(x => x.A);
            if (!result.TryGetValue(a, out var list)) result[a] = list = [];
            var pr = pairs.First(x => x.A == a);
            foreach (var (_, s) in segs.Where(x => ReferenceEquals(x.Pair, pr) && x.Seg.BStart < pe && x.Seg.BEnd > ps))
                list.Add(new CreditedSpan(child, s.AStart, s.AEnd, s.Bits));
        }
        foreach (var kv in result) yield return (kv.Key, kv.Value);
    }

    /// <summary>One pair's segments closer than <paramref name="gap"/> beats in B form one passage (section-sized credit).</summary>
    private static List<Segment> MergeNear(List<Segment> own, double gap)
    {
        if (gap <= 0 || own.Count < 2) return own;
        var sorted = own.OrderBy(x => x.BStart).ThenBy(x => x.BEnd).ToList();
        var outp = new List<Segment> { Clone(sorted[0]) };
        foreach (var x in sorted.Skip(1))
        {
            var cur = outp[^1];
            if (x.BStart - cur.BEnd <= gap)
            {
                cur.BEnd = Math.Max(cur.BEnd, x.BEnd);
                cur.AStart = Math.Min(cur.AStart, x.AStart);
                cur.AEnd = Math.Max(cur.AEnd, x.AEnd);
                cur.Bits += x.Bits;
                cur.N += x.N;
            }
            else outp.Add(Clone(x));
        }
        return outp;
    }

    private static Segment Clone(Segment x) => new()
    {
        Channel = x.Channel, AStart = x.AStart, AEnd = x.AEnd, BStart = x.BStart, BEnd = x.BEnd, Bits = x.Bits, N = x.N,
        Loop = x.Loop, SamePhase = x.SamePhase,
    };

    private static double Union(IEnumerable<(double S, double E)> spans)
    {
        double total = 0, cs = double.NaN, ce = double.NaN;
        foreach (var (s, e) in spans.OrderBy(x => x.S))
        {
            if (e <= s) continue;
            if (double.IsNaN(cs) || s > ce)
            {
                if (!double.IsNaN(cs)) total += ce - cs;
                cs = s;
                ce = e;
            }
            else ce = Math.Max(ce, e);
        }
        if (!double.IsNaN(cs)) total += ce - cs;
        return total;
    }
}

/// <summary>
/// Walkthrough excerpts (DESIGN.md §8.11): the tree edge's strongest segment of the child,
/// snapped outward to bar lines (first_downbeat + k * beats_per_bar), 8..24 bars. Roots use the
/// passage their children credit most, else the first visit of their most-covering loop family,
/// else the first 16 bars.
/// </summary>
internal static class Excerpts
{
    public static (double Start, double End)[] Compute(Song[] songs, TreeResult tree, Params p)
    {
        var parentPair = new PairResult?[songs.Length];
        foreach (var (pair, kind, _) in tree.Edges)
            if (kind == "tree") parentPair[pair.B] = pair;
        var outp = new (double, double)[songs.Length];
        for (int i = 0; i < songs.Length; i++)
        {
            var s = songs[i];
            if (parentPair[i] is { } pr && Strongest(pr) is { } seg)
                outp[i] = Snap(s, seg.BStart, seg.BEnd, p);
            else if (BestCreditedPassage(tree.CreditedSpans[i]) is { } cp)
                outp[i] = Snap(s, cp.Start, cp.End, p);
            else if (s.Loops.Length > 0)
            {
                var lp = s.Loops.OrderByDescending(l => l.Coverage).ThenByDescending(l => l.Visits).ThenBy(l => l.Family).First();
                double st = lp.VisitStarts.Length > 0 ? lp.VisitStarts.Min() : s.FirstDownbeat;
                outp[i] = Snap(s, st, st + lp.VisitBeats, p);
            }
            else outp[i] = Snap(s, s.FirstDownbeat, s.FirstDownbeat + p.ExcerptDefaultBars * Bpb(s), p);
        }
        return outp;
    }

    public static Segment? Strongest(PairResult pr)
    {
        var own = pr.Segments.Where(x => x.Channel != Channel.Loop && x.BEnd > x.BStart).ToList();
        if (own.Count == 0) own = pr.Segments.Where(x => x.BEnd > x.BStart).ToList();
        return own.OrderByDescending(x => x.Bits).ThenBy(x => x.BStart).FirstOrDefault();
    }

    /// <summary>Merge the credited source spans; the passage most children credit wins (then bits, then earliest).</summary>
    private static (double Start, double End)? BestCreditedPassage(List<CreditedSpan> spans)
    {
        if (spans.Count == 0) return null;
        var sorted = spans.Where(x => x.End > x.Start).OrderBy(x => x.Start).ToList();
        if (sorted.Count == 0) return null;
        var groups = new List<(double S, double E, HashSet<int> Children, double Bits)>();
        foreach (var sp in sorted)
        {
            if (groups.Count > 0 && sp.Start < groups[^1].E)
            {
                var g = groups[^1];
                g.Children.Add(sp.Child);
                groups[^1] = (g.S, Math.Max(g.E, sp.End), g.Children, g.Bits + sp.Bits);
            }
            else groups.Add((sp.Start, sp.End, [sp.Child], sp.Bits));
        }
        var best = groups.OrderByDescending(g => g.Children.Count).ThenByDescending(g => g.Bits).ThenBy(g => g.S).First();
        return (best.S, best.E);
    }

    private static double Bpb(Song s) => s.BeatsPerBar > 0 ? s.BeatsPerBar : 4.0;

    public static (double Start, double End) Snap(Song s, double start, double end, Params p)
    {
        double bpb = Bpb(s), fd = s.FirstDownbeat;
        double last = Math.Max(s.EndBeat, Math.Max(end, fd + bpb));
        double songEnd = fd + Math.Ceiling((last - fd) / bpb - 1e-9) * bpb;
        double a = fd + Math.Floor((start - fd) / bpb + 1e-9) * bpb;
        double e = fd + Math.Ceiling((end - fd) / bpb - 1e-9) * bpb;
        a = Math.Max(a, fd);
        e = Math.Min(e, songEnd);
        if (e <= a) e = Math.Min(songEnd, a + bpb);
        double minLen = p.ExcerptMinBars * bpb, maxLen = p.ExcerptMaxBars * bpb;
        if (e - a < minLen) e = Math.Min(songEnd, a + minLen);
        if (e - a < minLen) a = Math.Max(fd, e - minLen);
        if (e - a > maxLen) e = a + maxLen;
        return (Math.Round(a, 6), Math.Round(e, 6));
    }
}
