namespace MusicHistory.Influence.Tests;

/// <summary>Local alignment kernels: hits, consolidation, and the AVX2 kernel against the scalar one.</summary>
public class AlignTests
{
    private static readonly Params P = new();
    private static readonly Scoring Sc = new(P);
    private static readonly AlignParams Note = new(Sc.MelSub, 48, Sc.MelOpen, Sc.MelExt, Sc.Cons, Sc.MinHit);
    private static readonly AlignParams Chord = new(Sc.ChordSub, 36, Sc.ChordOpen, Sc.ChordExt, 0, Sc.MinHit);

    private static Seq Line(int[] pitches, double dur = 0.5)
    {
        var line = new NoteLine
        {
            Pitches = pitches, Onsets = pitches.Select((_, i) => i * dur).ToArray(), Durs = pitches.Select(_ => dur).ToArray(),
            Met = pitches.Select((_, i) => i % 8 == 0 ? 0 : i % 2 == 0 ? 1 : 2).ToArray(),
        };
        var s = new Seq();
        Features.LoadNotes(s, line, 0);
        Features.FinishNotes(s, true, new NgramScratch());
        return s;
    }

    private static int[] RandomPitches(ref Rng r, int n)
    {
        int[] scale = [0, 2, 4, 5, 7, 9, 11];
        var p = new int[n];
        int d = 7;
        for (int i = 0; i < n; i++)
        {
            d = Math.Clamp(d + r.Below(5) - 2, 0, 14);
            p[i] = 60 + 12 * (d / 7) + scale[d % 7];
        }
        return p;
    }

    [Fact]
    public void ScoringTablesFollowDesign()
    {
        // melody: +2 same pc, 0 for 1-2 semitones, -1 otherwise, x0.75 off metric; scaled by 20
        Assert.Equal(40, Sc.MelSub[(0 * 4 + 0) * 48 + (0 * 4 + 0)]);
        Assert.Equal(30, Sc.MelSub[(0 * 4 + 0) * 48 + (0 * 4 + 1)]);
        Assert.Equal(0, Sc.MelSub[(0 * 4 + 0) * 48 + (2 * 4 + 0)]);
        Assert.Equal(-20, Sc.MelSub[(0 * 4 + 0) * 48 + (4 * 4 + 0)]);
        Assert.Equal(40, Sc.MelSub[(11 * 4 + 2) * 48 + (11 * 4 + 2)]);
        Assert.Equal(0, Sc.MelSub[(11 * 4 + 2) * 48 + (0 * 4 + 2)]);   // B vs C: 1 semitone around the octave
        // chords: s = 3J - 1 (+0.25 same root): identical +2, parallel +0.75, relative +0.5, fifth -0.4, tritone -1
        Assert.Equal(40, Sc.ChordSub[0 * 36 + 0]);
        Assert.Equal(15, Sc.ChordSub[0 * 36 + 1]);      // C vs Cm
        Assert.Equal(10, Sc.ChordSub[0 * 36 + 28]);     // C vs Am
        Assert.Equal(-8, Sc.ChordSub[0 * 36 + 21]);     // C vs G
        Assert.Equal(-20, Sc.ChordSub[0 * 36 + 18]);    // C vs F#
        Assert.Equal(60, Sc.MelOpen);
        Assert.Equal(6, Sc.MelExt);
        Assert.Equal(4, Sc.Cons);
        Assert.Equal(50, Sc.ChordOpen);
        Assert.Equal(15, Sc.ChordExt);
    }

    [Fact]
    public void IdenticalLinesAlignEndToEnd()
    {
        var r = new Rng(3);
        var p = RandomPitches(ref r, 60);
        var a = Line(p);
        var b = Line(p);
        var al = new LocalAligner();
        var hits = new Hit[3];
        int n = al.Hits(a, b, Note, hits);
        Assert.True(n >= 1);
        Assert.Equal(0, hits[0].A0);
        Assert.Equal(59, hits[0].A1);
        Assert.Equal(0, hits[0].B0);
        Assert.Equal(59, hits[0].B1);
        Assert.True(hits[0].Score >= 60 * 30);
    }

    [Fact]
    public void HitsDoNotOverlap()
    {
        var r = new Rng(11);
        var al = new LocalAligner();
        var hits = new Hit[3];
        for (int t = 0; t < 30; t++)
        {
            var a = Line(RandomPitches(ref r, 80 + r.Below(80)));
            var b = Line(RandomPitches(ref r, 80 + r.Below(80)));
            int n = al.Hits(a, b, Note, hits);
            for (int x = 0; x < n; x++)
            {
                Assert.True(hits[x].A0 <= hits[x].A1 && hits[x].B0 <= hits[x].B1);
                Assert.True(hits[x].Score >= Sc.MinHit);
                if (x > 0) Assert.True(hits[x].Score <= hits[x - 1].Score);
                for (int y = 0; y < x; y++)
                {
                    Assert.False(hits[x].A0 <= hits[y].A1 && hits[y].A0 <= hits[x].A1, "A spans overlap");
                    Assert.False(hits[x].B0 <= hits[y].B1 && hits[y].B0 <= hits[x].B1, "B spans overlap");
                }
            }
        }
    }

    [Fact]
    public void ConsolidationAbsorbsASplitNote()
    {
        // B sings the phrase with one note split into two syllables (same pitch).
        int[] phrase = [60, 62, 64, 65, 67, 69, 71, 72, 71, 69, 67, 65];
        int[] split = [60, 62, 64, 65, 67, 67, 69, 71, 72, 71, 69, 67, 65];
        var al = new LocalAligner();
        var hits = new Hit[1];
        al.Hits(Line(phrase), Line(split), Note, hits);
        int withCons = hits[0].Score;
        var noCons = new AlignParams(Sc.MelSub, 48, Sc.MelOpen, Sc.MelExt, 0, Sc.MinHit);
        al.Hits(Line(phrase), Line(split), noCons, hits);
        Assert.True(withCons > hits[0].Score, $"consolidation {withCons} vs gap {hits[0].Score}");
    }

    [Fact]
    public void Avx2KernelMatchesScalar()
    {
        var r = new Rng(7);
        var al = new LocalAligner();
        var bests = new Best[8];
        var batch = Enumerable.Range(0, 8).Select(_ => new Seq()).ToArray();
        var sc = new NgramScratch();
        foreach (var ap in new[] { Note, Chord })
        {
            for (int t = 0; t < 6; t++)
            {
                int n = 40 + r.Below(120), m = 40 + r.Below(120);
                Seq a;
                if (ap.Cons > 0)
                {
                    a = Line(RandomPitches(ref r, n));
                    var src = new NoteLine
                    {
                        Pitches = RandomPitches(ref r, m), Onsets = Enumerable.Range(0, m).Select(i => i * 0.5).ToArray(),
                        Durs = Enumerable.Repeat(0.5, m).ToArray(), Met = Enumerable.Range(0, m).Select(i => i % 4).ToArray(),
                    };
                    // repeated notes exercise the consolidation lanes
                    for (int i = 1; i < m; i += 5) src.Pitches[i] = src.Pitches[i - 1];
                    var mk = MarkovIndex.Build(src.Pitches, 128);
                    for (int l = 0; l < 8; l++) Surrogates.Notes(src, mk, 0, ref r, batch[l], true, sc, false);
                }
                else
                {
                    a = ChordSeq(ref r, n);
                    for (int l = 0; l < 8; l++) batch[l] = ChordSeq(ref r, m);
                }
                int count = 1 + r.Below(8);
                al.Best8(a, batch, count, ap, bests);
                for (int l = 0; l < count; l++)
                {
                    var s = al.BestIn(a, batch[l], ap, 1, a.N, 1, batch[l].N);
                    Assert.Equal(s.Score, bests[l].Score);
                    Assert.Equal((s.I0, s.J0, s.I, s.J), (bests[l].I0, bests[l].J0, bests[l].I, bests[l].J));
                }
            }
        }
    }

    private static Seq ChordSeq(ref Rng r, int n)
    {
        int[] pool = [0, 7, 13, 15, 21, 28, 30, 9, 24];
        var s = new Seq();
        s.Ensure(n);
        s.N = n;
        int last = -1;
        for (int i = 0; i < n; i++)
        {
            int t;
            do t = pool[r.Below(pool.Length)]; while (t == last);
            last = t;
            s.Pitch[i] = t;
            s.Start[i] = i * 2;
            s.Dur[i] = 2;
        }
        Features.FinishChords(s);
        return s;
    }

    [Fact]
    public void GlobalIdentityCountsIdentities()
    {
        var nw = new GlobalIdentity();
        Assert.Equal(1.0, nw.Pid([1, 2, 3, 4, 5], [1, 2, 3, 4, 5], 12, 6), 9);
        Assert.Equal(0.0, nw.Pid([1, 1, 1, 1], [2, 2, 2, 2], 12, 6), 9);
        // one substitution out of 8
        Assert.Equal(7 / 8.0, nw.Pid([1, 2, 3, 4, 5, 6, 7, 8], [1, 2, 3, 9, 5, 6, 7, 8], 12, 6), 9);
    }
}
