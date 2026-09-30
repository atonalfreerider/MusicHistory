using System.Runtime.CompilerServices;

namespace MusicHistory.Influence;

/// <summary>
/// One channel of one song (or of a window of it) ready for alignment (display passages, versions): tokens and
/// per-token beats. Buffers grow and are reused.
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

    public double BeatStart(int i) => Start[i];
    public double BeatEnd(int i) => Start[i] + Dur[i];
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

/// <summary>The alignment view of a song (display passages and version measures) and its loop identities.</summary>
internal sealed class Features
{
    public Seq? Mel, Bass, Chord;
    public LoopId[] Loops = [];

    public static Features Build(Song s, int shift)
    {
        var f = new Features();
        if (s.Melody is { Count: > 0 } mel)
        {
            f.Mel = new Seq();
            LoadNotes(f.Mel, mel, shift);
            FinishNotes(f.Mel);
        }
        if (s.Bass is { Count: > 0 } bass)
        {
            f.Bass = new Seq();
            LoadNotes(f.Bass, bass, shift);
            FinishNotes(f.Bass);
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
        return f;
    }

    /// <summary>A line (or a window of one) as an alignment sequence, pitches shifted by <paramref name="shift"/>.</summary>
    private static readonly ulong[] LoopC = Enumerable.Range(0, 17).Select(n => Fnv.Prefix("l.c", n)).ToArray();
    private static readonly ulong[] LoopCP = Enumerable.Range(0, 17).Select(n => Fnv.Prefix("l.cp", n)).ToArray();
    private static readonly ulong[] LoopCPR = Enumerable.Range(0, 17).Select(n => Fnv.Prefix("l.cpr", n)).ToArray();

    public static Seq LineSeq(LineView v, int shift)
    {
        var s = new Seq();
        s.Ensure(v.Count);
        s.N = v.Count;
        for (int i = 0; i < v.Count; i++)
        {
            s.Pitch[i] = v.Pitch[i] + shift;
            s.Start[i] = v.On[i];
            s.Dur[i] = v.Dur[i];
            s.Met[i] = (byte)Math.Clamp(v.Met[i], 0, 3);
        }
        FinishNotes(s);
        return s;
    }

    /// <summary>Chords (or a window of them) as an alignment sequence, roots shifted by <paramref name="shift"/>.</summary>
    public static Seq ChordSeq(ChordView v, int shift)
    {
        var s = new Seq();
        s.Ensure(v.Count);
        s.N = v.Count;
        for (int i = 0; i < v.Count; i++)
        {
            s.Pitch[i] = ShiftChord(v.Tok[i], shift);
            s.Start[i] = v.Start[i];
            s.Dur[i] = v.Dur[i];
        }
        FinishChords(s);
        return s;
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

    /// <summary>Alignment tokens (pitch class x 4 + metric class) and same-pitch run lengths (capped at 3).</summary>
    public static void FinishNotes(Seq s)
    {
        for (int i = 0; i < s.N; i++)
        {
            int p = s.Pitch[i];
            int pc = ((p % 12) + 12) % 12;
            s.Tok[i] = (byte)(pc * 4 + (s.Met[i] & 3));
            s.Run[i] = (byte)(i > 0 && s.Pitch[i - 1] == p ? Math.Min(3, s.Run[i - 1] + 1) : 1);
        }
    }

    /// <summary>Alignment tokens of chords: the L1 token.</summary>
    public static void FinishChords(Seq s)
    {
        for (int i = 0; i < s.N; i++)
        {
            s.Tok[i] = (byte)s.Pitch[i];
            s.Run[i] = 1;
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
