namespace MusicHistory.Influence;

/// <summary>
/// Inverted index n-gram hash -> songs (DESIGN.md §8.3-4). Gives the document frequencies of the
/// rarity weights, time-sliced at the later song:
/// <c>w(g) = 0.5 log2((N_&lt;t + 1)/(df_&lt;t(g) + 0.5)) + 0.5 log2((N + 1)/(df(g) + 0.5))</c>,
/// and generates candidates from shared non-stop n-grams.
/// </summary>
internal sealed class Corpus
{
    public readonly int N;
    public readonly int StopDf;         // df > StopDf => stop-gram (skipped for candidates, still scored)
    public readonly int CapDf;          // df > CapDf => commonplace in evidence (bits capped per pair)
    public readonly int[] Cutoff;       // per song: number of songs with a strictly smaller time value
    private readonly ulong[] _keys;     // distinct hashes, ascending
    private readonly int[] _start;      // CSR offsets into _songs
    private readonly int[] _songs;      // song indices per hash, ascending

    public int DistinctCount => _keys.Length;
    public long Postings => _songs.Length;

    private Corpus(int n, int stopDf, int capDf, int[] cutoff, ulong[] keys, int[] start, int[] songs)
    {
        N = n;
        StopDf = stopDf;
        CapDf = capDf;
        Cutoff = cutoff;
        _keys = keys;
        _start = start;
        _songs = songs;
    }

    public static Corpus Build(Song[] songs, Params p)
    {
        long total = 0;
        foreach (var s in songs) total += s.F.Distinct.Length;
        var keys = new ulong[total];
        var vals = new int[total];
        long k = 0;
        foreach (var s in songs)
            foreach (ulong h in s.F.Distinct)
            {
                keys[k] = h;
                vals[k] = s.Index;
                k++;
            }
        Array.Sort(keys, vals);
        var uniq = new List<ulong>();
        var start = new List<int>();
        int i = 0;
        while (i < keys.Length)
        {
            int j = i + 1;
            while (j < keys.Length && keys[j] == keys[i]) j++;
            Array.Sort(vals, i, j - i);
            uniq.Add(keys[i]);
            start.Add(i);
            i = j;
        }
        start.Add(keys.Length);
        var cutoff = new int[songs.Length];
        for (int s = 0; s < songs.Length; s++)
            cutoff[s] = s > 0 && songs[s].TimeValue == songs[s - 1].TimeValue ? cutoff[s - 1] : s;
        int stopDf = Math.Max(p.StopMinDf, (int)Math.Floor(p.StopFraction * songs.Length));
        int capDf = Math.Max(p.StopMinDf, (int)Math.Floor(p.CapFraction * songs.Length));
        return new Corpus(songs.Length, stopDf, capDf, cutoff, [.. uniq], [.. start], vals);
    }

    public int Group(ulong h)
    {
        int g = Array.BinarySearch(_keys, h);
        return g >= 0 ? g : -1;
    }

    public int Df(int g) => _start[g + 1] - _start[g];

    public ReadOnlySpan<int> Members(int g) => _songs.AsSpan(_start[g], _start[g + 1] - _start[g]);

    /// <summary>Songs containing the n-gram among the first <paramref name="cutoff"/> songs in time order.</summary>
    public int DfBefore(int g, int cutoff)
    {
        int lo = _start[g], hi = _start[g + 1];
        while (lo < hi)
        {
            int mid = (lo + hi) >>> 1;
            if (_songs[mid] < cutoff) lo = mid + 1; else hi = mid;
        }
        return lo - _start[g];
    }

    /// <summary>Rarity in bits of n-gram <paramref name="h"/> as seen by a song whose cutoff is <paramref name="cutoff"/>.</summary>
    public double Weight(ulong h, int cutoff)
    {
        int g = Group(h);
        int df = g >= 0 ? Df(g) : 0, dfBefore = g >= 0 ? DfBefore(g, cutoff) : 0;
        return 0.5 * Math.Log2((cutoff + 1.0) / (dfBefore + 0.5)) + 0.5 * Math.Log2((N + 1.0) / (df + 0.5));
    }

    public bool IsStop(int g) => Df(g) > StopDf;

    public bool IsCommon(int g) => Df(g) > CapDf;
}

/// <summary>Rarity weights as seen from one later song, cached per worker.</summary>
internal sealed class WeightCache
{
    private readonly Dictionary<ulong, double> _w = new(8192);
    private Corpus _corpus = null!;
    private int _cutoff;
    private bool _skipStop;

    public void Reset(Corpus corpus, int cutoff, bool skipStop = false)
    {
        _corpus = corpus;
        _cutoff = cutoff;
        _skipStop = skipStop;
        _w.Clear();
    }

    /// <summary>Weight in bits; stop-grams are cached as negative values (see <see cref="Get"/>).</summary>
    private double Raw(ulong h)
    {
        if (_w.TryGetValue(h, out double w)) return w;
        w = _corpus.Weight(h, _cutoff);
        int g = _corpus.Group(h);
        if (g >= 0 && _corpus.IsCommon(g)) w = _skipStop ? 0 : -w;
        _w[h] = w;
        return w;
    }

    public double Get(ulong h, out bool stop)
    {
        double w = Raw(h);
        stop = w < 0;
        return Math.Abs(w);
    }

    public double this[ulong h] => Math.Abs(Raw(h));
}

internal readonly record struct Candidate(int A, double Bits, int Rank);

internal static class CandidateGen
{
    /// <summary>
    /// For each B, sum w over shared non-stop n-grams with each earlier A; keep the top
    /// <c>CandTop</c> plus any above <c>CandBits</c> (at most <c>CandMax</c>). Same-time songs above
    /// <c>CandBits</c> are returned separately (checked for versions only).
    /// </summary>
    public static (List<Candidate> Earlier, List<Candidate> Same) For(Song b, Song[] songs, Corpus corpus, WeightCache w,
        double[] accEarlier, double[] accSame, List<int> touched, Params p)
    {
        touched.Clear();
        foreach (ulong h in b.F.Distinct)
        {
            int g = corpus.Group(h);
            if (g < 0 || corpus.IsStop(g)) continue;
            var members = corpus.Members(g);
            if (members.Length < 2 || members[0] >= b.Index) continue;
            double wt = w[h];
            foreach (int a in members)
            {
                if (a >= b.Index) break;
                if (accEarlier[a] == 0 && accSame[a] == 0) touched.Add(a);
                if (DateOrder.Earlier(songs[a].Date, b.Date)) accEarlier[a] += wt;
                else if (!DateOrder.Earlier(b.Date, songs[a].Date)) accSame[a] += wt;
            }
        }
        var early = new List<(int A, double S)>();
        var same = new List<(int A, double S)>();
        foreach (int a in touched)
        {
            if (accEarlier[a] > 0) early.Add((a, accEarlier[a]));
            if (accSame[a] > 0) same.Add((a, accSame[a]));
            accEarlier[a] = 0;
            accSame[a] = 0;
        }
        early.Sort((x, y) => y.S != x.S ? y.S.CompareTo(x.S) : x.A.CompareTo(y.A));
        same.Sort((x, y) => y.S != x.S ? y.S.CompareTo(x.S) : x.A.CompareTo(y.A));
        var outE = new List<Candidate>();
        for (int i = 0; i < early.Count && outE.Count < p.CandMax; i++)
        {
            if (i >= p.CandTop && early[i].S <= p.CandBits) break;
            outE.Add(new Candidate(early[i].A, early[i].S, i + 1));
        }
        var outS = new List<Candidate>();
        for (int i = 0; i < same.Count && outS.Count < p.ContemporaneousMax && same[i].S > p.CandBits; i++)
            outS.Add(new Candidate(same[i].A, same[i].S, i + 1));
        return (outE, outS);
    }
}
