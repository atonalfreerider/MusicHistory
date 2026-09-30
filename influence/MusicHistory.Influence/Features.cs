using System.Runtime.CompilerServices;

namespace MusicHistory.Influence;

/// <summary>An n-gram occurrence: its hash and the token span it covers (inclusive, alignment coordinates).</summary>
internal struct Occ
{
    public ulong Hash;
    public int Start, End;
    public byte Kind;   // index into Features.KindNames (diagnostics)
}

/// <summary>
/// One channel of one song (or of a surrogate) ready for alignment: tokens, per-token beats and the
/// n-gram occurrences in token order. Buffers grow and are reused, so the null model allocates
/// nothing per surrogate.
/// </summary>
internal sealed class Seq
{
    public int N;
    public byte[] Tok = [];      // melody/bass: pc * 4 + metric class; chord: L1 token
    public byte[] Run = [];      // melody/bass: same-pitch run length ending here, capped at 3
    public int[] Pitch = [];     // melody/bass: MIDI pitch (shifted); chord: L1 token (shifted)
    public double[] Start = [];
    public double[] Dur = [];
    public byte[] Met = [];
    public byte[] Down = [];
    public Occ[] Occs = [];
    public int NOcc;

    public void Ensure(int n)
    {
        if (Tok.Length >= n) return;
        int c = Math.Max(n, Tok.Length * 2);
        Array.Resize(ref Tok, c);
        Array.Resize(ref Run, c);
        Array.Resize(ref Pitch, c);
        Array.Resize(ref Start, c);
        Array.Resize(ref Dur, c);
        Array.Resize(ref Met, c);
        Array.Resize(ref Down, c);
    }

    public void EnsureOccs(int n)
    {
        if (Occs.Length < n) Array.Resize(ref Occs, Math.Max(n, Occs.Length * 2));
    }

    public double BeatStart(int i) => Start[i];
    public double BeatEnd(int i) => Start[i] + Dur[i];

    /// <summary>First occurrence whose start is at or after <paramref name="token"/>.</summary>
    public int LowerBound(int token)
    {
        int lo = 0, hi = NOcc;
        while (lo < hi)
        {
            int mid = (lo + hi) >>> 1;
            if (Occs[mid].Start < token) lo = mid + 1; else hi = mid;
        }
        return lo;
    }
}

/// <summary>Scratch arrays for the pitch-change (repeats collapsed) view of a line.</summary>
internal sealed class NgramScratch
{
    public int[] Cp = [], Cf = [], Iv = [], R = [];
    public double[] Con = [];

    public void Ensure(int n)
    {
        if (Cp.Length >= n + 1) return;
        int c = Math.Max(n + 1, Cp.Length * 2);
        Cp = new int[c];
        Cf = new int[c];
        Iv = new int[c];
        R = new int[c];
        Con = new double[c];
    }
}

/// <summary>Successor lists of a first-order Markov chain over a line's states (pitch or chord token).</summary>
internal sealed class MarkovIndex
{
    public required int[] StateStart;   // CSR offsets per state (length States + 1)
    public required int[] Succ;         // indices of successor tokens

    /// <summary>
    /// Successor lists from a state sequence. <paramref name="mode"/> 0: every transition (empirical
    /// frequencies); 1: each distinct transition once; 2: transitions inside a verbatim repeat of an
    /// earlier passage (same <paramref name="window"/> states around the transition) are skipped, so a
    /// chorus copied three times shapes the chain once.
    /// </summary>
    public static MarkovIndex Build(ReadOnlySpan<int> states, int nStates, int mode = 0, int window = 8)
    {
        var lists = new List<int>[nStates];
        var seen = new HashSet<(int, int)>();
        var seenCtx = new HashSet<ulong>();
        int half = window / 2;
        for (int j = 0; j + 1 < states.Length; j++)
        {
            int s0 = Clamp(states[j], nStates), s1 = Clamp(states[j + 1], nStates);
            if (mode == 1 && !seen.Add((s0, s1))) continue;
            if (mode == 2)
            {
                int lo = Math.Max(0, j + 1 - half), hi = Math.Min(states.Length, lo + window);
                lo = Math.Max(0, hi - window);
                ulong h = Fnv.Int(Fnv.Offset, j + 1 - lo);
                for (int k = lo; k < hi; k++) h = Fnv.Int(h, states[k]);
                if (!seenCtx.Add(h)) continue;
            }
            (lists[s0] ??= []).Add(j + 1);
        }
        var start = new int[nStates + 1];
        var succ = new List<int>();
        for (int s0 = 0; s0 < nStates; s0++)
        {
            start[s0] = succ.Count;
            if (lists[s0] != null) succ.AddRange(lists[s0]);
        }
        start[nStates] = succ.Count;
        return new MarkovIndex { StateStart = start, Succ = [.. succ] };
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public static int Clamp(int s, int n) => s < 0 ? 0 : s >= n ? n - 1 : s;
}

/// <summary>A loop identity at three nested levels (DESIGN.md §8.2): (cycle), (cycle, phase), (cycle, phase, rhythm).</summary>
internal readonly struct LoopId
{
    public readonly ulong C, CP, CPR;
    public readonly int Row;             // index into Song.Loops, -1 for a surrogate loop

    public LoopId(ulong c, ulong cp, ulong cpr, int row)
    {
        C = c;
        CP = cp;
        CPR = cpr;
        Row = row;
    }
}

/// <summary>Everything the scorer needs from a song, precomputed once (shift 0) or per fifth shift.</summary>
internal sealed class Features
{
    public Seq? Mel, Bass, Chord;
    public HashSet64? MelSet, BassSet, ChordSet;
    public LoopId[] Loops = [];
    public ulong[] Distinct = [];        // every distinct n-gram (all kinds incl. keyfree and loops), sorted
    public MarkovIndex? MelMarkov, BassMarkov, ChordMarkov;

    public static readonly string[] KindNames = ["m.int5", "m.int7", "m.deg6", "m.mtype4", "b.int5", "b.deg6", "c.chg3", "c.chg4", "c.chg5", "c.chg6", "c.cd3", "c.cd4"];

    // ---------------------------------------------------------------- n-gram kinds (FNV prefixes)
    public static readonly ulong MInt5 = Fnv.Prefix("m.int", 5), MInt7 = Fnv.Prefix("m.int", 7);
    public static readonly ulong MDeg6 = Fnv.Prefix("m.deg", 6), MType4 = Fnv.Prefix("m.mtype", 4);
    public static readonly ulong BInt5 = Fnv.Prefix("b.int", 5), BDeg6 = Fnv.Prefix("b.deg", 6);
    public static readonly ulong[] Chg = [0, 0, 0, Fnv.Prefix("c.chg", 3), Fnv.Prefix("c.chg", 4), Fnv.Prefix("c.chg", 5), Fnv.Prefix("c.chg", 6)];
    public static readonly ulong[] Cd = [0, 0, 0, Fnv.Prefix("c.cd", 3), Fnv.Prefix("c.cd", 4)];
    public static readonly ulong[] Kf = [0, 0, 0, Fnv.Prefix("c.kf", 3), Fnv.Prefix("c.kf", 4)];
    private static readonly ulong[] LoopC = Enumerable.Range(0, 17).Select(n => Fnv.Prefix("l.c", n)).ToArray();
    private static readonly ulong[] LoopCP = Enumerable.Range(0, 17).Select(n => Fnv.Prefix("l.cp", n)).ToArray();
    private static readonly ulong[] LoopCPR = Enumerable.Range(0, 17).Select(n => Fnv.Prefix("l.cpr", n)).ToArray();

    public static Features Build(Song s, int shift, NgramScratch sc, bool full, int markovMode = 0)
    {
        var f = new Features();
        if (s.Melody is { Count: > 0 } mel)
        {
            f.Mel = new Seq();
            LoadNotes(f.Mel, mel, shift);
            FinishNotes(f.Mel, melody: true, sc);
        }
        if (s.Bass is { Count: > 0 } bass)
        {
            f.Bass = new Seq();
            LoadNotes(f.Bass, bass, shift);
            FinishNotes(f.Bass, melody: false, sc);
        }
        if (s.Chords is { Count: > 0 } ch)
        {
            f.Chord = new Seq();
            LoadChords(f.Chord, ch, shift);
            FinishChords(f.Chord);
        }
        var loops = new List<LoopId>();
        for (int r = 0; r < s.Loops.Length; r++)
            if (s.Loops[r].Own.Length >= 2) loops.Add(Identity(s.Loops[r].Own, s.Loops[r].OwnRhythm, shift, r));
        f.Loops = [.. loops];
        if (!full) return f;

        f.MelSet = SetOf(f.Mel);
        f.BassSet = SetOf(f.Bass);
        f.ChordSet = SetOf(f.Chord);
        var all = new HashSet<ulong>();
        foreach (var seq in new[] { f.Mel, f.Bass, f.Chord })
            if (seq != null)
                for (int i = 0; i < seq.NOcc; i++) all.Add(seq.Occs[i].Hash);
        if (s.Chords is { Count: > 0 } c2)
            foreach (ulong h in KeyfreeHashes(c2.Tokens)) all.Add(h);
        foreach (var l in f.Loops)
        {
            all.Add(l.C);
            all.Add(l.CP);
            all.Add(l.CPR);
        }
        f.Distinct = [.. all];
        Array.Sort(f.Distinct);
        if (s.Melody is { Count: > 0 }) f.MelMarkov = MarkovIndex.Build(s.Melody.Pitches, 128, markovMode, 8);
        if (s.Bass is { Count: > 0 }) f.BassMarkov = MarkovIndex.Build(s.Bass.Pitches, 128, markovMode, 8);
        if (s.Chords is { Count: > 0 }) f.ChordMarkov = MarkovIndex.Build(s.Chords.Tokens, 36, markovMode, 6);
        return f;
    }

    private static HashSet64? SetOf(Seq? seq)
    {
        if (seq == null) return null;
        var set = new HashSet64(seq.NOcc);
        for (int i = 0; i < seq.NOcc; i++) set.Add(seq.Occs[i].Hash);
        return set;
    }

    public static void LoadNotes(Seq s, NoteLine line, int shift)
    {
        int n = line.Count;
        s.Ensure(n);
        s.N = n;
        for (int i = 0; i < n; i++)
        {
            s.Pitch[i] = line.Pitches[i] + shift;
            s.Start[i] = line.Onsets[i];
            s.Dur[i] = line.Durs[i];
            s.Met[i] = (byte)Math.Clamp(line.Met[i], 0, 3);
        }
    }

    public static void LoadChords(Seq s, ChordLine line, int shift)
    {
        int n = line.Count;
        s.Ensure(n);
        s.N = n;
        for (int i = 0; i < n; i++)
        {
            s.Pitch[i] = ShiftChord(line.Tokens[i], shift);
            s.Start[i] = line.Starts[i];
            s.Dur[i] = line.Durs[i];
            s.Down[i] = (byte)(line.Downbeat[i] != 0 ? 1 : 0);
        }
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public static int ShiftChord(int token, int shift) =>
        shift == 0 ? token : ((token / 3 + shift) % 12 + 12) % 12 * 3 + token % 3;

    /// <summary>
    /// Alignment tokens, same-pitch runs, and the melody/bass n-grams of DESIGN.md §8.2 on the
    /// pitch-change sequence (repeated notes collapsed): int 5- and 7-grams (intervals clipped to
    /// +-12), deg 6-grams (pitch classes) and mtype 4-grams (interval, IOI-ratio class) for the
    /// melody; int 5 and deg 6 for the bass. Occurrence spans are in original note indices.
    /// </summary>
    public static void FinishNotes(Seq s, bool melody, NgramScratch sc)
    {
        int n = s.N;
        for (int i = 0; i < n; i++)
        {
            int p = s.Pitch[i];
            int pc = ((p % 12) + 12) % 12;
            s.Tok[i] = (byte)(pc * 4 + (s.Met[i] & 3));
            s.Run[i] = (byte)(i > 0 && s.Pitch[i - 1] == p ? Math.Min(3, s.Run[i - 1] + 1) : 1);
        }
        sc.Ensure(n);
        int nc = 0;
        for (int i = 0; i < n; i++)
        {
            if (i > 0 && s.Pitch[i] == s.Pitch[i - 1]) continue;
            sc.Cp[nc] = s.Pitch[i];
            sc.Cf[nc] = i;
            sc.Con[nc] = s.Start[i];
            nc++;
        }
        for (int k = 0; k + 1 < nc; k++) sc.Iv[k] = Math.Clamp(sc.Cp[k + 1] - sc.Cp[k], -12, 12);
        for (int k = 0; k + 2 < nc; k++)
        {
            double a = sc.Con[k + 1] - sc.Con[k], b = sc.Con[k + 2] - sc.Con[k + 1];
            sc.R[k] = a > 1e-9 && b > 1e-9 ? Math.Clamp((int)Math.Floor(Math.Log2(b / a) + 0.5), -2, 2) : 0;
        }
        s.EnsureOccs(Math.Max(1, nc * (melody ? 4 : 2)));
        int no = 0;
        var occs = s.Occs;
        ulong pInt5 = melody ? MInt5 : BInt5, pDeg6 = melody ? MDeg6 : BDeg6;
        for (int k = 0; k + 5 < nc; k++)
        {
            ulong h = pInt5;
            for (int t = 0; t < 5; t++) h = Fnv.Int(h, sc.Iv[k + t]);
            occs[no++] = new Occ { Hash = h, Start = sc.Cf[k], End = sc.Cf[k + 5], Kind = (byte)(melody ? 0 : 4) };
            if (melody && k + 7 < nc)
            {
                h = MInt7;
                for (int t = 0; t < 7; t++) h = Fnv.Int(h, sc.Iv[k + t]);
                occs[no++] = new Occ { Hash = h, Start = sc.Cf[k], End = sc.Cf[k + 7], Kind = 1 };
            }
            h = pDeg6;
            for (int t = 0; t < 6; t++) h = Fnv.Int(h, ((sc.Cp[k + t] % 12) + 12) % 12);
            occs[no++] = new Occ { Hash = h, Start = sc.Cf[k], End = sc.Cf[k + 5], Kind = (byte)(melody ? 2 : 5) };
            if (melody)
            {
                h = MType4;
                for (int t = 0; t < 4; t++) h = Fnv.Int(h, (sc.Iv[k + t] + 12) * 5 + sc.R[k + t] + 2);
                occs[no++] = new Occ { Hash = h, Start = sc.Cf[k], End = sc.Cf[k + 5], Kind = 3 };
            }
        }
        s.NOcc = no;
    }

    /// <summary>Chord n-grams used as evidence: chg 3..6 and cd 3..4 (chg token * 8 + duration class).</summary>
    public static void FinishChords(Seq s)
    {
        int n = s.N;
        for (int i = 0; i < n; i++) s.Tok[i] = (byte)s.Pitch[i];
        s.EnsureOccs(Math.Max(1, n * 6));
        int no = 0;
        var occs = s.Occs;
        for (int k = 0; k < n; k++)
        {
            for (int len = 3; len <= 6 && k + len <= n; len++)
            {
                ulong h = Chg[len];
                for (int t = 0; t < len; t++) h = Fnv.Int(h, s.Pitch[k + t]);
                occs[no++] = new Occ { Hash = h, Start = k, End = k + len - 1, Kind = (byte)(3 + len) };
            }
            for (int len = 3; len <= 4 && k + len <= n; len++)
            {
                ulong h = Cd[len];
                for (int t = 0; t < len; t++) h = Fnv.Int(h, s.Pitch[k + t] * 8 + DurClass(s.Dur[k + t]));
                occs[no++] = new Occ { Hash = h, Start = k, End = k + len - 1, Kind = (byte)(7 + len) };
            }
        }
        s.NOcc = no;
    }

    /// <summary>Key-free change tokens <c>((root_b - root_a) % 12) * 9 + qa * 3 + qb</c>, 3- and 4-grams (recall only).</summary>
    public static IEnumerable<ulong> KeyfreeHashes(int[] chg)
    {
        int m = chg.Length - 1;
        if (m < 3) yield break;
        var kf = new int[m];
        for (int i = 0; i < m; i++)
        {
            int ra = chg[i] / 3, qa = chg[i] % 3, rb = chg[i + 1] / 3, qb = chg[i + 1] % 3;
            kf[i] = ((rb - ra) % 12 + 12) % 12 * 9 + qa * 3 + qb;
        }
        for (int k = 0; k < m; k++)
            for (int len = 3; len <= 4 && k + len <= m; len++)
            {
                ulong h = Kf[len];
                for (int t = 0; t < len; t++) h = Fnv.Int(h, kf[k + t]);
                yield return h;
            }
    }

    /// <summary>0..5 for 1/2, 1, 2, 4, 8, 16+ beats (identity/chords.py dur_class, round half up).</summary>
    public static int DurClass(double beats)
    {
        if (beats <= 0) return 0;
        return Math.Min(4, Math.Max(-1, (int)Math.Floor(Math.Log2(beats) + 0.5))) + 1;
    }

    /// <summary>Booth (1980) least rotation start, a port of identity/loops.py <c>booth</c>.</summary>
    public static int Booth(ReadOnlySpan<int> seq)
    {
        int n = seq.Length;
        if (n == 0) return 0;
        var s = new int[2 * n];
        for (int i = 0; i < 2 * n; i++) s[i] = seq[i % n];
        var f = new int[2 * n];
        Array.Fill(f, -1);
        int k = 0;
        for (int j = 1; j < 2 * n; j++)
        {
            int sj = s[j];
            int i = f[j - k - 1];
            while (i != -1 && sj != s[k + i + 1])
            {
                if (sj < s[k + i + 1]) k = j - i - 1;
                i = f[i];
            }
            if (sj != s[k + i + 1])
            {
                if (sj < s[k]) k = j;
                f[j - k] = -1;
            }
            else f[j - k] = i + 1;
        }
        return k % n;
    }

    /// <summary>Loop identity of a primitive cycle given in the song's own phase (identity/loops.py <c>identity</c>).</summary>
    public static LoopId Identity(ReadOnlySpan<int> own, ReadOnlySpan<int> ownRhythm, int shift, int row)
    {
        int n = own.Length;
        Span<int> tok = stackalloc int[n];
        for (int i = 0; i < n; i++) tok[i] = ShiftChord(own[i], shift);
        int k = Booth(tok);
        int phase = (n - k) % n;
        ulong c = n < LoopC.Length ? LoopC[n] : Fnv.Prefix("l.c", n);
        ulong cp = n < LoopCP.Length ? LoopCP[n] : Fnv.Prefix("l.cp", n);
        ulong cpr = n < LoopCPR.Length ? LoopCPR[n] : Fnv.Prefix("l.cpr", n);
        for (int t = 0; t < n; t++)
        {
            int v = tok[(k + t) % n];
            c = Fnv.Int(c, v);
            cp = Fnv.Int(cp, v);
            cpr = Fnv.Int(cpr, v);
        }
        cp = Fnv.Int(cp, phase);
        cpr = Fnv.Int(cpr, phase);
        for (int t = 0; t < n; t++) cpr = Fnv.Int(cpr, ownRhythm.Length == n ? ownRhythm[(k + t) % n] : 0);
        return new LoopId(c, cp, cpr, row);
    }
}
