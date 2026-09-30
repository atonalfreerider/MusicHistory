namespace MusicHistory.Influence;

/// <summary>
/// First-order Markov surrogates of the later song's token sequence (DESIGN.md §8.7). A walk
/// over B's own transitions: each step picks uniformly among the tokens that followed the
/// current state somewhere in B (state = MIDI pitch for lines, L1 token for chords) and carries
/// that token's rhythm (metric class, duration; downbeat for chords). A plain shuffle would
/// overstate significance (mir-methods.md §5.2: unrelated-pair z p95 5.38 vs 1.51 with Markov-1).
/// </summary>
internal static class Surrogates
{
    /// <summary>A surrogate of a note line (with <paramref name="keepRhythm"/>, B's rhythm stays in place).</summary>
    public static void Notes(NoteLine line, MarkovIndex mk, int shift, ref Rng rng, Seq outSeq, bool melody, NgramScratch sc,
        bool keepRhythm)
    {
        int n = line.Count;
        outSeq.Ensure(n);
        outSeq.N = n;
        var src = Src(n);
        double t = 0;
        for (int k = 0; k < n; k++)
        {
            int cur = k == 0 ? rng.Below(n) : Step(mk, MarkovIndex.Clamp(line.Pitches[src[k - 1]], 128), n, ref rng);
            src[k] = cur;
            outSeq.Pitch[k] = line.Pitches[cur] + shift;
            int r = keepRhythm ? k : cur;   // whose rhythm the surrogate note takes
            outSeq.Met[k] = (byte)Math.Clamp(line.Met[r], 0, 3);
            outSeq.Dur[k] = line.Durs[r];
            outSeq.Start[k] = keepRhythm ? line.Onsets[k] : t;
            t += line.Durs[r];
        }
        Features.FinishNotes(outSeq, melody, sc);
    }

    [ThreadStatic] private static int[]? _src;

    private static int[] Src(int n)
    {
        if (_src == null || _src.Length < n) _src = new int[Math.Max(n, 1024)];
        return _src;
    }

    public static void Chords(ChordLine line, MarkovIndex mk, int shift, ref Rng rng, Seq outSeq, bool keepRhythm)
    {
        Walk(line, mk, shift, ref rng, outSeq, keepRhythm);
        Features.FinishChords(outSeq);
    }

    private static void Walk(ChordLine line, MarkovIndex mk, int shift, ref Rng rng, Seq outSeq, bool keepRhythm)
    {
        int n = line.Count;
        outSeq.Ensure(n);
        outSeq.N = n;
        var src = Src(n);
        double t = 0;
        for (int k = 0; k < n; k++)
        {
            int cur = k == 0 ? rng.Below(n) : Step(mk, MarkovIndex.Clamp(line.Tokens[src[k - 1]], 36), n, ref rng);
            src[k] = cur;
            outSeq.Pitch[k] = Features.ShiftChord(line.Tokens[cur], shift);
            int r = keepRhythm ? k : cur;
            outSeq.Dur[k] = line.Durs[r];
            outSeq.Down[k] = (byte)(line.Downbeat[r] != 0 ? 1 : 0);
            outSeq.Start[k] = keepRhythm ? line.Starts[k] : t;
            t += line.Durs[r];
        }
    }

    private static int Step(MarkovIndex mk, int state, int n, ref Rng rng)
    {
        int lo = mk.StateStart[state], cnt = mk.StateStart[state + 1] - lo;
        return cnt == 0 ? rng.Below(n) : mk.Succ[lo + rng.Below(cnt)];
    }

    /// <summary>
    /// Loop identities of a surrogate chord walk: every run that repeats a primitive 2..8-chord
    /// cycle at least twice becomes a loop, started (like a section) on its first downbeat chord.
    /// </summary>
    public static void Loops(ChordLine line, MarkovIndex mk, int shift, ref Rng rng, Seq buf, List<LoopId> outLoops, bool keepRhythm)
    {
        Walk(line, mk, shift, ref rng, buf, keepRhythm);
        outLoops.Clear();
        int n = buf.N;
        Span<int> own = stackalloc int[8];
        Span<int> rhy = stackalloc int[8];
        int i = 0;
        while (i < n)
        {
            bool found = false;
            for (int p = 2; p <= 8 && i + 2 * p <= n; p++)
            {
                bool periodic = true;
                for (int k = 0; k < p && periodic; k++) periodic = buf.Pitch[i + k] == buf.Pitch[i + p + k];
                if (!periodic) continue;
                int end = i + 2 * p;
                while (end < n && buf.Pitch[end] == buf.Pitch[end - p]) end++;
                int o = 0;
                for (int k = 0; k < p; k++)
                    if (buf.Down[i + k] != 0) { o = k; break; }
                for (int k = 0; k < p; k++)
                {
                    own[k] = buf.Pitch[i + (o + k) % p];
                    rhy[k] = Features.DurClass(buf.Dur[i + (o + k) % p]);
                }
                var id = Features.Identity(own[..p], rhy[..p], 0, -1);
                bool dup = false;
                foreach (var l in outLoops) dup |= l.CPR == id.CPR;
                if (!dup) outLoops.Add(id);
                i = end;
                found = true;
                break;
            }
            if (!found) i++;
        }
    }
}
