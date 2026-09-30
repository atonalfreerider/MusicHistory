using System.Runtime.CompilerServices;

namespace MusicHistory.Influence;

/// <summary>Per-channel values of a pair (melody, bass, chord, loop, lanes, and the riff family at index 5).</summary>
[InlineArray(6)]
internal struct F6
{
    private float _e;
}

internal static class Ch
{
    public const int Mel = 0, Bass = 1, Chord = 2, Loop = 3, Lanes = 4, Riff = 5, N = 6;
    public static readonly string[] Names = ["melody", "bass", "chord", "loop", "lanes", "riff"];
}

/// <summary>
/// The V8 result of one ordered pair (A index &lt; B index): the best window of B by fused z and the
/// per-channel values there, plus every channel's maximum over B's windows. Kept for every pair of the run.
/// </summary>
internal struct PairRec
{
    public float Z;          // max over B's windows of the fused z (NaN: no channel available anywhere)
    public short Win;        // winning window index
    public sbyte Shift;      // transposition of B's window at the winning window (semitones)
    public byte Avail;       // channels available at the winning window (bit per Ch index)
    public sbyte LaneSrc;    // lanes at the winning window: -1 none, 0 = B lead vs A lane LaneIdx, 1 + j = B lane j vs A lead
    public byte LaneIdx;
    public F6 Zc, Sc, Mu, Sd;
    public F6 ZMax;

    public readonly bool Has(int c) => (Avail & (1 << c)) != 0;
}

/// <summary>One window of B: its bar span and channel views.</summary>
internal sealed class V8Window
{
    public double T0, T1;
    public LineView? Mel, Bass;
    public ChordView? Chord;
    public LineView?[] Lanes = [];
}

/// <summary>The channel values of one window against one A (a row of <c>score-pairs</c> and the bass-alone re-scan).</summary>
internal sealed class WindowEval
{
    public int Window;
    public double T0, T1;
    public double Fused = double.NaN;     // penalized, at the best shift
    public int Shift;
    public int Avail;
    public int LaneSrc = -1, LaneIdx;
    public readonly double[] Z = new double[Ch.N], S = new double[Ch.N], Mu = new double[Ch.N], Sd = new double[Ch.N];

    public WindowEval Clone()
    {
        var c = new WindowEval { Window = Window, T0 = T0, T1 = T1, Fused = Fused, Shift = Shift, Avail = Avail, LaneSrc = LaneSrc, LaneIdx = LaneIdx };
        Array.Copy(Z, c.Z, Ch.N);
        Array.Copy(S, c.S, Ch.N);
        Array.Copy(Mu, c.Mu, Ch.N);
        Array.Copy(Sd, c.Sd, Ch.N);
        return c;
    }
}

/// <summary>Sparse per-target accumulator of one query (a passage's key set) walked through the postings.</summary>
internal sealed class Acc
{
    public double Mu0, Var0;
    public int Keys;
    public readonly double[] S, DMu, DVar;
    private readonly int[] _stamp, _kstamp;
    private int _epoch = 1, _kepoch = 1;
    public readonly List<int> Touched = [];

    public Acc(int n)
    {
        S = new double[n];
        DMu = new double[n];
        DVar = new double[n];
        _stamp = new int[n];
        _kstamp = new int[n];
    }

    /// <summary>Starts a new key: <see cref="FirstForKey"/> is true once per target until the next call.</summary>
    public void NextKey() => _kepoch++;

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public bool FirstForKey(int a)
    {
        if (_kstamp[a] == _kepoch) return false;
        _kstamp[a] = _kepoch;
        return true;
    }

    public void Reset()
    {
        _epoch++;
        Touched.Clear();
        Mu0 = Var0 = 0;
        Keys = 0;
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public void Touch(int a)
    {
        if (_stamp[a] == _epoch) return;
        _stamp[a] = _epoch;
        S[a] = DMu[a] = DVar[a] = 0;
        Touched.Add(a);
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public bool Has(int a) => a >= 0 && _stamp[a] == _epoch;
}

/// <summary>Accumulated shared bits per lane (global lane numbering) for "B's lead window vs A's lanes".</summary>
internal sealed class LaneAcc
{
    public readonly double[] S;
    private readonly int[] _stamp;
    private int _epoch = 1;
    public readonly List<int> TouchedSongs = [];
    private readonly int[] _songStamp;

    public LaneAcc(int lanes, int songs)
    {
        S = new double[Math.Max(1, lanes)];
        _stamp = new int[Math.Max(1, lanes)];
        _songStamp = new int[songs];
    }

    public void Reset()
    {
        _epoch++;
        TouchedSongs.Clear();
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public void Add(int song, int lane, double w)
    {
        if (_stamp[lane] != _epoch)
        {
            _stamp[lane] = _epoch;
            S[lane] = 0;
        }
        S[lane] += w;
        if (_songStamp[song] != _epoch)
        {
            _songStamp[song] = _epoch;
            TouchedSongs.Add(song);
        }
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public double Get(int lane) => _stamp[lane] == _epoch ? S[lane] : 0;
}

/// <summary>Per-thread buffers of the engine.</summary>
internal sealed class V8Worker
{
    public readonly Acc[][] G;           // [group: mel, bass, riff, chord][part: 0 key-free, 1 + shift index key-dependent]
    public readonly List<Acc[]> LaneII = [];
    public readonly LaneAcc[] LaneI;     // [part]
    public readonly Acc[] Loop;          // [shift index]
    public readonly List<GramOcc> Occ = [], OccS = [];
    public readonly List<long> Keys = [], Canon = [], Proj = [], LaneKeysFree = [], LaneCanonFree = [], LaneProjFree = [], TmpCanon = [], TmpProj = [];
    public readonly int[] Mark;
    public int MarkEpoch = 1;
    public readonly WindowEval Tmp = new(), Best = new();
    public readonly WindowEval?[] Cache = new WindowEval?[64];
    public readonly WindowEval[] CacheStore;
    private readonly int _n, _parts;

    public V8Worker(int nSongs, int nLanes, int nShifts)
    {
        _n = nSongs;
        _parts = 1 + nShifts;
        G = new Acc[4][];
        for (int g = 0; g < 4; g++) G[g] = NewParts();
        LaneI = Enumerable.Range(0, _parts).Select(_ => new LaneAcc(nLanes, nSongs)).ToArray();
        Loop = Enumerable.Range(0, nShifts).Select(_ => new Acc(nSongs)).ToArray();
        Mark = new int[nSongs];
        CacheStore = Enumerable.Range(0, 64).Select(_ => new WindowEval()).ToArray();
    }

    public Acc[] NewParts() => Enumerable.Range(0, _parts).Select(_ => new Acc(_n)).ToArray();

    public Acc[] LaneParts(int j)
    {
        while (LaneII.Count <= j) LaneII.Add(NewParts());
        return LaneII[j];
    }
}

/// <summary>
/// The V8 evidence of DESIGN §8 (as rebuilt from the calibration benchmark): for every 16-bar window of the later
/// song B (hop 4 bars, bar lines from first_downbeat / beats_per_bar) and every channel, S = the IDF bits of the
/// window's distinct rare n-grams that the earlier song A contains, z-scored against the analytic corpus null;
/// channels fused with a weighted Stouffer sum; the pair's score is the maximum over B's windows. No candidate
/// generation: each window's rare n-grams are walked through the postings (df &lt;= cap, so a handful of songs
/// each) and every earlier song gets its exact S, mu and sigma (mu and sigma differ from the window's
/// values only through the leave-two-out df of the n-grams A contains).
/// </summary>
internal sealed class V8Engine
{
    public readonly Song[] Songs;
    public readonly SongV8[] Data;
    public readonly DfIndex Df;
    public readonly V8Weights Wt;
    public readonly Params P;
    public readonly int[] Shifts;
    public readonly double[] ChanW;         // fusion weights by Ch index (riff unused)
    public readonly int TotalLanes;
    public readonly LineFilter MelFilter;
    private readonly bool _canon, _proj;
    private readonly int _projScope;

    /// <summary>Whether the pitch-only projection sets the weight of the group's rhythm-coded n-grams (KeyFreeDf 2, ProjScope).</summary>
    private bool ProjIn(int group) => _proj && (group switch { GMel => 2, GBass or GRiff => 4, GChord => 1, _ => 0 } & _projScope) != 0;
    private readonly LongIntMap? _laneMap;
    private readonly int[] _laneStart = [];
    private readonly int[] _laneSong = [], _laneIdx = [];

    private V8Engine(Song[] songs, SongV8[] data, DfIndex df, Params p, LongIntMap? laneMap, int[] laneStart, int[] laneSong, int[] laneIdx, int totalLanes)
    {
        Songs = songs;
        Data = data;
        Df = df;
        P = p;
        Wt = new V8Weights(songs.Length, p.DfCap, p.SigmaFloor);
        Shifts = p.Hedge != 0 ? [0, 3, -3, 5, -5] : [0];
        ChanW = [p.WMelody, p.WBass, p.WChord, p.WLoop, p.WLanes, 0];
        _laneMap = laneMap;
        _laneStart = laneStart;
        _laneSong = laneSong;
        _laneIdx = laneIdx;
        TotalLanes = totalLanes;
        MelFilter = LineFilter.Of(p);
        _canon = p.KeyFreeDf != 0;
        _proj = p.KeyFreeDf >= 2;
        _projScope = p.ProjScope;
        double M(Func<SongV8, HashSet64?> f)
        {
            var v = data.Select(f).Where(x => x != null && x.Count > 0).Select(x => (double)x!.Count).ToList();
            return v.Count > 0 ? v.Average() : 1;
        }
        _mean = [M(d => d.Mel), M(d => d.Bass), M(d => d.Riff), M(d => d.Chord), M(d => d.Loop)];
    }

    private readonly double[] _mean;

    public static V8Engine Build(Song[] songs, Params p)
    {
        var data = new SongV8[songs.Length];
        Parallel.For(0, songs.Length, new ParallelOptions { MaxDegreeOfParallelism = p.Threads }, i => data[i] = SongV8.Build(songs[i], p));
        var df = DfIndex.Build(data.Select(d => d.Doc).ToList());
        // Lane postings: key -> (song, lane), for lane keys that can weigh anything (df <= cap).
        int total = 0;
        for (int i = 0; i < data.Length; i++)
        {
            data[i].LaneOffset = total;
            total += data[i].Lanes.Length;
        }
        LongIntMap? map = null;
        int[] start = [], lsong = [], lidx = [];
        if (total > 0)
        {
            var entries = new List<(long K, int S, int L)>();
            for (int i = 0; i < data.Length; i++)
                for (int j = 0; j < data[i].Lanes.Length; j++)
                    foreach (ulong u in data[i].Lanes[j].Keys())
                    {
                        long k = unchecked((long)u);
                        if (p.DfCap > 0 && df.Df(k) > p.DfCap) continue;
                        entries.Add((k, i, j));
                    }
            entries.Sort((x, y) => x.K != y.K ? x.K.CompareTo(y.K) : x.S != y.S ? x.S.CompareTo(y.S) : x.L.CompareTo(y.L));
            map = new LongIntMap(Math.Max(16, entries.Count));
            var st = new List<int>();
            lsong = new int[entries.Count];
            lidx = new int[entries.Count];
            int g = 0;
            for (int e = 0; e < entries.Count; e++)
            {
                if (e == 0 || entries[e].K != entries[e - 1].K)
                {
                    map.Set(entries[e].K, g++);
                    st.Add(e);
                }
                lsong[e] = entries[e].S;
                lidx[e] = entries[e].L;
            }
            st.Add(entries.Count);
            start = [.. st];
        }
        return new V8Engine(songs, data, df, p, map, start, lsong, lidx, total);
    }

    public V8Worker NewWorker() => new(Songs.Length, TotalLanes, Shifts.Length);

    private static double Bpb(Song s) => s.BeatsPerBar > 0 ? s.BeatsPerBar : 4.0;

    /// <summary>benchmark <c>pairlevel.windows_of</c>: 16-bar windows every 4 bars from first_downbeat while the start is more than 4 bars before the end.</summary>
    public List<V8Window> Windows(Song b)
    {
        var outp = new List<V8Window>();
        double bpb = Bpb(b), fdb = b.FirstDownbeat, end = b.EndBeat;
        for (int k = 0; fdb + k * bpb < end - 4 * bpb; k += P.WindowHop)
        {
            double t0 = fdb + k * bpb, t1 = t0 + P.WindowBars * bpb;
            var w = new V8Window { T0 = t0, T1 = t1 };
            if (b.Melody != null && LineView.Of(b.Melody, t0, t1) is { Count: var nm } mv && nm >= P.MinLineNotes) w.Mel = mv;
            if (b.Bass != null && LineView.Of(b.Bass, t0, t1) is { Count: var nb } bv && nb >= P.MinLineNotes) w.Bass = bv;
            if (b.Chords != null && ChordView.Of(b.Chords, t0, t1) is { Count: var nc } cv && nc >= P.MinChordChanges) w.Chord = cv;
            w.Lanes = UsableLanes(b).Select(ln => LineView.Of(ln, t0, t1) is { Count: var n } lv && n >= P.MinLineNotes ? (LineView?)lv : null).ToArray();
            if (w.Mel != null || w.Bass != null || w.Chord != null || w.Lanes.Any(x => x != null)) outp.Add(w);
        }
        return outp;
    }

    /// <summary>The song's lane lines in the order of <see cref="SongV8.Lanes"/> (usable ones, the lead's own lane excluded).</summary>
    public IEnumerable<NoteLine> UsableLanes(Song s) => Data[s.Index].LaneRows.Select(j => s.Lanes[j]);

    // ------------------------------------------------------------------------------------------------ queries
    private const int GMel = 0, GBass = 1, GRiff = 2, GChord = 3;

    private HashSet64? Target(int group, int a) => group switch
    {
        GMel => Data[a].Mel,
        GBass => Data[a].Bass,
        GRiff => Data[a].Riff,
        GChord => Data[a].Chord,
        _ => Data[a].Loop,
    };

    /// <summary>Mean/variance of the window's keys against a song containing none of them, then the corrections and shared bits for every earlier song containing one.</summary>
    /// <summary>
    /// With <c>KeyFreeDf</c>, the pair df of the key's canonical (transposition-invariant) form, which sets its weight; the
    /// exact df keeps setting the probability that a song holds the key. Without it, the exact pair df.
    /// </summary>
    private int CanonDfp(long g, long c, long pj, int exactDfp, HashSet64 bDoc, int extra)
    {
        if (!_canon) return exactDfp;
        int d = c == g ? exactDfp : Wt.Dfp(Df.Df(c), (bDoc.Contains(unchecked((ulong)c)) ? 1 : 0) + extra);
        // KeyFreeDf 2: a rhythm-coded n-gram is only as rare as its pitch-only (harmony-only) content.
        if (_proj && pj != c && pj != 0) d = Math.Max(d, Wt.Dfp(Df.Df(pj), (bDoc.Contains(unchecked((ulong)pj)) ? 1 : 0) + extra));
        return d;
    }

    private void Run(Acc acc, List<long> keys, List<long>? canon, int b, int onlyA, int group, List<long>? proj = null)
    {
        if (!ProjIn(group)) proj = null;
        acc.Reset();
        acc.Keys = keys.Count;
        var bDoc = Data[b].DocSet;
        var W = Wt.W;
        for (int ki = 0; ki < keys.Count; ki++)
        {
            long g = keys[ki];
            long c = canon != null && _canon ? canon[ki] : g;
            long pj = proj != null && _proj ? proj[ki] : c;
            ulong u = unchecked((ulong)g);
            int idx = Df.Find(g);
            int df = idx >= 0 ? Df.DfAt(idx) : 0;
            int inB = bDoc.Contains(u) ? 1 : 0;
            int d0 = Wt.Dfp(df, inB);
            if (c != g || pj != c)
            {
                RunCanonical(acc, g, c, pj, idx, d0, df, inB, bDoc, b, onlyA, group);
                continue;
            }
            acc.Mu0 += Wt.WP[d0];
            acc.Var0 += Wt.WQ[d0];
            if (idx < 0) continue;
            int d1 = Wt.Dfp(df, inB + 1);
            if (W[d0] == 0 && W[d1] == 0) continue;
            double dmu = Wt.WP[d1] - Wt.WP[d0], dvar = Wt.WQ[d1] - Wt.WQ[d0], ws = W[d1];
            foreach (int a in Df.Postings(idx))
            {
                if (a >= b) break;
                if (onlyA >= 0 && a != onlyA) continue;
                acc.Touch(a);
                acc.DMu[a] += dmu;
                acc.DVar[a] += dvar;
                var t = Target(group, a);
                if (t != null && t.Contains(u)) acc.S[a] += ws;
            }
        }
    }

    /// <summary>
    /// One key whose weight comes from its canonical form <paramref name="c"/> (KeyFreeDf) and, for rhythm-coded
    /// n-grams of the ProjScope channels, its pitch-only projection <paramref name="pj"/>: the weight's pair df is the
    /// larger of theirs, each with the true leave-two-out (a target holds c or pj when its document does); the
    /// probability that a target holds the key keeps the exact df. Targets holding the key get S and their
    /// corrections through the key's postings; targets holding only c or pj (the same material in another key or
    /// rhythm) get the weight correction of their mu and var through c's and pj's postings.
    /// </summary>
    private void RunCanonical(Acc acc, long g, long c, long pj, int idx, int d0, int df, int inB, HashSet64 bDoc, int b, int onlyA, int group)
    {
        var W = Wt.W;
        ulong u = unchecked((ulong)g), cu = unchecked((ulong)c), pu = unchecked((ulong)pj);
        bool hasProj = pj != c;
        int ci = Df.Find(c), pi = hasProj ? Df.Find(pj) : -1;
        int dfc = ci >= 0 ? Df.DfAt(ci) : 0, dfp = pi >= 0 ? Df.DfAt(pi) : 0;
        int inBc = bDoc.Contains(cu) ? 1 : 0, inBp = hasProj && bDoc.Contains(pu) ? 1 : 0;
        int Dw(int ac, int ap) => hasProj ? Math.Max(Wt.Dfp(dfc, inBc + ac), Wt.Dfp(dfp, inBp + ap)) : Wt.Dfp(dfc, inBc + ac);
        int w0 = Dw(0, 0);
        acc.Mu0 += Wt.Mean(w0, d0);
        acc.Var0 += Wt.Var(w0, d0);
        // Nothing can change when no target membership moves the weight off zero.
        if (W[w0] == 0 && W[Dw(1, 1)] == 0 && W[Dw(1, 0)] == 0 && W[Dw(0, 1)] == 0) return;
        double m0 = Wt.Mean(w0, d0), v0 = Wt.Var(w0, d0);
        acc.NextKey();
        if (idx >= 0)
        {
            int d1 = Wt.Dfp(df, inB + 1);
            int w1 = Dw(1, 1);                                 // holding the key means holding its canonical form and projection
            double dmu = Wt.Mean(w1, d1) - m0, dvar = Wt.Var(w1, d1) - v0, ws = W[w1];
            foreach (int a in Df.Postings(idx))
            {
                if (a >= b) break;
                if (onlyA >= 0 && a != onlyA) continue;
                acc.FirstForKey(a);
                acc.Touch(a);
                acc.DMu[a] += dmu;
                acc.DVar[a] += dvar;
                var t = Target(group, a);
                if (t != null && t.Contains(u)) acc.S[a] += ws;
            }
        }
        void Others(int post, bool isCanon)
        {
            if (post < 0) return;
            foreach (int a in Df.Postings(post))
            {
                if (a >= b) break;
                if (onlyA >= 0 && a != onlyA) continue;
                if (!acc.FirstForKey(a)) continue;                // holds the key itself, or already corrected
                var doc = Data[a].DocSet;
                int ac = isCanon ? 1 : doc.Contains(cu) ? 1 : 0, ap = !hasProj ? 0 : !isCanon ? 1 : doc.Contains(pu) ? 1 : 0;
                int wa = Dw(ac, ap);
                if (wa == w0) continue;
                acc.Touch(a);
                acc.DMu[a] += Wt.Mean(wa, d0) - m0;
                acc.DVar[a] += Wt.Var(wa, d0) - v0;
            }
        }
        if (W[Dw(1, 0)] != W[w0] || hasProj && W[Dw(1, 1)] != W[w0]) Others(ci, true);
        if (hasProj && (W[Dw(0, 1)] != W[w0] || W[Dw(1, 1)] != W[w0])) Others(pi, false);
    }

    /// <summary>
    /// B's lead window against every lane of every earlier song (shared bits per lane; mu and var are the melody query's).
    /// With KeyFreeDf the weight's leave-two-out counts the lane's song only when it holds the key itself (lanes are
    /// not in the df document; the channel is off by default).
    /// </summary>
    private void RunLaneI(LaneAcc acc, List<long> keys, List<long>? canon, int b, int onlyA, List<long>? proj = null)
    {
        if (!ProjIn(GMel)) proj = null;
        acc.Reset();
        if (_laneMap == null) return;
        var bDoc = Data[b].DocSet;
        for (int ki = 0; ki < keys.Count; ki++)
        {
            long g = keys[ki];
            int li = _laneMap.Get(g);
            if (li < 0) continue;
            long c = canon != null && _canon ? canon[ki] : g;
            long pj = proj != null && _proj ? proj[ki] : c;
            ulong u = unchecked((ulong)g);
            int df = Df.Df(g);
            int inB = bDoc.Contains(u) ? 1 : 0;
            double w0 = Wt.W[CanonDfp(g, c, pj, Wt.Dfp(df, inB), bDoc, 0)], w1 = Wt.W[CanonDfp(g, c, pj, Wt.Dfp(df, inB + 1), bDoc, 1)];
            if (w0 == 0 && w1 == 0) continue;
            for (int e = _laneStart[li]; e < _laneStart[li + 1]; e++)
            {
                int a = _laneSong[e];
                if (a >= b) break;
                if (onlyA >= 0 && a != onlyA) continue;
                acc.Add(a, Data[a].LaneOffset + _laneIdx[e], Data[a].DocSet.Contains(u) ? w1 : w0);
            }
        }
    }

    /// <summary>The distinct keys of a group (and, with KeyFreeDf, their canonical keys in <paramref name="canon"/>, same order).</summary>
    private void KeysOf(List<GramOcc> occ, Grp g, List<long> dest, List<long> canon, bool free, bool dep, List<long>? proj = null)
    {
        if (_canon) Grams.GroupKeys(occ, g, dest, canon, free, dep, _proj ? proj : null);
        else
        {
            Grams.GroupKeys(occ, g, dest, free, dep);
            canon.Clear();
            proj?.Clear();
        }
    }

    private List<long>? CanonOrNull(List<long> canon) => _canon ? canon : null;

    private List<long>? ProjOrNull(List<long> proj) => _proj ? proj : null;

    private void BassKeys(List<GramOcc> occ, List<long> dest, List<long> canon, List<long> proj, bool free, bool dep, List<long> tmp,
        List<long> tmpCanon, List<long> tmpProj)
    {
        KeysOf(occ, Grp.BassNpc2, dest, canon, free, dep, proj);
        if (P.RiffInBass == 0) return;
        KeysOf(occ, Grp.Riff, tmp, tmpCanon, free, dep, tmpProj);
        if (!_canon)
        {
            dest.AddRange(tmp);
            Grams.SortUnique(dest);
            return;
        }
        // Merge the two sorted key lists with their canonical keys (the families differ, so no key is in both).
        var merged = new List<(long K, long C, long P)>(dest.Count + tmp.Count);
        for (int i = 0; i < dest.Count; i++) merged.Add((dest[i], canon[i], _proj ? proj[i] : canon[i]));
        for (int i = 0; i < tmp.Count; i++) merged.Add((tmp[i], tmpCanon[i], _proj ? tmpProj[i] : tmpCanon[i]));
        merged.Sort((x, y) => x.K.CompareTo(y.K));
        dest.Clear();
        canon.Clear();
        proj.Clear();
        foreach (var (k, c, pj) in merged)
        {
            if (dest.Count > 0 && dest[^1] == k) continue;
            dest.Add(k);
            canon.Add(c);
            proj.Add(pj);
        }
    }

    // ------------------------------------------------------------------------------------------------ scoring one later song
    /// <summary>
    /// Scores every song with a smaller index than <paramref name="b"/> (earlier or contemporaneous) against B.
    /// With <paramref name="onlyA"/> &gt;= 0 only that song is scored (identical arithmetic); with
    /// <paramref name="detail"/> every window's values for it are appended.
    /// </summary>
    public PairRec[] ScoreB(int b, V8Worker w, int onlyA = -1, List<WindowEval>? detail = null, int onlyWindow = -1)
    {
        var recs = new PairRec[b];
        for (int a = 0; a < b; a++)
        {
            recs[a].Z = float.NaN;
            recs[a].LaneSrc = -1;
            for (int c = 0; c < Ch.N; c++) recs[a].ZMax[c] = float.NaN;
        }
        if (b == 0) return recs;
        var song = Songs[b];
        double fdb = song.FirstDownbeat, bpb = Bpb(song);
        int ns = Shifts.Length;

        // Loop identities are song-level: one query per shift.
        var loopKeys = new List<long>[ns];
        for (int si = 0; si < ns; si++)
        {
            loopKeys[si] = Data[b].Loop != null ? SongV8.LoopKeys(song, Shifts[si]) : [];
            Run(w.Loop[si], loopKeys[si], null, b, onlyA, 4);
        }
        var loopTouched = new HashSet<int>();
        for (int si = 0; si < ns; si++) foreach (int a in w.Loop[si].Touched) loopTouched.Add(a);

        var windows = Windows(song);
        var tmpKeys = new List<long>();
        var laneKeysFree = w.LaneKeysFree;
        var laneCanonFree = w.LaneCanonFree;
        for (int wi = 0; wi < windows.Count; wi++)
        {
            if (onlyWindow >= 0 && wi != onlyWindow) continue;
            var win = windows[wi];
            w.MarkEpoch++;
            // Groups: key-free part once, key-dependent part per shift.
            RunLine(w, win.Mel, true, GMel, b, onlyA, fdb, bpb, tmpKeys);
            RunLine(w, win.Bass, false, GBass, b, onlyA, fdb, bpb, tmpKeys);
            RunLine(w, win.Bass, false, GRiff, b, onlyA, fdb, bpb, tmpKeys);
            RunChord(w, win.Chord, b, onlyA, fdb, bpb);
            // Lanes (i): B's lead window keys against A's lanes, per part.
            if (TotalLanes > 0 && win.Mel is { } mv)
            {
                w.Occ.Clear();
                Grams.Line(true, mv.On, mv.Pitch, fdb, bpb, 0, false, w.Occ, MelFilter);
                KeysOf(w.Occ, Grp.Mel, laneKeysFree, laneCanonFree, true, false, w.LaneProjFree);
                RunLaneI(w.LaneI[0], laneKeysFree, CanonOrNull(laneCanonFree), b, onlyA, ProjOrNull(w.LaneProjFree));
                for (int si = 0; si < ns; si++)
                {
                    if (Shifts[si] == 0) KeysOf(w.Occ, Grp.Mel, w.Keys, w.Canon, false, true, w.Proj);
                    else
                    {
                        w.OccS.Clear();
                        Grams.Line(true, mv.On, mv.Pitch, fdb, bpb, Shifts[si], true, w.OccS, MelFilter);
                        KeysOf(w.OccS, Grp.Mel, w.Keys, w.Canon, false, true, w.Proj);
                    }
                    RunLaneI(w.LaneI[1 + si], w.Keys, CanonOrNull(w.Canon), b, onlyA, ProjOrNull(w.Proj));
                }
            }
            else
                foreach (var la in w.LaneI) la.Reset();
            // Lanes (ii): each of B's lanes in the window against A's lead line.
            for (int j = 0; j < win.Lanes.Length; j++)
            {
                var parts = w.LaneParts(j);
                if (win.Lanes[j] is not { } lv)
                {
                    foreach (var acc in parts) acc.Reset();
                    continue;
                }
                RunLineParts(w, parts, lv, true, Grp.Mel, GMel, b, onlyA, fdb, bpb, tmpKeys);
            }
            // Which targets are touched by any query of this window (the others share one value per availability mask).
            MarkTouched(w, win, loopTouched);
            Array.Clear(w.Cache);
            int from = onlyA >= 0 ? onlyA : 0, to = onlyA >= 0 ? onlyA + 1 : b;
            for (int a = from; a < to; a++)
            {
                int mask = Data[a].Mask;
                WindowEval ev;
                if (w.Mark[a] != w.MarkEpoch && P.NullSizeAdjust == 0)
                {
                    ev = w.Cache[mask] ??= Eval(w.CacheStore[mask], w, win, -1, mask, b);
                }
                else ev = Eval(w.Tmp, w, win, a, mask, b);
                if (double.IsNaN(ev.Fused)) continue;
                ref var r = ref recs[a];
                for (int c = 0; c < Ch.N; c++)
                    if ((ev.Avail & (1 << c)) != 0 && !(ev.Z[c] <= r.ZMax[c])) r.ZMax[c] = (float)ev.Z[c];
                if (float.IsNaN(r.Z) || ev.Fused > r.Z)
                {
                    r.Z = (float)ev.Fused;
                    r.Win = (short)wi;
                    r.Shift = (sbyte)ev.Shift;
                    r.Avail = (byte)ev.Avail;
                    r.LaneSrc = (sbyte)ev.LaneSrc;
                    r.LaneIdx = (byte)ev.LaneIdx;
                    for (int c = 0; c < Ch.N; c++)
                    {
                        r.Zc[c] = (float)ev.Z[c];
                        r.Sc[c] = (float)ev.S[c];
                        r.Mu[c] = (float)ev.Mu[c];
                        r.Sd[c] = (float)ev.Sd[c];
                    }
                }
                if (detail != null && a == onlyA)
                {
                    var d = ev.Clone();
                    d.Window = wi;
                    d.T0 = win.T0;
                    d.T1 = win.T1;
                    detail.Add(d);
                }
            }
        }
        return recs;
    }

    private void RunLine(V8Worker w, LineView? view, bool melody, int group, int b, int onlyA, double fdb, double bpb, List<long> tmp)
    {
        var parts = w.G[group];
        if (view is not { } v)
        {
            foreach (var acc in parts) acc.Reset();
            return;
        }
        RunLineParts(w, parts, v, melody, group == GMel ? Grp.Mel : group == GRiff ? Grp.Riff : Grp.BassNpc2, group, b, onlyA, fdb, bpb, tmp);
    }

    private void RunLineParts(V8Worker w, Acc[] parts, LineView v, bool melody, Grp grp, int group, int b, int onlyA, double fdb, double bpb, List<long> tmp)
    {
        w.Occ.Clear();
        var mf = melody ? MelFilter : default;
        Grams.Line(melody, v.On, v.Pitch, fdb, bpb, 0, false, w.Occ, mf);
        bool bassCh = group == GBass;
        if (bassCh) BassKeys(w.Occ, w.Keys, w.Canon, w.Proj, true, false, tmp, w.TmpCanon, w.TmpProj);
        else KeysOf(w.Occ, grp, w.Keys, w.Canon, true, false, w.Proj);
        Run(parts[0], w.Keys, CanonOrNull(w.Canon), b, onlyA, group, ProjOrNull(w.Proj));
        for (int si = 0; si < Shifts.Length; si++)
        {
            var occ = w.Occ;
            if (Shifts[si] != 0)
            {
                w.OccS.Clear();
                Grams.Line(melody, v.On, v.Pitch, fdb, bpb, Shifts[si], true, w.OccS, mf);
                occ = w.OccS;
            }
            if (bassCh) BassKeys(occ, w.Keys, w.Canon, w.Proj, false, true, tmp, w.TmpCanon, w.TmpProj);
            else KeysOf(occ, grp, w.Keys, w.Canon, false, true, w.Proj);
            Run(parts[1 + si], w.Keys, CanonOrNull(w.Canon), b, onlyA, group, ProjOrNull(w.Proj));
        }
    }

    private void RunChord(V8Worker w, ChordView? view, int b, int onlyA, double fdb, double bpb)
    {
        var parts = w.G[GChord];
        if (view is not { } v)
        {
            foreach (var acc in parts) acc.Reset();
            return;
        }
        w.Occ.Clear();
        Grams.Chords(v.Tok, v.Start, v.Dur, fdb, bpb, 0, false, w.Occ);
        KeysOf(w.Occ, Grp.Chord, w.Keys, w.Canon, true, false, w.Proj);
        Run(parts[0], w.Keys, CanonOrNull(w.Canon), b, onlyA, GChord, ProjOrNull(w.Proj));
        for (int si = 0; si < Shifts.Length; si++)
        {
            var occ = w.Occ;
            if (Shifts[si] != 0)
            {
                w.OccS.Clear();
                Grams.Chords(v.Tok, v.Start, v.Dur, fdb, bpb, Shifts[si], true, w.OccS);
                occ = w.OccS;
            }
            KeysOf(occ, Grp.Chord, w.Keys, w.Canon, false, true, w.Proj);
            Run(parts[1 + si], w.Keys, CanonOrNull(w.Canon), b, onlyA, GChord, ProjOrNull(w.Proj));
        }
    }

    private void MarkTouched(V8Worker w, V8Window win, HashSet<int> loopTouched)
    {
        int e = w.MarkEpoch;
        foreach (var parts in w.G)
            foreach (var acc in parts)
                foreach (int a in acc.Touched) w.Mark[a] = e;
        for (int j = 0; j < win.Lanes.Length; j++)
            foreach (var acc in w.LaneII[j])
                foreach (int a in acc.Touched) w.Mark[a] = e;
        foreach (var la in w.LaneI)
            foreach (int a in la.TouchedSongs) w.Mark[a] = e;
        foreach (int a in loopTouched) w.Mark[a] = e;
    }

    // ------------------------------------------------------------------------------------------------ evaluation
    /// <summary>S, mu and var of target <paramref name="a"/> (a &lt; 0: untouched) from a key-free and a key-dependent part.</summary>
    private static (double S, double Mu, double Var) Raw(Acc free, Acc dep, int a)
    {
        double s = 0, mu = free.Mu0 + dep.Mu0, var = free.Var0 + dep.Var0;
        if (free.Has(a))
        {
            s += free.S[a];
            mu += free.DMu[a];
            var += free.DVar[a];
        }
        if (dep.Has(a))
        {
            s += dep.S[a];
            mu += dep.DMu[a];
            var += dep.DVar[a];
        }
        return (s, mu, var);
    }

    private static (double S, double Mu, double Var) Raw(Acc one, int a)
    {
        double s = 0, mu = one.Mu0, var = one.Var0;
        if (one.Has(a))
        {
            s += one.S[a];
            mu += one.DMu[a];
            var += one.DVar[a];
        }
        return (s, mu, var);
    }

    /// <summary>z = (S - r mu) / sqrt(r var + floor^2); r = the target's size factor (1 unless NullSizeAdjust).</summary>
    private (double Z, double S, double Mu, double Sd) Z((double S, double Mu, double Var) x, double r)
    {
        double mu = r * x.Mu, sd = Math.Sqrt(r * x.Var + Wt.Floor2);
        return ((x.S - mu) / sd, x.S, mu, sd);
    }

    /// <summary>
    /// With <c>NullSizeAdjust</c>, P(A contains g) = p(g) x |A's key set| / mean |key set| of the channel (a song with
    /// twice the distinct n-grams holds a given rare n-gram about twice as often), so mu and var scale by that ratio.
    /// </summary>
    private double R(int group, int a, int lane = -1)
    {
        if (P.NullSizeAdjust == 0 || a < 0) return 1;
        var d = Data[a];
        return group switch
        {
            GMel when lane >= 0 => d.Lanes[lane].Count / _mean[GMel],
            GMel => (d.Mel?.Count ?? 0) / _mean[GMel],
            GBass => (d.Bass?.Count ?? 0) / _mean[GBass],
            GRiff => (d.Riff?.Count ?? 0) / _mean[GRiff],
            GChord => (d.Chord?.Count ?? 0) / _mean[GChord],
            _ => (d.Loop?.Count ?? 0) / _mean[4],
        };
    }

    /// <summary>
    /// Channel z values of one target (a = -1: a target no query touched, with availability <paramref name="mask"/>)
    /// at every shift; the fused Stouffer z (loop only beside a main channel at z &gt;= LoopAuxZ), minus the shift
    /// penalty for s != 0; the best shift wins (ties: the earlier shift in the list, 0 first).
    /// </summary>
    private WindowEval Eval(WindowEval ev, V8Worker w, V8Window win, int a, int mask, int b)
    {
        ev.Fused = double.NaN;
        ev.Avail = 0;
        ev.LaneSrc = -1;
        double bestF = double.NegativeInfinity;
        Span<double> z = stackalloc double[Ch.N], s = stackalloc double[Ch.N], mu = stackalloc double[Ch.N], sd = stackalloc double[Ch.N];
        int ns = Shifts.Length;
        bool melKeys = w.G[GMel][0].Keys + w.G[GMel][1].Keys > 0 && win.Mel != null;
        bool bassKeys = w.G[GBass][0].Keys + w.G[GBass][1].Keys > 0 && win.Bass != null;
        bool riffKeys = w.G[GRiff][0].Keys + w.G[GRiff][1].Keys > 0 && win.Bass != null;
        bool chordKeys = w.G[GChord][0].Keys + w.G[GChord][1].Keys > 0 && win.Chord != null;
        bool loopAv = (mask & SongV8.MaskLoop) != 0 && w.Loop[0].Keys > 0;
        for (int si = 0; si < ns; si++)
        {
            int avail = 0;
            int laneSrc = -1, laneIdx = 0;
            if (melKeys && (mask & SongV8.MaskMel) != 0) { (z[0], s[0], mu[0], sd[0]) = Z(Raw(w.G[GMel][0], w.G[GMel][1 + si], a), R(GMel, a)); avail |= 1 << Ch.Mel; }
            if (bassKeys && (mask & SongV8.MaskBass) != 0) { (z[1], s[1], mu[1], sd[1]) = Z(Raw(w.G[GBass][0], w.G[GBass][1 + si], a), R(GBass, a)); avail |= 1 << Ch.Bass; }
            if (chordKeys && (mask & SongV8.MaskChord) != 0) { (z[2], s[2], mu[2], sd[2]) = Z(Raw(w.G[GChord][0], w.G[GChord][1 + si], a), R(GChord, a)); avail |= 1 << Ch.Chord; }
            if (riffKeys && (mask & SongV8.MaskRiff) != 0) { (z[5], s[5], mu[5], sd[5]) = Z(Raw(w.G[GRiff][0], w.G[GRiff][1 + si], a), R(GRiff, a)); avail |= 1 << Ch.Riff; }
            if (loopAv) { (z[3], s[3], mu[3], sd[3]) = Z(Raw(w.Loop[si], a), R(4, a)); avail |= 1 << Ch.Loop; }
            // Lanes: (i) B lead window vs A's lanes (A's best lane), (ii) B's lanes in the window vs A's lead.
            double zl = double.NegativeInfinity;
            if (melKeys && (mask & SongV8.MaskLanes) != 0 && TotalLanes > 0)
            {
                var raw = Raw(w.G[GMel][0], w.G[GMel][1 + si], a);
                if (a < 0)
                {
                    var (zi, si0, mi, di) = Z(raw, 1);
                    if (zi > zl) { zl = zi; s[4] = si0; mu[4] = mi; sd[4] = di; laneSrc = 0; laneIdx = 0; }
                }
                else
                {
                    var d = Data[a];
                    for (int j = 0; j < d.Lanes.Length; j++)
                    {
                        int gl = d.LaneOffset + j;
                        var (zi, si0, mi, di) = Z((w.LaneI[0].Get(gl) + w.LaneI[1 + si].Get(gl), raw.Mu, raw.Var), R(GMel, a, j));
                        if (zi > zl) { zl = zi; s[4] = si0; mu[4] = mi; sd[4] = di; laneSrc = 0; laneIdx = j; }
                    }
                }
            }
            if ((mask & SongV8.MaskMel) != 0)
                for (int j = 0; j < win.Lanes.Length; j++)
                {
                    if (win.Lanes[j] == null) continue;
                    var parts = w.LaneII[j];
                    if (parts[0].Keys + parts[1].Keys == 0) continue;
                    var (zj, sj, mj, dj) = Z(Raw(parts[0], parts[1 + si], a), R(GMel, a));
                    if (zj > zl) { zl = zj; s[4] = sj; mu[4] = mj; sd[4] = dj; laneSrc = 1 + j; laneIdx = 0; }
                }
            if (laneSrc >= 0) { z[4] = zl; avail |= 1 << Ch.Lanes; }

            double fz = Fuse(avail, z, ChanW, P);
            if (double.IsNaN(fz)) continue;
            double f = fz - (Shifts[si] != 0 ? P.ShiftPenalty : 0);
            if (f > bestF)
            {
                bestF = f;
                ev.Fused = f;
                ev.Shift = Shifts[si];
                ev.Avail = avail;
                ev.LaneSrc = laneSrc;
                ev.LaneIdx = laneIdx;
                for (int c = 0; c < Ch.N; c++)
                {
                    bool av = (avail & (1 << c)) != 0;
                    ev.Z[c] = av ? z[c] : double.NaN;
                    ev.S[c] = av ? s[c] : double.NaN;
                    ev.Mu[c] = av ? mu[c] : double.NaN;
                    ev.Sd[c] = av ? sd[c] : double.NaN;
                }
            }
        }
        return ev;
    }

    private static readonly int[] MainChannels = [Ch.Mel, Ch.Bass, Ch.Chord, Ch.Lanes];

    /// <summary>
    /// The fused z of one window: the weighted Stouffer sum <c>sum w_c z_c / sqrt(sum w_c^2)</c> over the main
    /// channels (melody, bass, chord, lanes) available in both songs (<c>FuseMode</c> 0), or over those at
    /// z &gt;= <c>FuseMinZ</c> (<c>FuseMode</c> 1; the strongest channel's z when none is). The loop channel joins
    /// only beside a main channel at z &gt;= <c>LoopAuxZ</c>. NaN when no main channel is available.
    /// </summary>
    public static double Fuse(int avail, ReadOnlySpan<double> z, double[] w, Params p)
    {
        double num = 0, den = 0, mainMax = double.NegativeInfinity;
        foreach (int c in MainChannels)
        {
            if ((avail & (1 << c)) == 0) continue;
            mainMax = Math.Max(mainMax, z[c]);
            if (p.FuseMode == 1 && !(z[c] >= p.FuseMinZ)) continue;
            num += w[c] * z[c];
            den += w[c] * w[c];
        }
        if (double.IsNegativeInfinity(mainMax)) return double.NaN;
        if (den <= 0) return p.FuseMode == 1 ? mainMax : double.NaN;
        if ((avail & (1 << Ch.Loop)) != 0 && w[Ch.Loop] > 0 && mainMax >= p.LoopAuxZ)
        {
            num += w[Ch.Loop] * z[Ch.Loop];
            den += w[Ch.Loop] * w[Ch.Loop];
        }
        return num / Math.Sqrt(den);
    }
}
