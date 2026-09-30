using System.Globalization;

namespace MusicHistory.Influence.Tests;

/// <summary>
/// The evidence settings added by the calibration on the real data (V2.1, README "V2.1 calibration on the real data"):
/// canonical (transposition-invariant) keys and pitch-only projections of the n-grams, the weight rule that uses their
/// document frequency (KeyFreeDf), the figuration filter of the melody families, the extra rhythm families, the
/// engine's exactness under the calibrated defaults, and the defaults themselves.
/// </summary>
public class CalibrationTests
{
    private static readonly double[] On = [0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 5, 6, 7, 8, 8.5, 9, 10];
    private static readonly int[] Mel = [60, 62, 64, 67, 65, 64, 62, 60, 59, 62, 65, 69, 67, 64, 62, 60];
    private static readonly int[] ChT = [0, 21, 28, 15, 0, 22, 13, 15, 3, 21];
    private static readonly double[] ChS = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18];
    private static readonly double[] ChD = [2, 2, 2, 1.5, 2, 2, 2, 2, 4, 2];

    private static Dictionary<(Fam, int), GramOcc> ByFamStart(List<GramOcc> occ) =>
        occ.GroupBy(o => (o.Fam, o.Start)).ToDictionary(g => g.Key, g => g.First());

    [Fact]
    public void CanonicalKeysAreTranspositionInvariant()
    {
        var o0 = new List<GramOcc>();
        var o5 = new List<GramOcc>();
        Grams.Line(true, On, Mel, 0, 4, 0, false, o0);
        Grams.Line(true, On, Mel, 0, 4, 5, false, o5);
        var a = ByFamStart(o0);
        var b = ByFamStart(o5);
        Assert.Equal(a.Count, b.Count);
        foreach (var (k, x) in a)
        {
            var y = b[k];
            Assert.Equal(x.Canon, y.Canon);                    // the canonical form never changes under transposition
            if (Grams.KeyDependent(k.Item1)) Assert.NotEqual(x.Key, y.Key);
            else
            {
                Assert.Equal(x.Key, y.Key);
                Assert.Equal(x.Key, x.Canon);                  // key-free families are their own canonical form
            }
        }
        Assert.Contains(a.Keys, k => k.Item1 == Fam.MDeg6);
        Assert.Contains(a.Keys, k => k.Item1 == Fam.MDp3);

        var c0 = new List<GramOcc>();
        var c7 = new List<GramOcc>();
        Grams.Chords(ChT, ChS, ChD, 0, 4, 0, false, c0);
        Grams.Chords(ChT, ChS, ChD, 0, 4, 7, false, c7);
        var ca = ByFamStart(c0);
        var cb = ByFamStart(c7);
        foreach (var (k, x) in ca)
        {
            Assert.Equal(x.Canon, cb[k].Canon);
            Assert.Equal(k.Item1 != Fam.CKfd3, x.Key != cb[k].Key);
        }
        // The canonical form measures roots from the first chord: C-G-Am-F and D-A-Bm-G are one progression.
        var p1 = new List<GramOcc>();
        var p2 = new List<GramOcc>();
        Grams.Chords([0, 21, 28, 15], [0, 2, 4, 6], [2, 2, 2, 2], 0, 4, 0, false, p1);
        Grams.Chords([6, 27, 34, 21], [0, 2, 4, 6], [2, 2, 2, 2], 0, 4, 0, false, p2);
        var g1 = p1.First(o => o.Fam == Fam.CChg4);
        var g2 = p2.First(o => o.Fam == Fam.CChg4);
        Assert.NotEqual(g1.Key, g2.Key);
        Assert.Equal(g1.Canon, g2.Canon);

        // Bass: the riff family too.
        var r0 = new List<GramOcc>();
        var r3 = new List<GramOcc>();
        Grams.Line(false, On, Mel.Select(x => x - 24).ToArray(), 0, 4, 0, false, r0);
        Grams.Line(false, On, Mel.Select(x => x - 24).ToArray(), 0, 4, 3, false, r3);
        var ra = r0.Where(o => o.Fam == Fam.BRs8).ToList();
        var rb = r3.Where(o => o.Fam == Fam.BRs8).ToList();
        Assert.NotEmpty(ra);
        Assert.Equal(ra.Select(o => o.Canon), rb.Select(o => o.Canon));
        Assert.All(ra.Zip(rb), t => Assert.NotEqual(t.First.Key, t.Second.Key));
    }

    [Fact]
    public void ProjectionsAreThePitchOnlyContentOfRhythmCodedNgrams()
    {
        var occ = new List<GramOcc>();
        Grams.Line(true, On, Mel, 0, 4, 0, false, occ, new LineFilter(0, 0, 0, Extra: true));
        var m = ByFamStart(occ);
        // mtype5 / mtype7 weigh by the int5 / int7 n-gram of the same notes; mtype4 by its 4 intervals.
        foreach (var (k, x) in m)
        {
            if (k.Item1 == Fam.MType5) Assert.Equal(m[(Fam.MInt5, k.Item2)].Key, x.Proj);
            if (k.Item1 == Fam.MType7) Assert.Equal(m[(Fam.MInt7, k.Item2)].Key, x.Proj);
            if (k.Item1 is Fam.MInt5 or Fam.MInt7 or Fam.MDeg6) Assert.Equal(x.Canon, x.Proj);
        }
        Assert.Contains(m.Keys, k => k.Item1 == Fam.MType7);
        int[] iv = [2, 2, 3, -2];                               // 60 62 64 67 65: the first mtype4's pitch content
        Assert.Equal(Grams.ProjKey(Grams.ProjMelInt4, iv), m[(Fam.MType4, 0)].Proj);
        // Without the option the extra families are not produced (the V2 / benchmark families).
        var plain = new List<GramOcc>();
        Grams.Line(true, On, Mel, 0, 4, 0, false, plain);
        Assert.DoesNotContain(plain, o => o.Fam is Fam.MType5 or Fam.MType7);

        var ch = new List<GramOcc>();
        Grams.Chords(ChT, ChS, ChD, 0, 4, 0, false, ch);
        var c = ByFamStart(ch);
        foreach (var (k, x) in c)
        {
            switch (k.Item1)
            {
                case Fam.CCd3 or Fam.CCdp3:
                    Assert.Equal(c[(Fam.CChg3, k.Item2)].Canon, x.Proj);   // durations / beats dropped: the progression
                    break;
                case Fam.CCd4 or Fam.CKfd3:
                    Assert.Equal(c[(Fam.CChg4, k.Item2)].Canon, x.Proj);
                    break;
                default:
                    Assert.Equal(x.Canon, x.Proj);
                    break;
            }
        }
        // Riffs are rhythm: no pitch-only projection.
        var b = new List<GramOcc>();
        Grams.Line(false, On, Mel.Select(x => x - 24).ToArray(), 0, 4, 0, false, b);
        Assert.All(b.Where(o => o.Fam == Fam.BRs8), o => Assert.Equal(o.Canon, o.Proj));
    }

    [Fact]
    public void FigurationFilterDropsAlternationsAndBrokenChordCycles()
    {
        Assert.True(Grams.Periodic([0, 7, 0, 7, 0, 7], 2));
        Assert.True(Grams.Periodic([0, 4, 7, 0, 4, 7], 3));
        Assert.False(Grams.Periodic([0, 4, 7, 0, 4, 7], 2));
        Assert.False(Grams.Periodic([0, 2, 4, 5, 7, 9], 3));
        Assert.False(Grams.Periodic([0, 4, 7, 0, 4, 7, 0, 5], 3));
        Assert.True(Grams.Periodic([0, 4, 7, 0, 4, 7, 0, 5], 3, 0.75));   // 4 of 5 positions repeat after 3 notes

        double[] on = Enumerable.Range(0, 24).Select(i => i * 0.5).ToArray();
        var alternation = Enumerable.Range(0, 24).Select(i => i % 2 == 0 ? 60 : 67).ToArray();
        var arpeggio = Enumerable.Range(0, 24).Select(i => new[] { 60, 64, 67 }[i % 3]).ToArray();
        var f = new LineFilter(3, 3);
        List<GramOcc> Occ(int[] p, LineFilter lf)
        {
            var o = new List<GramOcc>();
            Grams.Line(true, on, p, 0, 4, 0, false, o, lf);
            return o;
        }
        Assert.All(Occ(alternation, f), o => Assert.True(o.Drop));                       // two pitch classes: accompaniment
        Assert.All(Occ(arpeggio, new LineFilter(3, 0)), o => Assert.False(o.Drop));       // three pitch classes pass the minimum ...
        // ... but the cycle is a figure (a 3-note dp3 n-gram is too short to show a period of 3).
        Assert.All(Occ(arpeggio, f).Where(o => o.Fam != Fam.MDp3), o => Assert.True(o.Drop));
        Assert.All(Occ(alternation, default), o => Assert.False(o.Drop));                // no filter: nothing dropped (benchmark port)
        var melody = Occ(Mel, f);
        Assert.Contains(melody, o => !o.Drop);
        // Dropped occurrences are left out of the evidence groups, not out of the song's document.
        var keys = new List<long>();
        Grams.GroupKeys(Occ(alternation, f), Grp.Mel, keys);
        Assert.Empty(keys);
        var song = new Song
        {
            Index = 0, WorkId = "W0", BeatsPerBar = 4, FirstDownbeat = 0, EndBeat = 12,
            Melody = new NoteLine { Onsets = on, Pitches = alternation, Durs = on.Select(_ => 0.5).ToArray(), Met = on.Select(_ => 3).ToArray() },
        };
        var d = SongV8.Build(song, new Params { LineMinPc = 3, FigPeriod = 3, KeyFreeDf = 0 });
        Assert.NotNull(d.Mel);
        Assert.Equal(0, d.Mel!.Count);
        Assert.All(Occ(alternation, default), o => Assert.Contains(o.Key, d.Doc));
        // The family mask: bit 4 (ivr4, the same tokens as mtype4) leaves every ivr4 occurrence out.
        var masked = Occ(Mel, new LineFilter(0, 0, 16));
        Assert.All(masked.Where(o => o.Fam == Fam.MIvr4), o => Assert.True(o.Drop));
        Assert.All(masked.Where(o => o.Fam == Fam.MType4), o => Assert.False(o.Drop));
        Assert.Equal(masked.Where(o => o.Fam == Fam.MIvr4).Select(o => o.Start), masked.Where(o => o.Fam == Fam.MType4).Select(o => o.Start));
    }

    /// <summary>A corpus whose songs 0..11 all play one progression, each in another key; songs 20 and 25 share it in a key none of the others use.</summary>
    private static Song[] StockProgressionCorpus()
    {
        var mats = DecisionTests.Corpus(30, 505);
        int[] prog = [0, 21, 28, 15, 5, 26];                     // C G Am F Dm-ish: tokens root*3 + q
        List<(double S, double D, int T)> Plant(int shift) =>
            Enumerable.Range(0, 24).Select(i => (i * 2.0, 2.0, Features.ShiftChord(prog[i % prog.Length], shift))).ToList();
        void Put(DecisionTests.Material m, double t0, int shift)
        {
            m.Chords.RemoveAll(c => c.S >= t0 && c.S < t0 + 48);
            m.Chords.AddRange(Plant(shift).Select(c => (c.S + t0, c.D, c.T)));
            m.Chords.Sort((x, y) => x.S.CompareTo(y.S));
        }
        int[] others = [0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 1];
        for (int i = 0; i < 12; i++) Put(mats[i], 64, others[i]);
        Put(mats[20], 128, 3);
        Put(mats[25], 64, 3);
        return mats.Select((m, i) => DecisionTests.ToSong(i, m)).ToArray();
    }

    [Fact]
    public void KeyFreeDfWeighsAStockProgressionByItsFrequencyInAnyKey()
    {
        var songs = StockProgressionCorpus();
        double ChordS(Params p)
        {
            var e = V8Engine.Build(songs, p);
            return e.ScoreB(25, e.NewWorker(), 20)[20].Sc[Ch.Chord];
        }
        var exact = DecisionTests.V2();
        exact.DfCap = 8;
        var keyFree = DecisionTests.V2();
        keyFree.DfCap = 8;
        keyFree.KeyFreeDf = 1;
        // In the exact key the shared progression is rare (only songs 20 and 25 hold it in that key) ...
        Assert.True(ChordS(exact) > 60, $"exact-key chord S {ChordS(exact)}");
        // ... but 14 songs play it in some key: transposition-invariant, it is a commonplace and weighs nothing.
        Assert.True(ChordS(keyFree) < 0.25 * ChordS(exact), $"key-free chord S {ChordS(keyFree)} vs exact {ChordS(exact)}");
    }

    [Fact]
    public void EngineEqualsTheDirectFormulaUnderTheCalibratedDefaults()
    {
        var songs = StockProgressionCorpus();
        var mats = DecisionTests.Corpus(30, 505);
        // Song 27 also quotes song 3's melody (beats 64-112).
        var m27 = mats[27];
        DecisionTests.Put(m27.Mel, 128, 176, DecisionTests.Slice(mats[3].Mel, 64, 112));
        songs[27] = DecisionTests.ToSong(27, m27);
        var p = new Params { Hedge = 0, Threads = 2 };             // calibrated defaults, one shift for the direct check
        Assert.Equal(2, p.KeyFreeDf);
        Assert.Equal(1, p.NullSizeAdjust);
        var e = V8Engine.Build(songs, p);
        double Mean(Func<SongV8, HashSet64?> f) => e.Data.Where(x => f(x) is { Count: > 0 }).Average(x => (double)f(x)!.Count);
        var mf = LineFilter.Of(p);

        void Check(int a, int b, Grp g, int ch, Func<SongV8, HashSet64?> target, bool proj)
        {
            var detail = new List<WindowEval>();
            e.ScoreB(b, e.NewWorker(), a, detail);
            double r = target(e.Data[a])!.Count / Mean(target);
            int checkedWindows = 0;
            foreach (var d in detail)
            {
                var occ = new List<GramOcc>();
                if (g == Grp.Chord)
                {
                    var v = ChordView.Of(songs[b].Chords!, d.T0, d.T1);
                    if (v.Count < p.MinChordChanges) continue;
                    Grams.Chords(v.Tok, v.Start, v.Dur, 0, 4, 0, false, occ);
                }
                else
                {
                    var v = LineView.Of(songs[b].Melody!, d.T0, d.T1);
                    if (v.Count < p.MinLineNotes) continue;
                    Grams.Line(true, v.On, v.Pitch, 0, 4, 0, false, occ, mf);
                }
                var keys = new List<long>();
                var canon = new List<long>();
                var pj = new List<long>();
                Grams.GroupKeys(occ, g, keys, canon, true, true, pj);
                if (keys.Count == 0) continue;
                var direct = V8Math.Direct(keys, target(e.Data[a])!, e.Df, e.Data[a].DocSet, e.Data[b].DocSet, e.Wt, r, canon, proj ? pj : null);
                Assert.Equal(direct.S, d.S[ch], 9);
                Assert.Equal(direct.Mu, d.Mu[ch], 9);
                Assert.Equal(direct.Z, d.Z[ch], 9);
                checkedWindows++;
            }
            Assert.True(checkedWindows > 5);
        }
        // Chords: canonical df and the harmony-only projection (ProjScope 1); melody: canonical df, figuration filter,
        // the extra rhythm families, no projection; both with the size-adjusted null.
        Check(20, 25, Grp.Chord, Ch.Chord, x => x.Chord, proj: true);
        Check(3, 27, Grp.Mel, Ch.Mel, x => x.Mel, proj: false);
        var rec = e.ScoreB(27, e.NewWorker(), 3)[3];
        Assert.True(rec.Zc[Ch.Mel] > 10, $"planted melody z {rec.Zc[Ch.Mel]}");
    }

    [Fact]
    public void DefaultsAreTheCalibratedOnes()
    {
        // The README's calibration table documents these values; change both together.
        var p = new Params();
        Assert.Equal(8, p.DfCap);
        Assert.Equal(3e-5, p.TargetFpr);
        Assert.Equal(30000, p.NullSample);
        Assert.Equal(2, p.KeyFreeDf);
        Assert.Equal(1, p.ProjScope);
        Assert.Equal(1, p.NullSizeAdjust);
        Assert.Equal(3, p.LineMinPc);
        Assert.Equal(3, p.FigPeriod);
        Assert.Equal(16, p.MelDropFams);
        Assert.Equal(1, p.MelRhythmFams);
        Assert.Equal(0, p.WLanes);
        Assert.Equal((1.0, 1.0, 0.7, 0.2), (p.WMelody, p.WChord, p.WBass, p.WLoop));
        Assert.Equal((1, 12.0), (p.Hedge, p.ShiftPenalty));
        Assert.Equal(1.0, p.RiffFactor);
        Assert.Equal(0.5, p.ParentFraction);
        Assert.Equal(0, p.HubZ);
        _ = CultureInfo.InvariantCulture;
    }
}
