using System.Globalization;

namespace MusicHistory.Influence.Tests;

/// <summary>
/// The all-pairs engine and the decision on small in-memory corpora with planted borrowings: engine = the direct
/// V8 formula, window max, the transposition hedge and its penalty, the lanes channel, the empirical threshold,
/// the bass-alone rule, and the parent rule restricted to strong influencers. The port checks use the V2 evidence
/// settings (<see cref="V2"/>); the calibrated defaults are covered in <see cref="CalibrationTests"/>.
/// </summary>
public class DecisionTests
{
    /// <summary>The V2 evidence (exact df, no figuration filter, the benchmark's melody families, lanes on, no size adjustment).</summary>
    internal static Params V2(int hedge = 0) => new()
    {
        Hedge = hedge, Threads = 2, DfCap = 5, KeyFreeDf = 0, LineMinPc = 0, FigPeriod = 0, MelDropFams = 0, MelRhythmFams = 0,
        NullSizeAdjust = 0, WLanes = 0.7, TargetFpr = 1e-3,
    };

    /// <summary>Random material in 64 bars of 4/4 (first downbeat 0); plants overwrite spans of it.</summary>
    internal sealed class Material
    {
        public List<(double On, int P)> Mel = [], Bass = [];
        public List<(double S, double D, int T)> Chords = [];
    }

    private static readonly double[] Durs = [0.25, 0.5, 0.75, 1, 1.5, 2, 0.5, 1];

    internal static Material Random(ref Rng r)
    {
        var m = new Material();
        double t = 0;
        int p = 67;
        while (t < 256)
        {
            m.Mel.Add((t, p));
            t += Durs[r.Below(Durs.Length)];
            int step;
            do step = r.Below(15) - 7; while (step == 0);
            p = Math.Clamp(p + step, 55, 79);
        }
        t = 0;
        p = 40;
        while (t < 256)
        {
            m.Bass.Add((t, p));
            t += Durs[r.Below(Durs.Length)];
            int step;
            do step = r.Below(13) - 6; while (step == 0);
            p = Math.Clamp(p + step, 33, 52);
        }
        t = 0;
        int last = -1;
        while (t < 256)
        {
            int tok;
            do tok = r.Below(36); while (tok == last);
            last = tok;
            double d = r.Below(2) == 0 ? 2 : 4;
            m.Chords.Add((t, d, tok));
            t += d;
        }
        return m;
    }

    /// <summary>Replace the notes of a line in [t0, t1) by <paramref name="notes"/> (onsets relative to t0).</summary>
    internal static void Put(List<(double On, int P)> line, double t0, double t1, IEnumerable<(double On, int P)> notes)
    {
        line.RemoveAll(x => x.On >= t0 && x.On < t1);
        line.AddRange(notes.Select(x => (x.On + t0, x.P)));
        line.Sort((a, b) => a.On.CompareTo(b.On));
    }

    internal static List<(double On, int P)> Slice(List<(double On, int P)> line, double t0, double t1) =>
        line.Where(x => x.On >= t0 && x.On < t1).Select(x => (x.On - t0, x.P)).ToList();

    internal static NoteLine Line(List<(double On, int P)> l) => new()
    {
        Onsets = l.Select(x => x.On).ToArray(), Pitches = l.Select(x => x.P).ToArray(),
        Durs = l.Select((x, i) => i + 1 < l.Count ? l[i + 1].On - x.On : 0.5).ToArray(), Met = l.Select(_ => 3).ToArray(),
    };

    internal static Song ToSong(int i, Material m, NoteLine[]? lanes = null)
    {
        var s = new Song
        {
            Index = i, WorkId = "W" + i.ToString("D3", CultureInfo.InvariantCulture), Title = "Song " + i, Artist = "A", Year = 1950 + i,
            BeatsPerBar = 4, FirstDownbeat = 0, EndBeat = 256, DurationS = 128 + i % 7,
            Melody = Line(m.Mel), Bass = Line(m.Bass),
            Chords = new ChordLine
            {
                Tokens = m.Chords.Select(c => c.T).ToArray(), Starts = m.Chords.Select(c => c.S).ToArray(),
                Durs = m.Chords.Select(c => c.D).ToArray(), Downbeat = m.Chords.Select(_ => 1).ToArray(),
            },
            Lanes = lanes ?? [],
        };
        s.Date = DateOrder.Make(s.Year, s.Year.ToString(CultureInfo.InvariantCulture), 9, null);
        s.F = Features.Build(s, 0);
        return s;
    }

    internal static Material[] Corpus(int n, ulong seed)
    {
        var r = new Rng(seed);
        return Enumerable.Range(0, n).Select(_ => Random(ref r)).ToArray();
    }

    internal static List<long> WindowKeys(Song b, double t0, double t1, Grp g, int shift = 0)
    {
        var v = LineView.Of(g == Grp.Mel ? b.Melody! : b.Bass!, t0, t1);
        var occ = new List<GramOcc>();
        Grams.Line(g == Grp.Mel, v.On, v.Pitch, b.FirstDownbeat, b.BeatsPerBar, shift, false, occ);
        var keys = new List<long>();
        Grams.GroupKeys(occ, g, keys);
        return keys;
    }

    [Fact]
    public void EngineEqualsTheDirectFormulaAndTakesTheWindowMax()
    {
        var mats = Corpus(24, 101);
        // Song 20 quotes song 5's melody (beats 64-96) at beats 128-160.
        Put(mats[20].Mel, 128, 160, Slice(mats[5].Mel, 64, 96));
        var songs = mats.Select((m, i) => ToSong(i, m)).ToArray();
        var p = V2();
        var e = V8Engine.Build(songs, p);
        var w = e.NewWorker();
        var detail = new List<WindowEval>();
        var recs = e.ScoreB(20, w, 5, detail);
        var rec = recs[5];
        // Window max: the pair's z is the best window's fused z, and that window holds the quote.
        Assert.Equal(detail.Max(d => d.Fused), rec.Z, 4);
        var best = detail.First(d => d.Window == rec.Win);
        Assert.True(best.T0 <= 128 && best.T1 >= 160, $"winning window {best.T0}-{best.T1}");
        Assert.True(rec.Zc[Ch.Mel] > 20, $"melody z {rec.Zc[Ch.Mel]}");
        // Engine (postings walk, leave-two-out corrections) = the direct formula on the window's keys.
        var target = e.Data[5].Mel!;
        foreach (var d in detail)
        {
            var keys = WindowKeys(songs[20], d.T0, d.T1, Grp.Mel);
            if (keys.Count == 0) continue;
            var direct = V8Math.Direct(keys, target, e.Df, e.Data[5].DocSet, e.Data[20].DocSet, e.Wt);
            Assert.Equal(direct.Z, d.Z[Ch.Mel], 9);
            Assert.Equal(direct.S, d.S[Ch.Mel], 9);
            Assert.Equal(direct.Mu, d.Mu[Ch.Mel], 9);
        }
        // Scoring every earlier song at once gives the same values as scoring one.
        var all = e.ScoreB(20, w);
        for (int a = 0; a < 20; a++)
        {
            var one = e.ScoreB(20, w, a)[a];
            Assert.Equal(one.Z, all[a].Z);
            Assert.Equal(one.Win, all[a].Win);
        }
        // An unrelated pair stays near 0.
        Assert.True(all[7].Z < 5, $"unrelated pair z {all[7].Z}");

        // NullSizeAdjust: mu and var scale by A's key-set size over the channel mean (engine = direct formula with r).
        var ps = V2();
        ps.NullSizeAdjust = 1;
        var es = V8Engine.Build(songs, ps);
        var ds = new List<WindowEval>();
        es.ScoreB(20, es.NewWorker(), 5, ds);
        double mean = es.Data.Where(x => x.Mel is { Count: > 0 }).Average(x => (double)x.Mel!.Count);
        double r = es.Data[5].Mel!.Count / mean;
        Assert.NotEqual(1.0, r, 3);
        foreach (var d in ds)
        {
            var keys = WindowKeys(songs[20], d.T0, d.T1, Grp.Mel);
            if (keys.Count == 0) continue;
            var direct = V8Math.Direct(keys, target, es.Df, es.Data[5].DocSet, es.Data[20].DocSet, es.Wt, r);
            var plain = V8Math.Direct(keys, target, es.Df, es.Data[5].DocSet, es.Data[20].DocSet, es.Wt);
            Assert.Equal(direct.Z, d.Z[Ch.Mel], 9);
            Assert.Equal(plain.Mu * r, direct.Mu, 9);
            Assert.Equal(plain.Var * r, direct.Var, 9);
        }
    }

    [Fact]
    public void HedgeFindsATransposedQuoteAndPaysItsPenalty()
    {
        var mats = Corpus(20, 202);
        // Song 15 quotes song 4's melody 5 semitones higher (a region normalized a fourth off).
        Put(mats[15].Mel, 128, 176, Slice(mats[4].Mel, 64, 112).Select(x => (x.On, x.P + 5)));
        var songs = mats.Select((m, i) => ToSong(i, m)).ToArray();
        var eOff = V8Engine.Build(songs, V2());
        var off = eOff.ScoreB(15, eOff.NewWorker(), 4)[4];
        var pOn = V2(hedge: 1);
        pOn.ShiftPenalty = 12;
        var eOn = V8Engine.Build(songs, pOn);
        var detail = new List<WindowEval>();
        var on = eOn.ScoreB(15, eOn.NewWorker(), 4, detail)[4];
        Assert.Equal(-5, on.Shift);                         // B's window moved down 5 semitones matches A
        Assert.True(on.Z > off.Z + 10, $"hedge {on.Z} vs none {off.Z}");
        // The shifted window's fused z carries the penalty: fused(-5) = weighted Stouffer of the channel z - 12.
        var win = detail.First(d => d.Window == on.Win);
        double[] z = new double[Ch.N];
        for (int c = 0; c < Ch.N; c++) z[c] = win.Z[c];
        Assert.Equal(V8Engine.Fuse(win.Avail, z, eOn.ChanW, pOn) - 12, win.Fused, 9);
        // Without the key-dependent families the quote is still partly visible (intervals are key-free).
        Assert.True(off.Zc[Ch.Mel] > 3);
    }

    [Fact]
    public void LanesChannelMatchesTheLeadAgainstAnotherLane()
    {
        var mats = Corpus(16, 303);
        var r = new Rng(99);
        // Song 3 plays a string line (a lane, not its lead); song 12's lead quotes it.
        var lane = Random(ref r).Mel;
        Put(mats[12].Mel, 64, 112, Slice(lane, 32, 80));
        var songs = mats.Select((m, i) => ToSong(i, m, i == 3 ? [Line(lane)] : null)).ToArray();
        var e = V8Engine.Build(songs, V2());
        var rec = e.ScoreB(12, e.NewWorker(), 3)[3];
        Assert.True(rec.Has(Ch.Lanes));
        Assert.Equal(0, rec.LaneSrc);
        Assert.True(rec.Zc[Ch.Lanes] > 20, $"lanes z {rec.Zc[Ch.Lanes]}");
        Assert.True(rec.Zc[Ch.Mel] < 5, $"lead vs lead z {rec.Zc[Ch.Mel]}");
        // Without lane rows the channel is unavailable (and the pair is unremarkable).
        var noLanes = mats.Select((m, i) => ToSong(i, m)).ToArray();
        var e2 = V8Engine.Build(noLanes, V2());
        var rec2 = e2.ScoreB(12, e2.NewWorker(), 3)[3];
        Assert.False(rec2.Has(Ch.Lanes));
        Assert.Equal(0, e2.TotalLanes);
        // The calibrated default turns the channel off (WLanes 0): no lane is indexed even when the rows exist.
        Assert.Equal(0, new Params().WLanes);
        var e3 = V8Engine.Build(songs, new Params { Hedge = 0, Threads = 2 });
        Assert.Equal(0, e3.TotalLanes);
        Assert.False(e3.ScoreB(12, e3.NewWorker(), 3)[3].Has(Ch.Lanes));
    }

    [Fact]
    public void EmpiricalThresholdIsTheOrderStatisticOrATailFit()
    {
        var v = Enumerable.Range(0, 30000).Select(i => (double)i).ToArray();
        var t = Threshold.Of(v, 1e-3);
        Assert.Equal("empirical", t.Method);
        Assert.Equal(29969, t.Value);                       // 30 values above it: P(x > t) = 1e-3
        Assert.Equal(30 / 30000.0, v.Count(x => x > t.Value) / 30000.0, 12);
        // Fewer than 10 sample values above the target: exponential tail over the 99th percentile.
        var small = Enumerable.Range(0, 1000).Select(i => (double)i).ToArray();
        var tf = Threshold.Of(small, 1e-3);
        Assert.StartsWith("exponential", tf.Method);
        double u = 0.99 * 999, exc = small.Where(x => x > u).Average(x => x - u);
        Assert.Equal(u + Math.Log(10) * exc, tf.Value, 9);
        Assert.Equal(0.5, Threshold.Tail(small, 500), 12);
        Assert.Equal(0, Threshold.Tail(small, 1e9), 12);
        Assert.Equal(double.PositiveInfinity, Threshold.Of([], 1e-3).Value);
    }

    [Fact]
    public void BassMayNotBeTheOnlyCountingChannelWithoutRiffEvidence()
    {
        var mats = Corpus(40, 404);
        // Schema plant: songs 6 -> 30 share 16 bars of straight-eighth bass (4 pitch classes, no riff rhythm).
        int[] walk = [36, 40, 43, 45, 46, 45, 43, 40];
        var eighths = Enumerable.Range(0, 128).Select(i => (i * 0.5, walk[i % 8] + (i / 32 % 2) * 2)).ToList();
        Put(mats[6].Bass, 64, 128, eighths);
        Put(mats[30].Bass, 128, 192, eighths);
        // Riff plant: songs 9 -> 33 share a syncopated two-bar riff with repeated notes for 16 bars.
        (double, int)[] riff = [(0, 38), (0.75, 38), (1, 45), (1.5, 38), (2.25, 41), (2.5, 43), (3.5, 38), (4, 38), (4.75, 38), (5, 45), (6, 36), (6.5, 38), (7.25, 41)];
        var riffLine = Enumerable.Range(0, 8).SelectMany(k => riff.Select(x => (x.Item1 + 8 * k, x.Item2))).ToList();
        Put(mats[9].Bass, 64, 128, riffLine);
        Put(mats[33].Bass, 128, 192, riffLine);
        // Melody plant: 12 -> 36.
        Put(mats[36].Mel, 128, 176, Slice(mats[12].Mel, 64, 112));
        var songs = mats.Select((m, i) => ToSong(i, m)).ToArray();
        var p = V2();
        p.TargetFpr = 0.02;
        var st = Runner.ScoreSongs(songs, new LoadStats(), p, TextWriter.Null);
        var schema = st.StoredResult(6, 30)!;
        var riffPair = st.StoredResult(9, 33)!;
        var mel = st.StoredResult(12, 36)!;
        Assert.True(schema.Zc > st.Fused.Value, $"schema pair z {schema.Zc} vs threshold {st.Fused.Value}");
        Assert.True(schema.BassAlone && schema.BassAloneRejected && !schema.Significant);
        Assert.True(double.IsNaN(schema.RiffZ) || schema.RiffZ <= st.Riff.Value);
        Assert.True(riffPair.Zc > st.Fused.Value && riffPair.RiffZ > st.Riff.Value, $"riff z {riffPair.RiffZ} vs {st.Riff.Value}");
        Assert.True(riffPair.Significant && !riffPair.BassAloneRejected);
        Assert.True(mel.Significant && !mel.BassAlone);
        Assert.Equal(st.Fused.Value, st.Riff.Value, 12);   // RiffFactor 1: the riff alone must clear the fused threshold
        // The decided pairs got display passages: the winning window of B, located in A.
        var seg = Excerpts.Strongest(mel)!;
        Assert.True(seg.BStart <= 128 && seg.BEnd >= 176 - 16, $"passage {seg.BStart}-{seg.BEnd}");
        Assert.InRange(seg.AStart, 40, 112);
    }

    [Fact]
    public void ParentIsTheMostReferencedAmongStrongInfluencersOnly()
    {
        // B = 3 has two significant influencers: song 0 (S 100, cited by no one else) and song 1 (S 40, the most
        // referenced song of the corpus). 40 < 0.5 x 100, so popularity cannot override the evidence.
        var songs = Enumerable.Range(0, 8).Select(i =>
        {
            var s = new Song { Index = i, WorkId = "W" + i, Year = 1960 + i };
            s.Date = DateOrder.Make(s.Year, s.Year.ToString(CultureInfo.InvariantCulture), 9, null);
            return s;
        }).ToArray();
        PairResult Pr(int a, int b, double s, double bStart)
        {
            var r = new PairResult { A = a, B = b, S = s, Significant = true, Relation = "influence" };
            r.Segments.Add(new Segment { Channel = Channel.Melody, BStart = bStart, BEnd = bStart + 64, AStart = 0, AEnd = 64, Bits = s });
            return r;
        }
        var sig = new List<PairResult> { Pr(0, 3, 100, 0), Pr(1, 3, 40, 128) };
        for (int b = 4; b < 8; b++) sig.Add(Pr(1, b, 60, 0));
        sig.Sort((x, y) => x.B != y.B ? x.B.CompareTo(y.B) : x.A.CompareTo(y.A));
        var tree = Tree.Build(songs, sig, new Params());
        Assert.Equal(5, tree.RefCount[1]);
        Assert.Equal(0, tree.Parent[3]);
        // With both strong (S 100 vs 60 >= 50), the most referenced wins.
        sig[1] = Pr(1, 3, 60, 128);
        tree = Tree.Build(songs, sig, new Params());
        Assert.Equal(1, tree.Parent[3]);
    }
}
