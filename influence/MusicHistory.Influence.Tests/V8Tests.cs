using System.Globalization;

namespace MusicHistory.Influence.Tests;

/// <summary>
/// The V8 evidence: the n-gram port against golden vectors from the benchmark's Python (bench_lib.py, analytic.py),
/// the analytic corpus-null math, the rare-only cap, the leave-two-out df, and the engine's exact decomposition.
/// </summary>
public class V8Tests
{
    // ---------------------------------------------------------------- golden vectors (scratchpad diag/v2/golden.py)
    // Toy lines: first_downbeat 1, 4 beats per bar.
    private static readonly double[] MelOn = [1.0, 1.5, 2.0, 2.5, 3.0, 3.25, 3.5, 4.0, 5.0, 5.5, 6.0, 7.0, 7.5, 8.0, 9.0, 9.75, 10.0, 11.0];
    private static readonly int[] MelP = [60, 62, 64, 64, 65, 67, 69, 67, 65, 64, 62, 60, 60, 62, 64, 62, 60, 59];
    private static readonly double[] BassOn = [1.0, 1.5, 2.0, 2.5, 2.75, 3.0, 3.5, 5.0, 5.5, 6.0, 6.5, 6.75, 7.0, 7.5, 9.0];
    private static readonly int[] BassP = [36, 36, 36, 36, 36, 36, 43, 36, 36, 36, 36, 36, 36, 43, 36];
    private static readonly int[] ChT = [0, 21, 28, 15, 0, 21, 28, 15, 3];
    private static readonly double[] ChS = [1.0, 3.0, 5.0, 7.0, 9.0, 11.0, 13.0, 15.0, 17.0];
    private static readonly double[] ChD = [2.0, 2.0, 2.0, 1.5, 2.0, 2.0, 2.0, 2.0, 4.0];

    private static readonly long[] MelMix = [-8962052173224162318L, -8394538950241639794L, -7738508167088324490L, -7447547837348709043L, -7276353133751260468L, -6050516035832552402L, -5612961292640997054L, -5612220971081985200L, -5411371073013860981L, -4898320826103268818L, -4473309397435332484L, -4306083185123995266L, -4282617000850530228L, -4231475313215404522L, -3942250200926845872L, -3666361701289648174L, -3249201463254061918L, -2736348097909909469L, -2615702569067929981L, -2566364141651926693L, -2506291457379944025L, -1386331219036681412L, -1095391213764859236L, -871905269201904765L, -298918683053644791L, 697843676129721635L, 895434885668736630L, 1231611647486867154L, 1234380079943921612L, 1431438646700184269L, 1450418849269989099L, 1788824963957724249L, 1810040659251645819L, 1949124018601639214L, 1987619413783825450L, 2352480873284251781L, 2427747761388667577L, 2490975948014423026L, 2616538911093351683L, 2670574045853983901L, 3170886252823842876L, 3252216074590648433L, 3320991784218121327L, 3633987779292903598L, 3813083299602528385L, 4030887476008330003L, 4233503958684304261L, 4650441677694357774L, 4725756015045504680L, 4882756997092665833L, 4908514635277161721L, 4938792746975318764L, 4965258532847068005L, 5182530197972300677L, 5226312342569751889L, 5378318635776455266L, 5528278978274404519L, 5599297290978543409L, 5599550648292690854L, 5826376776101108768L, 6271949672251870439L, 6341893658205654480L, 7068551710318756659L, 8086443770938169684L, 8399426270299798911L, 8573872418416225943L];
    private static readonly long[] BassNpc2 = [];
    private static readonly long[] BassRs8 = [-4116086242128987734L, -3563302124349920128L, -2637247543324118090L, 228853311517664415L, 3359457676943571989L, 4688835061407070813L, 8154305100574267663L];
    private static readonly long[] ChordMix = [-8732259881319798247L, -8349572546602341967L, -8089087005808498807L, -7539622171182068627L, -6000970230735163605L, -5868337768776602946L, -5686836151845607699L, -4365236905561566451L, -4349275206996309633L, -3902309953561496762L, -3847908916672353547L, -2535415283891522224L, -1740935611487908102L, -1671715306279924919L, -1002604723317724509L, -750451819280002144L, -671108594333457286L, -228162541763562447L, 282557051803087991L, 400040137866966771L, 1213522382239838167L, 1352466082220994249L, 1858222171393589880L, 1905701916630092375L, 2160752087502310909L, 2659659823001512021L, 3643535276516461333L, 5060505062427090498L, 5714134574290791568L, 5802572570769031813L, 6450492176951375565L, 6541796739342281922L, 6863296160042788537L, 7007776175363727648L, 7367385376763393689L, 7569583998098380339L, 8231355639994128989L, 9039272500543263262L, 9200146101531500282L];
    private static readonly long[] MelMixShift5 = [-9054943455837470971L, -8853381623841937600L, -8824551160415167992L, -8394538950241639794L, -8295602966473620142L, -8081015733152926305L, -7738508167088324490L, -7447547837348709043L, -7276353133751260468L, -7162371478656516998L, -6911040162437484527L, -6050516035832552402L, -5702702151078502206L, -5612961292640997054L, -5612220971081985200L, -4898320826103268818L, -4473309397435332484L, -4306083185123995266L, -4240463533814213371L, -4231475313215404522L, -3942250200926845872L, -3788577469770606099L, -3481977151581930626L, -3249201463254061918L, -2832707609880656586L, -2736348097909909469L, -2615702569067929981L, -2566364141651926693L, -2506291457379944025L, -1965284830703792555L, -1636895404057932377L, -1632656170447767586L, -1622952667068546746L, -1386331219036681412L, -1095391213764859236L, 417536044617947540L, 1122543802501746867L, 1231611647486867154L, 1234380079943921612L, 1431438646700184269L, 1949124018601639214L, 1987619413783825450L, 2352480873284251781L, 2490975948014423026L, 2616538911093351683L, 2670574045853983901L, 3633987779292903598L, 3869283004331013218L, 4160010628450961004L, 4233503958684304261L, 4650441677694357774L, 4725756015045504680L, 4789764694584181673L, 4965258532847068005L, 5226312342569751889L, 5244848540925909941L, 5378318635776455266L, 5528278978274404519L, 5599550648292690854L, 6175100232798042481L, 6265746497729085585L, 6271949672251870439L, 6341893658205654480L, 7068551710318756659L, 8399426270299798911L, 8573872418416225943L];

    private static long[] Keys(List<GramOcc> occ, Grp g)
    {
        var l = new List<long>();
        Grams.GroupKeys(occ, g, l);
        return [.. l];
    }

    [Theory]
    // CPython 3.13: hash(t) for these tuples.
    [InlineData(new long[] { }, 5740354900026072187L)]
    [InlineData(new long[] { 5 }, -7813438383599366905L)]
    [InlineData(new long[] { 1, -1, 2, 3, 4, 5 }, -6754340686123135371L)]
    [InlineData(new long[] { 3, 0, 4, 7, 9, 11, 0 }, 6632590472318145823L)]
    [InlineData(new long[] { 98, 0, 2, 48, 50, 100, 400, 575, 12 }, -3648949212425961086L)]
    [InlineData(new long[] { -2 }, 8078679518589016365L)]
    [InlineData(new long[] { -12, 12, 0 }, -4445459356870920435L)]
    [InlineData(new long[] { 1099511627776L, 7 }, 7208148762814418017L)]
    public void TupleHashMatchesCPython(long[] items, long expected) => Assert.Equal(expected, PyHash.Tuple(items));

    [Fact]
    public void LineAndChordNgramsMatchThePythonBenchmark()
    {
        var occ = new List<GramOcc>();
        Grams.Line(true, MelOn, MelP, 1.0, 4.0, 0, false, occ);
        Assert.Equal(MelMix, Keys(occ, Grp.Mel));
        occ.Clear();
        Grams.Line(true, MelOn, MelP, 1.0, 4.0, 5, false, occ);
        Assert.Equal(MelMixShift5, Keys(occ, Grp.Mel));
        // Only the key-dependent families change under transposition: the key-free ones are shared.
        var free0 = new List<long>();
        var occ0 = new List<GramOcc>();
        Grams.Line(true, MelOn, MelP, 1.0, 4.0, 0, false, occ0);
        Grams.GroupKeys(occ0, Grp.Mel, free0, keyFree: true, keyDep: false);
        Assert.All(free0, k => Assert.Contains(k, MelMixShift5));
        var dep5 = new List<GramOcc>();
        Grams.Line(true, MelOn, MelP, 1.0, 4.0, 5, true, dep5);
        Assert.All(dep5, o => Assert.True(Grams.KeyDependent(o.Fam)));

        occ.Clear();
        Grams.Line(false, BassOn, BassP, 1.0, 4.0, 0, false, occ);
        Assert.Equal(BassNpc2, Keys(occ, Grp.BassNpc2));     // two pitch classes only: every pitch n-gram is dropped
        Assert.Equal(BassRs8, Keys(occ, Grp.RiffRaw));
        Assert.Equal(BassRs8, Keys(occ, Grp.Riff));          // syncopated (not periodic), two pitch classes: kept
        occ.Clear();
        Grams.Chords(ChT, ChS, ChD, 1.0, 4.0, 0, false, occ);
        Assert.Equal(ChordMix, Keys(occ, Grp.Chord));
    }

    [Fact]
    public void RiffSchemaFilterDropsIsochronousAndSwungLines()
    {
        Assert.True(Grams.PeriodicRhythm([0, 1, 2, 3, 4, 5, 6, 7]));                                // walking quarters
        Assert.True(Grams.PeriodicRhythm([0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5]));                        // straight eighths
        Assert.True(Grams.PeriodicRhythm([0, 2 / 3.0, 1, 5 / 3.0, 2, 8 / 3.0, 3, 11 / 3.0]));      // swung eighths
        Assert.True(Grams.PeriodicRhythm([0, 0.75, 1, 1.75, 2, 2.75, 3, 3.75]));                    // dotted shuffle
        Assert.False(Grams.PeriodicRhythm([0, 0.5, 1, 1.5, 1.75, 2, 2.5, 4]));                      // Under Pressure figure
        // A boogie line on the bass: eighth notes 1-3-5-6-b7-6-5-3 is a schema; the same pitches syncopated are a riff.
        int[] boogie = [36, 40, 43, 45, 46, 45, 43, 40, 36, 40, 43, 45, 46, 45, 43, 40];
        var even = Enumerable.Range(0, 16).Select(i => 1.0 + i * 0.5).ToArray();
        var occ = new List<GramOcc>();
        Grams.Line(false, even, boogie, 1.0, 4.0, 0, false, occ);
        Assert.Empty(Keys(occ, Grp.Riff));
        Assert.NotEmpty(Keys(occ, Grp.RiffRaw));
        var synco = new[] { 1.0, 1.75, 2.0, 2.5, 3.25, 3.5, 4.0, 4.75, 5.0, 5.75, 6.0, 6.5, 7.25, 7.5, 8.0, 8.75 };
        occ.Clear();
        Grams.Line(false, synco, boogie, 1.0, 4.0, 0, false, occ);
        Assert.NotEmpty(Keys(occ, Grp.Riff));
        // One repeated pitch class is rhythm only, whatever the rhythm.
        occ.Clear();
        Grams.Line(false, synco, Enumerable.Repeat(36, 16).ToArray(), 1.0, 4.0, 0, false, occ);
        Assert.Empty(Keys(occ, Grp.Riff));
    }

    // ---------------------------------------------------------------- the V8 math
    /// <summary>The toy corpus of golden.py: 12 songs; the melody keys (sorted) have df 1, 2, 3, 5, 7, 11 in turn, held by w0..w(df-1).</summary>
    private static (DfIndex Df, HashSet64 W0, HashSet64 W1) ToyCorpus()
    {
        var works = Enumerable.Range(0, 12).Select(_ => new HashSet<long>()).ToArray();
        int[] dfs = [1, 2, 3, 5, 7, 11];
        for (int j = 0; j < MelMix.Length; j++)
            for (int i = 0; i < dfs[j % 6]; i++) works[i].Add(MelMix[j]);
        works[1].Add(123456789);
        works[1].Add(-987654321);
        var docs = works.Select(w => w.Order().ToArray()).ToList();
        var df = DfIndex.Build(docs);
        HashSet64 Set(HashSet<long> s)
        {
            var h = new HashSet64(s.Count);
            foreach (long k in s) h.Add(unchecked((ulong)k));
            return h;
        }
        return (df, Set(works[0]), Set(works[1]));
    }

    [Theory]
    // analytic.v8(window = the toy melody, target = its first 12 notes, works w0 / w1): S, mu, sd, z
    [InlineData(0, 56.68783582133986, 12.2933300226159, 3.587237008501208, 12.37568236877455)]
    [InlineData(5, 49.45505403065887, 6.177720004054359, 3.3325014467692924, 12.9864411817615)]
    [InlineData(3, 43.25001353313989, 2.082393275691837, 2.7473500586402957, 14.984483003167977)]
    public void AnalyticNullMatchesPython(int cap, double s, double mu, double sd, double z)
    {
        var (df, w0, w1) = ToyCorpus();
        var occ = new List<GramOcc>();
        Grams.Line(true, MelOn[..12], MelP[..12], 1.0, 4.0, 0, false, occ);
        var target = new HashSet64(64);
        foreach (long k in Keys(occ, Grp.Mel)) target.Add(unchecked((ulong)k));
        var wt = new V8Weights(12, cap, 2.0);
        var r = V8Math.Direct(MelMix, target, df, w0, w1, wt);
        Assert.Equal(s, r.S, 9);
        Assert.Equal(mu, r.Mu, 9);
        Assert.Equal(sd, r.Sd(wt.Floor2), 9);
        Assert.Equal(z, r.Z, 9);
    }

    [Fact]
    public void AnalyticMeanAndVarianceByHand()
    {
        // N = 10 songs. Key a: in songs 0 (window's) and 1 (target's) -> pair df 2 (both of the pair), p = 0.
        // Key b: in songs 0, 1, 2, 3 -> pair df = 4 - 2 + 2 = 4, p = 2/8. Key c: only in song 0 -> pair df 2, p = 0, not shared.
        long a = 11, b = 22, c = 33;
        var docs = new List<long[]> { new[] { a, b, c }, new[] { a, b }, new[] { b }, new[] { b } };
        for (int i = 4; i < 10; i++) docs.Add([]);
        var df = DfIndex.Build(docs);
        HashSet64 S(params long[] k)
        {
            var h = new HashSet64(4);
            foreach (long x in k) h.Add((ulong)x);
            return h;
        }
        var wt = new V8Weights(10, 0, 2.0);
        var r = V8Math.Direct([a, b, c], S(a, b), df, S(a, b, c), S(a, b), wt);
        double W(int d) => Math.Log2(11.0 / (d + 0.5));
        double pb = 2 / 8.0;
        Assert.Equal(W(2) + W(4), r.S, 12);
        Assert.Equal(W(4) * pb, r.Mu, 12);
        Assert.Equal(W(4) * W(4) * pb * (1 - pb), r.Var, 12);
        Assert.Equal((r.S - r.Mu) / Math.Sqrt(r.Var + 4), r.Z, 12);
        // Rare-only cap 3: key b (pair df 4) weighs nothing, in S, mu and var alike.
        var capped = V8Math.Direct([a, b, c], S(a, b), df, S(a, b, c), S(a, b), new V8Weights(10, 3, 2.0));
        Assert.Equal(W(2), capped.S, 12);
        Assert.Equal(0, capped.Mu, 12);
        Assert.Equal(0, capped.Var, 12);
        // Leave-two-out: against a target song that does not hold b, b's pair df is 4 - 1 + 2 = 5 (only the window's song removed).
        var other = V8Math.Direct([a, b, c], S(a), df, S(a, b, c), S(a), wt);
        Assert.Equal(W(2), other.S, 12);
        Assert.Equal(W(5) * 3 / 8.0, other.Mu, 12);
    }

    [Fact]
    public void WeightTablesFollowTheFormula()
    {
        var wt = new V8Weights(1012, 5, 2.0);
        Assert.Equal(Math.Log2(1013 / 2.5), wt.W[2], 12);
        Assert.Equal(Math.Log2(1013 / 5.5), wt.W[5], 12);
        Assert.Equal(0, wt.W[6]);                         // pair df 6 > cap 5: not rare
        Assert.Equal(0, wt.WP[2]);                        // p = 0 when only the pair holds it
        Assert.Equal(Math.Log2(1013 / 4.5) * 2 / 1010.0, wt.WP[4], 12);
        Assert.Equal(4, wt.Floor2);
        Assert.Equal(2, wt.Dfp(0, 0));
        Assert.Equal(2, wt.Dfp(1, 1));
        Assert.Equal(5, wt.Dfp(5, 2));
    }
}
