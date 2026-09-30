using System.Runtime.CompilerServices;

namespace MusicHistory.Influence;

/// <summary>
/// CPython's <c>hash()</c> of a tuple of small ints (CPython 3.8+ xxHash-based tuple hash, 64-bit). The V8
/// n-gram keys are <c>hash((kind, *tokens))</c> exactly as the benchmark's <c>bench_lib.py</c> builds them,
/// so a key computed here can be looked up in the benchmark's <c>df_corpus.npz</c> and an identical key
/// means an identical n-gram (the port-fidelity check of <c>evaluate --benchmark</c>).
/// </summary>
internal static class PyHash
{
    private const ulong P1 = 11400714785074694791UL, P2 = 14029467366897019727UL, P5 = 2870177450012600261UL;
    private const long Modulus = (1L << 61) - 1;

    public const ulong Start = P5;

    /// <summary><c>hash(int)</c>: the value itself below 2^61 - 1 (reduced modulo it above), with hash(-1) = -2.</summary>
    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public static ulong Int(long v)
    {
        if (v >= 0) return (ulong)(v < Modulus ? v : v % Modulus);
        long m = v > -Modulus ? -v : -(v % Modulus);
        long h = -m;
        return unchecked((ulong)(h == -1 ? -2 : h));
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public static ulong Add(ulong acc, long item)
    {
        acc += Int(item) * P2;
        acc = (acc << 31) | (acc >> 33);
        return acc * P1;
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public static long Finish(ulong acc, int length)
    {
        acc += (ulong)length ^ (P5 ^ 3527539UL);
        return acc == ulong.MaxValue ? 1546275796L : unchecked((long)acc);
    }

    public static long Tuple(ReadOnlySpan<long> items)
    {
        ulong acc = Start;
        foreach (long x in items) acc = Add(acc, x);
        return Finish(acc, items.Length);
    }
}

/// <summary>
/// The n-gram families of the V8 evidence, a port of the benchmark's <c>bench_lib.line_ngrams</c> /
/// <c>chord_ngrams</c> (the <c>mix</c> families of <c>analytic.py</c>). The enum order is fixed; <see cref="Kind"/>
/// is the benchmark's kind code (first element of the hashed tuple).
/// </summary>
internal enum Fam : byte
{
    MInt5, MInt7, MDeg6, MType4, MIvr4, MDp3,          // melody (lead line), pitch-change sequence
    MType5, MType7,                                     // melody, optional (MelRhythmFams): interval x IOI-ratio class, 5 and 7 intervals
    BInt5, BDeg6, BIvr4, BDp4, BRs8,                    // bass; rs8 = riff slots, every note kept
    CChg3, CChg4, CChg5, CChg6, CCd3, CCd4, CKfd3, CCdp3, // chords (L1 chg)
}

/// <summary>The family groups scored as one V8 statistic (analytic.py FAMS names in brackets).</summary>
internal enum Grp
{
    Mel,       // mel_mix: int5 int7 deg6 mtype4 ivr4 dp3
    BassNpc2,  // bass_mix_npc2: int5 deg6 ivr4 dp4, n-grams spanning <= 2 pitch classes dropped
    Riff,      // rs8 with the schema filter (>= RiffMinPc pitch classes, rhythm not periodic with period 1 or 2)
    RiffRaw,   // rs8 unfiltered (benchmark bass_mix_rs, for the port check only)
    Chord,     // chord_mix: chg3-6 cd3-4 kfd3 cdp3
}

/// <summary>
/// Figuration filter for the melody families (lead line and lanes): an n-gram counts only if it spans at least
/// <see cref="MinPc"/> pitch classes and its pitch-class sequence (repeated notes collapsed) does not repeat with a
/// period of 2..<see cref="FigPeriod"/> notes (two-note alternations, broken-chord and Alberti cycles are accompaniment
/// figures, not melody). Zero turns a test off; the default keeps every n-gram (the benchmark port).
/// </summary>
internal readonly record struct LineFilter(int MinPc, int FigPeriod, int DropFams = 0, bool Extra = false, double FigShare = 1)
{
    public bool Active => MinPc > 0 || FigPeriod >= 2 || DropFams != 0;
    public static LineFilter Of(Params p) => new(p.LineMinPc, p.FigPeriod, p.MelDropFams, p.MelRhythmFams != 0, p.FigShare);
}

/// <summary>
/// One n-gram occurrence: key, its transposition-invariant (canonical) key, family, note (or chord) index span,
/// distinct pitch classes, riff schema flag.
/// </summary>
internal struct GramOcc
{
    public long Key;
    public long Canon;    // key-dependent families: the n-gram transposed so its first pitch class (chord root) is 0; key-free: = Key
    public long Proj;     // rhythm-coded families: the canonical pitch-only (harmony-only) n-gram of the same notes (chords); else = Canon
    public int Start, End;
    public Fam Fam;
    public byte Npc;
    public bool Schema;   // rs8 only: the IOIs repeat with period 1 or 2 (walking, boogie, pumping lines)
    public bool Drop;     // melody families: left out of the evidence by the LineFilter (still a document key)
}

internal static class Grams
{
    public static readonly int[] KindCode = [1, 2, 3, 4, 24, 43, 45, 47, 5, 6, 34, 54, 98, 7, 8, 9, 10, 11, 12, 63, 73];

    public static readonly string[] FamNames =
        ["m.int5", "m.int7", "m.deg6", "m.mtype4", "m.ivr4", "m.dp3", "m.mtype5", "m.mtype7", "b.int5", "b.deg6", "b.ivr4", "b.dp4", "b.rs8",
         "c.chg3", "c.chg4", "c.chg5", "c.chg6", "c.cd3", "c.cd4", "c.kfd3", "c.cdp3"];

    /// <summary>Families whose tokens carry absolute pitch classes (they change under transposition).</summary>
    public static bool KeyDependent(Fam f) => f is Fam.MDeg6 or Fam.MDp3 or Fam.BDeg6 or Fam.BDp4 or Fam.BRs8
        or Fam.CChg3 or Fam.CChg4 or Fam.CChg5 or Fam.CChg6 or Fam.CCd3 or Fam.CCd4 or Fam.CCdp3;

    /// <summary>Minimum distinct pitch classes of a riff n-gram (one repeated pitch class is rhythm only).</summary>
    public const int RiffMinPc = 2;

    /// <summary>IOIs closer than this (beats; onsets are on a 1/12-beat grid) count as equal for the schema test.</summary>
    public const double SchemaTol = 0.1;

    public static bool InGroup(in GramOcc o, Grp g) => !o.Drop && g switch
    {
        Grp.Mel => o.Fam <= Fam.MType7,
        Grp.BassNpc2 => o.Fam is Fam.BInt5 or Fam.BDeg6 or Fam.BIvr4 or Fam.BDp4 && o.Npc > 2,
        Grp.Riff => o.Fam == Fam.BRs8 && o.Npc >= RiffMinPc && !o.Schema,
        Grp.RiffRaw => o.Fam == Fam.BRs8,
        _ => o.Fam >= Fam.CChg3,
    };

    /// <summary>numpy's <c>np.mod</c> for floats: the remainder with the divisor's sign.</summary>
    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public static double PyMod(double a, double b)
    {
        double r = a % b;
        if (r != 0 && (r < 0) != (b < 0)) r += b;
        return r;
    }

    /// <summary>bench_lib.pos_in_bar: beats from the last bar line (grid from first_downbeat, beats_per_bar).</summary>
    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public static double PosInBar(double onset, double firstDownbeat, double bpb) => PyMod(onset - firstDownbeat + 1e-6, bpb);

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    private static int PyFloorMod(int a, int m)
    {
        int r = a % m;
        return r < 0 ? r + m : r;
    }

    [ThreadStatic] private static LineScratch? _ls;

    private sealed class LineScratch
    {
        public int[] Keep = [], Cp = [], Iv = [], R = [], Pcs = [], Slot = [], Apcs = [], S16 = [], Dc = [], Bpos = [], Kf = [], T = [];
        public double[] Con = [];
        public long[] Buf = new long[16];
        public int[] PcCount = new int[12], ChordCount = new int[36];

        public void Ensure(int n)
        {
            if (Keep.Length >= n + 1) return;
            int c = Math.Max(n + 1, Keep.Length * 2);
            Keep = new int[c]; Cp = new int[c]; Iv = new int[c]; R = new int[c]; Pcs = new int[c]; Slot = new int[c];
            Apcs = new int[c]; S16 = new int[c]; Dc = new int[c]; Bpos = new int[c]; Kf = new int[c]; T = new int[c];
            Con = new double[c];
        }
    }

    /// <summary>Kind code of a canonical (transposition-invariant) key: the family's code + 100 (no clash with any family code).</summary>
    public const int CanonKindOffset = 100;

    /// <summary>Kind codes of the pitch-only projections of rhythm-coded families (df only, never matched).</summary>
    public const int ProjMelInt4 = 201, ProjBassInt4 = 202, ProjMelDeg3 = 203, ProjBassDeg4 = 204;

    private static long Key(int kind, ReadOnlySpan<int> toks)
    {
        ulong acc = PyHash.Add(PyHash.Start, kind);
        foreach (int t in toks) acc = PyHash.Add(acc, t);
        return PyHash.Finish(acc, toks.Length + 1);
    }

    private static void AddLine(Fam f, ReadOnlySpan<int> toks, int k0, int k1, List<GramOcc> o, LineScratch sc, int nc, LineFilter filter,
        ReadOnlySpan<int> canon = default, long proj = 0)
    {
        int npc = Npc(sc.Pcs.AsSpan(k0, k1 - k0 + 1), sc.PcCount);
        bool drop = false;
        if (f <= Fam.MType7 && filter.Active)
            drop = (filter.DropFams & (1 << (int)f)) != 0 || filter.MinPc > 0 && npc < filter.MinPc
                   || filter.FigPeriod >= 2 && Periodic(sc.Pcs.AsSpan(k0, k1 - k0 + 1), filter.FigPeriod, filter.FigShare);
        long key = Key(KindCode[(int)f], toks);
        long c = canon.IsEmpty ? key : Key(KindCode[(int)f] + CanonKindOffset, canon);
        o.Add(new GramOcc
        {
            Key = key, Canon = c, Proj = proj != 0 ? proj : c, Fam = f,
            Start = sc.Keep[k0], End = sc.Keep[k1], Npc = (byte)npc, Drop = drop,
        });
    }

    /// <summary>A projection key: <paramref name="kind"/> over the tokens (the pitch-only content of a rhythm-coded n-gram).</summary>
    public static long ProjKey(int kind, ReadOnlySpan<int> toks) => Key(kind, toks);

    /// <summary>
    /// True when the pitch classes repeat with some period d in 2..maxPeriod (shorter than the sequence): at least
    /// <paramref name="share"/> of the positions i have pcs[i] == pcs[i + d] (1 = exactly periodic).
    /// </summary>
    public static bool Periodic(ReadOnlySpan<int> pcs, int maxPeriod, double share = 1)
    {
        for (int d = 2; d <= maxPeriod && d < pcs.Length; d++)
        {
            int m = pcs.Length - d, eq = 0;
            for (int i = 0; i < m; i++)
                if (pcs[i] == pcs[i + d]) eq++;
            if (eq >= share * m - 1e-9) return true;
        }
        return false;
    }

    private static int Npc(ReadOnlySpan<int> pcs, int[] cnt)
    {
        int n = 0;
        foreach (int p in pcs)
            if (cnt[p]++ == 0) n++;
        foreach (int p in pcs) cnt[p] = 0;
        return n;
    }

    /// <summary>
    /// The line n-grams of <c>bench_lib.line_ngrams</c> for the production families: melody int5 int7 deg6 mtype4
    /// ivr4 dp3, bass int5 deg6 ivr4 dp4 rs8. Pitches are shifted by <paramref name="shift"/> semitones; with
    /// <paramref name="keyDependentOnly"/> only the families that change under transposition are produced.
    /// Spans are original note indices (inclusive). <paramref name="onsets"/> and <paramref name="pitches"/> are
    /// the notes of the line (or of a window of it), in order. <paramref name="filter"/> drops melody-family n-grams
    /// that are accompaniment figures (default: none). Every occurrence carries its canonical key (<see cref="GramOcc.Canon"/>).
    /// </summary>
    public static void Line(bool melody, ReadOnlySpan<double> onsets, ReadOnlySpan<int> pitches, double firstDownbeat, double bpb,
        int shift, bool keyDependentOnly, List<GramOcc> outp, LineFilter filter = default)
    {
        int n = pitches.Length;
        if (n == 0) return;
        var s = _ls ??= new LineScratch();
        s.Ensure(n);
        int nc = 0;
        for (int i = 0; i < n; i++)
        {
            if (i > 0 && pitches[i] == pitches[i - 1]) continue;
            s.Keep[nc] = i;
            s.Cp[nc] = pitches[i] + shift;
            s.Con[nc] = onsets[i];
            nc++;
        }
        for (int k = 0; k + 1 < nc; k++) s.Iv[k] = Math.Clamp(s.Cp[k + 1] - s.Cp[k], -12, 12);
        for (int k = 0; k + 2 < nc; k++)
        {
            double a = s.Con[k + 1] - s.Con[k], b = s.Con[k + 2] - s.Con[k + 1];
            s.R[k] = a > 1e-9 && b > 1e-9 ? Math.Clamp((int)Math.Floor(Math.Log2(b / a) + 0.5), -2, 2) : 0;
        }
        for (int k = 0; k < nc; k++)
        {
            s.Pcs[k] = PyFloorMod(s.Cp[k], 12);
            double pos = PosInBar(s.Con[k], firstDownbeat, bpb);
            s.Slot[k] = Math.Min(15, (int)Math.Floor(pos * 2 + 1e-6));
        }
        Span<int> tok = stackalloc int[8];
        Span<int> ctk = stackalloc int[8];
        Span<int> pdg = stackalloc int[4];

        for (int k = 0; k < nc; k++)
        {
            if (k + 5 < nc)
            {
                if (!keyDependentOnly)
                {
                    for (int t = 0; t < 5; t++) tok[t] = s.Iv[k + t];
                    AddLine(melody ? Fam.MInt5 : Fam.BInt5, tok[..5], k, k + 5, outp, s, nc, filter);
                }
                for (int t = 0; t < 6; t++)
                {
                    tok[t] = s.Pcs[k + t];
                    ctk[t] = PyFloorMod(s.Pcs[k + t] - s.Pcs[k], 12);
                }
                AddLine(melody ? Fam.MDeg6 : Fam.BDeg6, tok[..6], k, k + 5, outp, s, nc, filter, ctk[..6]);
                if (melody && !keyDependentOnly)
                {
                    for (int t = 0; t < 4; t++) tok[t] = (s.Iv[k + t] + 12) * 5 + s.R[k + t] + 2;
                    AddLine(Fam.MType4, tok[..4], k, k + 5, outp, s, nc, filter, default, Key(ProjMelInt4, s.Iv.AsSpan(k, 4)));
                    // Optional longer interval x rhythm n-grams; their pitch-only content is the int5 / int7 n-gram of the same notes.
                    if (filter.Extra && k + 6 < nc)
                    {
                        for (int t = 0; t < 5; t++) tok[t] = (s.Iv[k + t] + 12) * 5 + s.R[k + t] + 2;
                        AddLine(Fam.MType5, tok[..5], k, k + 6, outp, s, nc, filter, default, Key(KindCode[(int)Fam.MInt5], s.Iv.AsSpan(k, 5)));
                    }
                    if (filter.Extra && k + 8 < nc)
                    {
                        for (int t = 0; t < 7; t++) tok[t] = (s.Iv[k + t] + 12) * 5 + s.R[k + t] + 2;
                        AddLine(Fam.MType7, tok[..7], k, k + 8, outp, s, nc, filter, default, Key(KindCode[(int)Fam.MInt7], s.Iv.AsSpan(k, 7)));
                    }
                    if (k + 7 < nc)
                    {
                        for (int t = 0; t < 7; t++) tok[t] = s.Iv[k + t];
                        AddLine(Fam.MInt7, tok[..7], k, k + 7, outp, s, nc, filter);
                    }
                }
            }
            if (!keyDependentOnly && k + 4 + 1 < nc)
            {
                for (int t = 0; t < 4; t++) tok[t] = (s.Iv[k + t] + 12) * 5 + s.R[k + t] + 2;
                AddLine(melody ? Fam.MIvr4 : Fam.BIvr4, tok[..4], k, k + 5, outp, s, nc, filter, default,
                    Key(melody ? ProjMelInt4 : ProjBassInt4, s.Iv.AsSpan(k, 4)));
            }
            int dn = melody ? 3 : 4;
            if (k + dn - 1 < nc)
            {
                for (int t = 0; t < dn; t++)
                {
                    tok[t] = s.Pcs[k + t] * 16 + s.Slot[k + t];
                    ctk[t] = PyFloorMod(s.Pcs[k + t] - s.Pcs[k], 12) * 16 + s.Slot[k + t];
                    pdg[t] = PyFloorMod(s.Pcs[k + t] - s.Pcs[k], 12);
                }
                AddLine(melody ? Fam.MDp3 : Fam.BDp4, tok[..dn], k, k + dn - 1, outp, s, nc, filter, ctk[..dn],
                    Key(melody ? ProjMelDeg3 : ProjBassDeg4, pdg[..dn]));
            }
        }
        if (melody) return;
        // Riff family: ALL notes (repeated notes kept), pitch class x sixteenth slot in the bar, 8 notes.
        for (int i = 0; i < n; i++)
        {
            s.Apcs[i] = PyFloorMod(pitches[i] + shift, 12);
            double pos = PosInBar(onsets[i], firstDownbeat, bpb);
            s.S16[i] = Math.Min(47, (int)Math.Floor(pos * 4 + 1e-6));
        }
        for (int i = 0; i + 8 <= n; i++)
        {
            for (int t = 0; t < 8; t++)
            {
                tok[t] = s.Apcs[i + t] * 48 + s.S16[i + t];
                ctk[t] = PyFloorMod(s.Apcs[i + t] - s.Apcs[i], 12) * 48 + s.S16[i + t];
            }
            long rc = Key(KindCode[(int)Fam.BRs8] + CanonKindOffset, ctk[..8]);
            outp.Add(new GramOcc
            {
                Key = Key(KindCode[(int)Fam.BRs8], tok[..8]), Canon = rc, Proj = rc,   // riffs are rhythm: no pitch-only projection
                Fam = Fam.BRs8, Start = i, End = i + 7,
                Npc = (byte)Npc(s.Apcs.AsSpan(i, 8), s.PcCount), Schema = PeriodicRhythm(onsets.Slice(i, 8)),
            });
        }
    }

    /// <summary>
    /// The riff schema test: the 7 inter-onset intervals of an 8-note riff n-gram repeat with period 1
    /// (isochronous: walking bass, straight boogie, octave pumping) or period 2 (swung or dotted boogie,
    /// long-short shuffles), within <see cref="SchemaTol"/> beats. Such lines are left to the pitch families.
    /// </summary>
    public static bool PeriodicRhythm(ReadOnlySpan<double> on)
    {
        int m = on.Length - 1;
        if (m < 3) return true;
        Span<double> d = stackalloc double[m];
        for (int t = 0; t < m; t++) d[t] = on[t + 1] - on[t];
        bool p1 = true, p2 = true;
        for (int t = 1; t < m && p1; t++) p1 = Math.Abs(d[t] - d[0]) <= SchemaTol;
        if (p1) return true;
        for (int t = 0; t + 2 < m && p2; t++) p2 = Math.Abs(d[t + 2] - d[t]) <= SchemaTol;
        return p2;
    }

    /// <summary>
    /// The chord n-grams of <c>bench_lib.chord_ngrams</c> for chord_mix: chg3-6 (L1 tokens), cd3-4 (token x duration
    /// class), kfd3 (key-free change x duration class of the arriving chord), cdp3 (token x duration class x beat in
    /// bar). Tokens are shifted by <paramref name="shift"/> semitones (root only).
    /// </summary>
    public static void Chords(ReadOnlySpan<int> tokens, ReadOnlySpan<double> starts, ReadOnlySpan<double> durs, double firstDownbeat,
        double bpb, int shift, bool keyDependentOnly, List<GramOcc> outp)
    {
        int n = tokens.Length;
        if (n == 0) return;
        var s = _ls ??= new LineScratch();
        s.Ensure(n);
        for (int i = 0; i < n; i++)
        {
            s.T[i] = Features.ShiftChord(tokens[i], shift);
            s.Dc[i] = Features.DurClass(durs[i]);
            s.Bpos[i] = Math.Min(7, (int)Math.Floor(PosInBar(starts[i], firstDownbeat, bpb) + 1e-6));
        }
        for (int i = 0; i + 1 < n; i++)
            s.Kf[i] = PyFloorMod(s.T[i + 1] / 3 - s.T[i] / 3, 12) * 9 + (s.T[i] % 3) * 3 + s.T[i + 1] % 3;
        Span<int> tok = stackalloc int[8];
        Span<int> ctk = stackalloc int[8];
        // Canonical chg3 / chg4 keys at each position: the harmony-only projections of cd3, cdp3 (3 chords) and cd4, kfd3 (4 chords).
        var c3 = new long[n];
        var c4 = new long[n];
        for (int k = 0; k < n; k++)
            for (int ln = 3; ln <= 4; ln++)
                if (k + ln <= n)
                {
                    for (int x = 0; x < ln; x++) ctk[x] = CanonChord(s.T[k + x], s.T[k]);
                    long h = Key(KindCode[(int)(ln == 3 ? Fam.CChg3 : Fam.CChg4)] + CanonKindOffset, ctk[..ln]);
                    if (ln == 3) c3[k] = h; else c4[k] = h;
                }

        for (int k = 0; k < n; k++)
        {
            for (int ln = 3; ln <= 6; ln++)
                if (k + ln <= n)
                {
                    for (int x = 0; x < ln; x++)
                    {
                        tok[x] = s.T[k + x];
                        ctk[x] = CanonChord(s.T[k + x], s.T[k]);
                    }
                    AddChord((Fam)((int)Fam.CChg3 + ln - 3), tok[..ln], k, k + ln - 1, outp, s, ctk[..ln]);
                }
            for (int ln = 3; ln <= 4; ln++)
                if (k + ln <= n)
                {
                    for (int x = 0; x < ln; x++)
                    {
                        tok[x] = s.T[k + x] * 8 + s.Dc[k + x];
                        ctk[x] = CanonChord(s.T[k + x], s.T[k]) * 8 + s.Dc[k + x];
                    }
                    AddChord(ln == 3 ? Fam.CCd3 : Fam.CCd4, tok[..ln], k, k + ln - 1, outp, s, ctk[..ln], ln == 3 ? c3[k] : c4[k]);
                }
            if (!keyDependentOnly && k + 3 <= n - 1)
            {
                for (int x = 0; x < 3; x++) tok[x] = s.Kf[k + x] * 8 + s.Dc[k + x + 1];
                AddChord(Fam.CKfd3, tok[..3], k, k + 3, outp, s, default, c4[k]);
            }
            if (k + 3 <= n)
            {
                for (int x = 0; x < 3; x++)
                {
                    tok[x] = (s.T[k + x] * 8 + s.Dc[k + x]) * 8 + s.Bpos[k + x];
                    ctk[x] = (CanonChord(s.T[k + x], s.T[k]) * 8 + s.Dc[k + x]) * 8 + s.Bpos[k + x];
                }
                AddChord(Fam.CCdp3, tok[..3], k, k + 2, outp, s, ctk[..3], c3[k]);
            }
        }
    }

    /// <summary>An L1 chord token (root x 3 + quality) with its root measured from <paramref name="first"/>'s root.</summary>
    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    private static int CanonChord(int token, int first) => PyFloorMod(token / 3 - first / 3, 12) * 3 + token % 3;

    private static void AddChord(Fam f, ReadOnlySpan<int> toks, int k0, int k1, List<GramOcc> o, LineScratch sc, ReadOnlySpan<int> canon = default,
        long proj = 0)
    {
        var cnt = sc.ChordCount;
        int npc = 0;
        for (int x = k0; x <= k1; x++)
            if (cnt[sc.T[x]]++ == 0) npc++;
        for (int x = k0; x <= k1; x++) cnt[sc.T[x]] = 0;
        long key = Key(KindCode[(int)f], toks);
        long c = canon.IsEmpty ? key : Key(KindCode[(int)f] + CanonKindOffset, canon);
        o.Add(new GramOcc
        {
            Key = key, Canon = c, Proj = proj != 0 ? proj : c, Fam = f, Start = k0, End = k1, Npc = (byte)npc,
        });
    }

    /// <summary>
    /// Distinct keys of the occurrences in <paramref name="g"/> (ascending) in <paramref name="outp"/>, and in
    /// <paramref name="canon"/> the canonical key of each (same order).
    /// </summary>
    public static void GroupKeys(List<GramOcc> occs, Grp g, List<long> outp, List<long> canon, bool keyFree = true, bool keyDep = true,
        List<long>? proj = null)
    {
        var pairs = new List<(long K, long C, long P)>();
        foreach (var o in occs)
        {
            if (!InGroup(o, g)) continue;
            bool dep = KeyDependent(o.Fam);
            if (dep ? !keyDep : !keyFree) continue;
            pairs.Add((o.Key, o.Canon, o.Proj != 0 ? o.Proj : o.Canon));
        }
        pairs.Sort((x, y) => x.K.CompareTo(y.K));
        outp.Clear();
        canon.Clear();
        proj?.Clear();
        foreach (var (k, c, pj) in pairs)
        {
            if (outp.Count > 0 && outp[^1] == k) continue;
            outp.Add(k);
            canon.Add(c);
            proj?.Add(pj);
        }
    }

    /// <summary>Distinct keys of the occurrences in <paramref name="g"/> (ascending), appended to <paramref name="outp"/> after clearing it.</summary>
    public static void GroupKeys(List<GramOcc> occs, Grp g, List<long> outp, bool keyFree = true, bool keyDep = true)
    {
        outp.Clear();
        foreach (var o in occs)
        {
            if (!InGroup(o, g)) continue;
            bool dep = KeyDependent(o.Fam);
            if (dep ? !keyDep : !keyFree) continue;
            outp.Add(o.Key);
        }
        SortUnique(outp);
    }

    public static void SortUnique(List<long> v)
    {
        if (v.Count < 2) return;
        v.Sort();
        int w = 1;
        for (int i = 1; i < v.Count; i++)
            if (v[i] != v[w - 1]) v[w++] = v[i];
        v.RemoveRange(w, v.Count - w);
    }
}
