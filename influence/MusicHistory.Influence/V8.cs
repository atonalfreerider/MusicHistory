using System.Runtime.CompilerServices;

namespace MusicHistory.Influence;

/// <summary>Open-addressing map from 64-bit keys to non-negative ints (-1 = absent).</summary>
internal sealed class LongIntMap
{
    private readonly long[] _keys;
    private readonly int[] _vals;
    private readonly int _mask;

    public LongIntMap(int capacity)
    {
        int cap = 16;
        while (cap < capacity * 2) cap <<= 1;
        _keys = new long[cap];
        _vals = new int[cap];
        Array.Fill(_vals, -1);
        _mask = cap - 1;
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    private int Slot(long k) => (int)(((ulong)k * 0x9E3779B97F4A7C15UL) >> 33) & _mask;

    public void Set(long k, int v)
    {
        int i = Slot(k);
        while (_vals[i] >= 0)
        {
            if (_keys[i] == k)
            {
                _vals[i] = v;
                return;
            }
            i = (i + 1) & _mask;
        }
        _keys[i] = k;
        _vals[i] = v;
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public int Get(long k)
    {
        int i = Slot(k);
        while (true)
        {
            int v = _vals[i];
            if (v < 0 || _keys[i] == k) return v;
            i = (i + 1) & _mask;
        }
    }
}

/// <summary>
/// Document frequencies of n-gram keys over the corpus songs, with postings (the songs containing each key,
/// ascending). A song's document is every n-gram of its lead line, bass line and chords (all production
/// families, no filters) plus its loop identities, as the benchmark's <c>build_df.py</c> counts them.
/// </summary>
internal sealed class DfIndex
{
    public readonly int N;
    private readonly LongIntMap _map;
    private readonly int[] _df;
    private readonly int[] _start;
    private readonly int[] _posts;

    public int Distinct => _df.Length;
    public long PostingsCount => _posts.Length;

    private DfIndex(int n, LongIntMap map, int[] df, int[] start, int[] posts)
    {
        N = n;
        _map = map;
        _df = df;
        _start = start;
        _posts = posts;
    }

    /// <summary>Index over per-song documents (each sorted and distinct).</summary>
    public static DfIndex Build(IReadOnlyList<long[]> docs)
    {
        long total = 0;
        foreach (var d in docs) total += d.Length;
        var keys = new long[total];
        var songs = new int[total];
        long k = 0;
        for (int s = 0; s < docs.Count; s++)
            foreach (long h in docs[s])
            {
                keys[k] = h;
                songs[k++] = s;
            }
        Array.Sort(keys, songs);
        var dfs = new List<int>();
        var starts = new List<int>();
        var map = new LongIntMap(Math.Max(16, (int)Math.Min(int.MaxValue / 4, total)));
        int i = 0;
        while (i < keys.Length)
        {
            int j = i + 1;
            while (j < keys.Length && keys[j] == keys[i]) j++;
            Array.Sort(songs, i, j - i);
            map.Set(keys[i], dfs.Count);
            dfs.Add(j - i);
            starts.Add(i);
            i = j;
        }
        starts.Add(keys.Length);
        return new DfIndex(docs.Count, map, [.. dfs], [.. starts], songs);
    }

    /// <summary>df only (no postings), e.g. the benchmark's <c>df_corpus.npz</c>.</summary>
    public static DfIndex FromCounts(long[] keys, long[] counts, int n)
    {
        var map = new LongIntMap(keys.Length);
        var df = new int[keys.Length];
        for (int i = 0; i < keys.Length; i++)
        {
            map.Set(keys[i], i);
            df[i] = (int)counts[i];
        }
        return new DfIndex(n, map, df, [], []);
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public int Find(long key) => _map.Get(key);

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public int DfAt(int idx) => _df[idx];

    public int Df(long key)
    {
        int i = _map.Get(key);
        return i < 0 ? 0 : _df[i];
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public ReadOnlySpan<int> Postings(int idx) => _posts.AsSpan(_start[idx], _start[idx + 1] - _start[idx]);
}

/// <summary>
/// The per-n-gram terms of the V8 analytic corpus null, tabulated by the pair's document frequency
/// <c>d</c> = df with both songs of the pair removed, plus 2 (benchmark <c>DF.df_pair</c>):
/// <c>w(d) = log2((N + 1)/(d + 0.5))</c> (0 when d exceeds the rare-only cap), <c>p(d) = clip((d - 2)/(N - 2), 0, 1)</c>,
/// and the mean and variance terms <c>w p</c> and <c>w^2 p (1 - p)</c>.
/// </summary>
internal sealed class V8Weights
{
    public readonly int N, Cap;
    public readonly double Floor2;
    public readonly double[] W, WP, WQ, P;

    public V8Weights(int n, int cap, double floor)
    {
        N = n;
        Cap = cap;
        Floor2 = floor * floor;
        int m = n + 3;
        W = new double[m];
        WP = new double[m];
        WQ = new double[m];
        P = new double[m];
        for (int d = 0; d < m; d++)
        {
            double w = Math.Log2((n + 1.0) / (d + 0.5));
            double p = n > 2 ? Math.Clamp((d - 2) / (double)(n - 2), 0, 1) : 0;
            if (cap > 0 && d > cap) w = 0;
            W[d] = w;
            P[d] = p;
            WP[d] = w * p;
            WQ[d] = w * w * p * (1 - p);
        }
    }

    /// <summary>The pair document frequency: df minus the pair's own songs that contain the key, floored at 0, plus 2.</summary>
    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public int Dfp(int df, int inPair) => Math.Min(N + 2, Math.Max(df - inPair, 0) + 2);

    public double Z(double s, double mu, double var) => (s - mu) / Math.Sqrt(var + Floor2);

    /// <summary>Mean term w p with the weight from pair df <paramref name="dw"/> and the probability from <paramref name="dp"/> (equal: <see cref="WP"/>).</summary>
    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public double Mean(int dw, int dp) => dw == dp ? WP[dw] : W[dw] * P[dp];

    /// <summary>Variance term w^2 p (1 - p) with the weight from <paramref name="dw"/> and the probability from <paramref name="dp"/>.</summary>
    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public double Var(int dw, int dp) => dw == dp ? WQ[dw] : W[dw] * W[dw] * P[dp] * (1 - P[dp]);
}

/// <summary>One V8 statistic: shared IDF bits S, the null mean and variance, z, and how many distinct keys the passage has.</summary>
internal readonly record struct V8Stat(double S, double Mu, double Var, double Z, int Keys)
{
    public double Sd(double floor2) => Math.Sqrt(Var + floor2);
}

internal static class V8Math
{
    /// <summary>
    /// The V8 formula for one passage against one song, exactly as <c>analytic.v8</c>: for the passage's distinct
    /// keys, <c>S = sum w</c> over the keys the target contains, <c>mu = sum w p</c>, <c>var = sum w^2 p (1 - p)</c>,
    /// <c>z = (S - mu)/sqrt(var + floor^2)</c>. <paramref name="docA"/>/<paramref name="docB"/> are the two songs'
    /// corpus documents for the leave-two-out df (the same object counts once, as Python's <c>{wa, wb}</c>).
    /// <paramref name="sizeFactor"/> scales mu and var (the NullSizeAdjust experiment; 1 = analytic.py).
    /// </summary>
    public static V8Stat Direct(IReadOnlyList<long> keys, HashSet64 target, DfIndex df, HashSet64? docA, HashSet64? docB, V8Weights wt,
        double sizeFactor = 1, IReadOnlyList<long>? canon = null, IReadOnlyList<long>? proj = null)
    {
        double s = 0, mu = 0, var = 0;
        int InPair(ulong u) => (docA != null && docA.Contains(u) ? 1 : 0) + (docB != null && !ReferenceEquals(docA, docB) && docB.Contains(u) ? 1 : 0);
        for (int i = 0; i < keys.Count; i++)
        {
            long g = keys[i];
            ulong u = unchecked((ulong)g);
            int d = wt.Dfp(df.Df(g), InPair(u));
            // KeyFreeDf: the weight comes from the transposition-invariant form's df, the probability from the exact df.
            long c = canon != null ? canon[i] : g;
            int dw = c == g ? d : wt.Dfp(df.Df(c), InPair(unchecked((ulong)c)));
            // KeyFreeDf 2: the pitch-only projection of a rhythm-coded n-gram, when it is commoner, sets the weight.
            if (proj != null && proj[i] != c && proj[i] != 0)
                dw = Math.Max(dw, wt.Dfp(df.Df(proj[i]), InPair(unchecked((ulong)proj[i]))));
            if (target.Contains(u)) s += wt.W[dw];
            mu += wt.Mean(dw, d);
            var += wt.Var(dw, d);
        }
        mu *= sizeFactor;
        var *= sizeFactor;
        return new V8Stat(s, mu, var, wt.Z(s, mu, var), keys.Count);
    }
}

/// <summary>A note line restricted to a window (or whole): onsets, pitches, durations and metric classes of the selected notes.</summary>
internal readonly record struct LineView(double[] On, int[] Pitch, double[] Dur, int[] Met)
{
    public int Count => Pitch.Length;

    /// <summary>benchmark <c>chans_of</c>: notes with onset in [t0, t1) (1e-6 tolerance); the whole line when t0 is NaN.</summary>
    public static LineView Of(NoteLine line, double t0, double t1)
    {
        if (double.IsNaN(t0)) return new LineView(line.Onsets, line.Pitches, line.Durs, line.Met);
        int n = line.Count;
        bool sorted = true;
        for (int i = 1; i < n && sorted; i++) sorted = line.Onsets[i] >= line.Onsets[i - 1];
        if (!sorted)
        {
            var idx = Enumerable.Range(0, n).Where(i => line.Onsets[i] >= t0 - 1e-6 && line.Onsets[i] < t1 - 1e-6).ToArray();
            return new LineView(idx.Select(i => line.Onsets[i]).ToArray(), idx.Select(i => line.Pitches[i]).ToArray(),
                idx.Select(i => line.Durs[i]).ToArray(), idx.Select(i => line.Met[i]).ToArray());
        }
        int i0 = 0;
        while (i0 < n && !(line.Onsets[i0] >= t0 - 1e-6)) i0++;
        int i1 = i0;
        while (i1 < n && line.Onsets[i1] < t1 - 1e-6) i1++;
        return new LineView(line.Onsets[i0..i1], line.Pitches[i0..i1], line.Durs[i0..i1], line.Met[i0..i1]);
    }
}

/// <summary>Chords restricted to a window: those overlapping [t0, t1), the boundary chords clipped (benchmark <c>chans_of</c>).</summary>
internal readonly record struct ChordView(int[] Tok, double[] Start, double[] Dur)
{
    public int Count => Tok.Length;

    public static ChordView Of(ChordLine c, double t0, double t1)
    {
        if (double.IsNaN(t0)) return new ChordView(c.Tokens, c.Starts, c.Durs);
        var tok = new List<int>();
        var st = new List<double>();
        var du = new List<double>();
        for (int i = 0; i < c.Count; i++)
        {
            double s = c.Starts[i], d = c.Durs[i];
            if (!(s + d > t0 + 1e-6 && s < t1 - 1e-6)) continue;
            double e2 = Math.Min(s + d, t1), s2 = Math.Max(s, t0);
            tok.Add(c.Tokens[i]);
            st.Add(s2);
            du.Add(e2 - s2);
        }
        return new ChordView([.. tok], [.. st], [.. du]);
    }
}

/// <summary>Per-song data of the V8 scorer: the corpus document and the whole-song target key sets per channel.</summary>
internal sealed class SongV8
{
    public long[] Doc = [];                 // sorted distinct keys (df document)
    public HashSet64 DocSet = new(16);
    public HashSet64? Mel, Bass, Riff, Chord, Loop;   // null = channel not usable in this song
    public HashSet64[] Lanes = [];          // mel-family keys of every usable lane:* line (the lead's own lane excluded)
    public int[] LaneRows = [];             // index in Song.Lanes of each entry of Lanes
    public int LaneOffset;                  // index of this song's first lane in the global lane numbering

    public const int MaskMel = 1, MaskBass = 2, MaskRiff = 4, MaskChord = 8, MaskLanes = 16, MaskLoop = 32;

    public int Mask => (Mel != null ? MaskMel : 0) | (Bass != null ? MaskBass : 0) | (Riff != null ? MaskRiff : 0)
                       | (Chord != null ? MaskChord : 0) | (Lanes.Length > 0 ? MaskLanes : 0) | (Loop != null ? MaskLoop : 0);

    public static SongV8 Build(Song s, Params p)
    {
        var r = new SongV8();
        var doc = new List<long>();
        var occ = new List<GramOcc>();
        var keys = new List<long>();
        var mf = LineFilter.Of(p);
        bool canon = p.KeyFreeDf != 0, proj = p.KeyFreeDf >= 2;
        // The df document: every n-gram key, plus (KeyFreeDf) the canonical form of every key-dependent one and (KeyFreeDf 2)
        // the pitch-only projection of every rhythm-coded one.
        void Doc(List<GramOcc> os)
        {
            foreach (var o in os)
            {
                doc.Add(o.Key);
                if (canon && o.Canon != o.Key) doc.Add(o.Canon);
                if (proj && o.Proj != 0 && o.Proj != o.Canon) doc.Add(o.Proj);
            }
        }
        double fdb = s.FirstDownbeat, bpb = s.BeatsPerBar > 0 ? s.BeatsPerBar : 4.0;
        if (s.Melody is { Count: >= 2 } mel)
        {
            occ.Clear();
            Grams.Line(true, mel.Onsets, mel.Pitches, fdb, bpb, 0, false, occ, mf);
            Doc(occ);
            if (mel.Count >= p.MinLineNotes) r.Mel = SetOf(occ, keys, Grp.Mel);
        }
        if (s.Bass is { Count: >= 2 } bass)
        {
            occ.Clear();
            Grams.Line(false, bass.Onsets, bass.Pitches, fdb, bpb, 0, false, occ);
            Doc(occ);
            if (bass.Count >= p.MinLineNotes)
            {
                r.Bass = SetOf(occ, keys, Grp.BassNpc2);
                r.Riff = SetOf(occ, keys, Grp.Riff);
                if (p.RiffInBass != 0)
                {
                    Grams.GroupKeys(occ, Grp.Riff, keys);
                    foreach (long k in keys) r.Bass.Add(unchecked((ulong)k));
                }
            }
        }
        if (s.Chords is { Count: >= 2 } ch)
        {
            occ.Clear();
            Grams.Chords(ch.Tokens, ch.Starts, ch.Durs, fdb, bpb, 0, false, occ);
            Doc(occ);
            if (ch.Count >= p.MinChordChanges) r.Chord = SetOf(occ, keys, Grp.Chord);
        }
        var loops = LoopKeys(s, 0);
        if (loops.Count > 0)
        {
            doc.AddRange(loops);
            r.Loop = new HashSet64(loops.Count);
            foreach (long k in loops) r.Loop.Add(unchecked((ulong)k));
        }
        // Lanes: every lane:* line with enough notes, except the lead line's own lane (its melody n-grams would
        // repeat the melody channel: a lane whose key set overlaps the lead's with Jaccard >= LaneDupJaccard).
        HashSet64? lead = null;
        if (s.Melody is { Count: >= 2 } lm && p.WLanes > 0)
        {
            occ.Clear();
            Grams.Line(true, lm.Onsets, lm.Pitches, fdb, bpb, 0, false, occ, mf);
            lead = SetOf(occ, keys, Grp.Mel);
        }
        var lanes = new List<HashSet64>();
        var laneIdx = new List<int>();
        // WLanes <= 0 turns the lanes channel off entirely (no lane is indexed, so it is never available or counting).
        for (int j = 0; j < (p.WLanes > 0 ? s.Lanes.Length : 0); j++)
        {
            var ln = s.Lanes[j];
            if (ln.Count < p.MinLineNotes) continue;
            occ.Clear();
            Grams.Line(true, ln.Onsets, ln.Pitches, fdb, bpb, 0, false, occ, mf);
            var set = SetOf(occ, keys, Grp.Mel);
            if (lead != null && Jaccard(set, lead) >= p.LaneDupJaccard) continue;
            lanes.Add(set);
            laneIdx.Add(j);
        }
        r.Lanes = [.. lanes];
        r.LaneRows = [.. laneIdx];
        Grams.SortUnique(doc);
        r.Doc = [.. doc];
        r.DocSet = new HashSet64(r.Doc.Length);
        foreach (long k in r.Doc) r.DocSet.Add(unchecked((ulong)k));
        return r;
    }

    /// <summary>Jaccard index of two key sets (0 when both are empty).</summary>
    public static double Jaccard(HashSet64 a, HashSet64 b)
    {
        var (small, large) = a.Count <= b.Count ? (a, b) : (b, a);
        int inter = 0;
        foreach (ulong k in small.Keys())
            if (large.Contains(k)) inter++;
        int union = a.Count + b.Count - inter;
        return union > 0 ? inter / (double)union : 0;
    }

    private static HashSet64 SetOf(List<GramOcc> occ, List<long> keys, Grp g)
    {
        Grams.GroupKeys(occ, g, keys);
        var set = new HashSet64(Math.Max(4, keys.Count));
        foreach (long k in keys) set.Add(unchecked((ulong)k));
        return set;
    }

    /// <summary>Loop identity keys (cycle), (cycle, phase), (cycle, phase, rhythm) of every loop row at a transposition.</summary>
    public static List<long> LoopKeys(Song s, int shift)
    {
        var l = new List<long>();
        for (int r = 0; r < s.Loops.Length; r++)
        {
            if (s.Loops[r].Own.Length < 2) continue;
            var id = Features.Identity(s.Loops[r].Own, s.Loops[r].OwnRhythm, shift, r);
            l.Add(unchecked((long)id.C));
            l.Add(unchecked((long)id.CP));
            l.Add(unchecked((long)id.CPR));
        }
        Grams.SortUnique(l);
        return l;
    }
}
