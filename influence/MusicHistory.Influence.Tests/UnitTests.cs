using System.Globalization;

namespace MusicHistory.Influence.Tests;

/// <summary>Small pieces checked against the Python identity code and DESIGN.md conventions.</summary>
public class UnitTests
{
    [Theory]
    // Expected values from musichistory.identity.loops.booth.
    [InlineData(new[] { 30, 0 }, 1)]
    [InlineData(new[] { 0, 21, 28, 15 }, 0)]
    [InlineData(new[] { 28, 15, 0, 21 }, 2)]
    [InlineData(new[] { 15, 0, 21, 28 }, 1)]
    [InlineData(new[] { 5, 3, 5, 1, 2 }, 3)]
    [InlineData(new[] { 7, 7, 1 }, 2)]
    [InlineData(new[] { 9, 24, 15, 16, 9, 30, 3 }, 6)]
    public void BoothMatchesPython(int[] seq, int expected) => Assert.Equal(expected, Features.Booth(seq));

    [Theory]
    // Expected values from musichistory.identity.chords.dur_class.
    [InlineData(0, 0)]
    [InlineData(0.5, 0)]
    [InlineData(0.7, 0)]
    [InlineData(0.75, 1)]
    [InlineData(1.4, 1)]
    [InlineData(1.5, 2)]
    [InlineData(3, 3)]
    [InlineData(4, 3)]
    [InlineData(6, 4)]
    [InlineData(12, 5)]
    [InlineData(40, 5)]
    public void DurClassMatchesPython(double beats, int expected) => Assert.Equal(expected, Features.DurClass(beats));

    [Fact]
    public void RomanMatchesPython()
    {
        // musichistory.identity.loops.roman
        Assert.Equal("bVII", Fixture.Roman(30));
        Assert.Equal("vi", Fixture.Roman(28));
        Assert.Equal("viio", Fixture.Roman(35));
        Assert.Equal("i", Fixture.Roman(28, 9, true));
        Assert.Equal("VII", Fixture.Roman(21, 9, true));
        Assert.Equal("V", Fixture.Roman(12, 9, true));
    }

    [Theory]
    [InlineData(0, "major", "C major")]
    [InlineData(10, "major", "Bb major")]
    [InlineData(6, "major", "Gb major")]
    [InlineData(1, "minor", "C# minor")]
    [InlineData(3, "minor", "Eb minor")]
    [InlineData(8, "minor", "G# minor")]
    [InlineData(9, "minor", "A minor")]
    [InlineData(2, "minor", "D minor")]
    public void KeyNamesFollowDesign(int tonic, string mode, string expected) => Assert.Equal(expected, Keys.Name(tonic, mode));

    [Fact]
    public void TimeValuesFollowPrecision()
    {
        Assert.Equal(1970.5, DateOrder.Make(1970, "1970", 9, null).TimeValue, 9);
        Assert.Equal(1970 + 2.5 / 12, DateOrder.Make(1970, "1970-03", 10, null).TimeValue, 9);
        Assert.Equal(1970 + (31 + 28 + 15 - 0.5) / 365.25, DateOrder.Make(1970, "1970-03-15", 11, null).TimeValue, 9);
        // A date from another year than work_year is ignored (year precision).
        Assert.Equal(9, DateOrder.Make(1970, "1971-03-15", 11, null).Precision);
        // Year precision with a chart week in that year uses the chart week.
        var d = DateOrder.Make(1970, "1970", 9, "1970-06-06");
        Assert.Equal(1970 + (new DateOnly(1970, 6, 6).DayOfYear - 0.5) / 365.25, d.TimeValue, 9);
        Assert.True(DateOrder.Make(1970, "1970-12-31", 11, null).TimeValue < 1971);
    }

    [Fact]
    public void OrderRules()
    {
        DateOrder D(int y, string? date, int? p, string? chart = null) => DateOrder.Make(y, date, p, chart);
        Assert.True(DateOrder.Earlier(D(1969, "1969", 9), D(1970, "1970", 9)));
        Assert.False(DateOrder.Earlier(D(1970, "1970", 9), D(1970, "1970", 9)));                       // same year: contemporaneous
        Assert.True(DateOrder.Earlier(D(1970, "1970-03", 10), D(1970, "1970-04-02", 11)));            // month precision differs
        Assert.False(DateOrder.Earlier(D(1970, "1970-03-01", 11), D(1970, "1970-03", 10)));           // same month, one month-only
        Assert.True(DateOrder.Earlier(D(1970, "1970-03-01", 11), D(1970, "1970-03-20", 11)));         // both day precision
        Assert.True(DateOrder.Earlier(D(1970, "1970", 9, "1970-02-07"), D(1970, "1970", 9, "1970-05-02")));  // chart weeks > 4 weeks apart
        Assert.False(DateOrder.Earlier(D(1970, "1970", 9, "1970-02-07"), D(1970, "1970", 9, "1970-02-28"))); // within 4 weeks
        Assert.False(DateOrder.Earlier(D(1970, "1970", 9, "1970-02-07"), D(1970, "1970", 9)));        // one chart week missing
    }

    [Fact]
    public void ChartWeekFromAnotherYearNeverOrdersSameYearSongs()
    {
        // Review finding canon #0, live DB rows: So Much in Love (Q7549445: 1963, '1963', year precision, first chart
        // week 1963-06-01) and Please Please Me (Q736799: 1963, '1963', year precision, first chart week 1964-02-01 --
        // the 1964 US reissue). Please Please Me came out in January 1963, so the 245-day chart gap must not make
        // So Much in Love the earlier song: a week from another year says nothing about order within 1963.
        var smil = DateOrder.Make(1963, "1963", 9, "1963-06-01");
        var ppm = DateOrder.Make(1963, "1963", 9, "1964-02-01");
        Assert.False(DateOrder.Earlier(smil, ppm));
        Assert.False(DateOrder.Earlier(ppm, smil));
        Assert.False(ppm.HasChart);
        Assert.Equal(1963.5, ppm.TimeValue, 9);
        Assert.True(smil.HasChart);
        // I Saw Her Standing There (1963, charted 1964-02-08) is likewise contemporaneous with So Much in Love.
        Assert.False(DateOrder.Earlier(smil, DateOrder.Make(1963, "1963", 9, "1964-02-08")));
        // Day and month precision songs do not pick up an out-of-year week either.
        Assert.False(DateOrder.Make(1963, "1963-03-22", 11, "1964-02-01").HasChart);
        Assert.False(DateOrder.Make(1963, "1963-03", 10, "1964-02-01").HasChart);
        // A week in the song's own year still orders, as before.
        Assert.True(DateOrder.Earlier(smil, DateOrder.Make(1963, "1963", 9, "1963-10-05")));
        Assert.True(DateOrder.Make(1963, "1963-03", 10, "1963-04-06").HasChart);
    }

    [Fact]
    public void NormalTailAndErfc()
    {
        Assert.Equal(0.157299207050285, Stats.Erfc(1.0), 12);
        Assert.Equal(2.209049699858544e-5, Stats.Erfc(3.0), 15);
        Assert.Equal(0.5, Stats.NormalSf(0), 12);
        Assert.Equal(0.0013498980316301, Stats.NormalSf(3), 12);
        Assert.Equal(0.9772498680518208, Stats.NormalSf(-2), 12);
    }

    [Fact]
    public void BenjaminiHochberg()
    {
        // R: p.adjust(c(0.01, 0.04, 0.03, 0.5, 0.02), "BH") = 0.05 0.05 0.05 0.50 0.05
        var q = Stats.BenjaminiHochberg([0.01, 0.04, 0.03, 0.5, 0.02]);
        Assert.Equal([0.05, 0.05, 0.05, 0.5, 0.05], q.Select(x => Math.Round(x, 10)).ToArray());
        // p.adjust(c(0.001, 0.2, 0.03, 0.04), "BH") = 0.004 0.200 0.0533 0.0533
        q = Stats.BenjaminiHochberg([0.001, 0.2, 0.03, 0.04]);
        Assert.Equal(0.004, q[0], 12);
        Assert.Equal(0.2, q[1], 12);
        Assert.Equal(0.04 * 4 / 3, q[2], 12);
        Assert.Equal(0.04 * 4 / 3, q[3], 12);
    }

    [Fact]
    public void HashSetAndRngAreDeterministic()
    {
        var set = new HashSet64(4);
        for (ulong i = 0; i < 1000; i++) Assert.True(set.Add(i * 0x9E3779B97F4A7C15UL));
        Assert.Equal(1000, set.Count);
        Assert.True(set.Contains(0));
        Assert.False(set.Add(unchecked(5 * 0x9E3779B97F4A7C15UL)));
        set.Clear();
        Assert.Equal(0, set.Count);
        var r1 = new Rng(Rng.Seed("Q1", "Q2", "melody"));
        var r2 = new Rng(Rng.Seed("Q1", "Q2", "melody"));
        var r3 = new Rng(Rng.Seed("Q1", "Q2", "bass"));
        var a = Enumerable.Range(0, 50).Select(_ => r1.Below(1000)).ToArray();
        var b = Enumerable.Range(0, 50).Select(_ => r2.Below(1000)).ToArray();
        var c = Enumerable.Range(0, 50).Select(_ => r3.Below(1000)).ToArray();
        Assert.Equal(a, b);
        Assert.NotEqual(a, c);
        Assert.All(a, x => Assert.InRange(x, 0, 999));
    }

    [Fact]
    public void FnvIsStandard()
    {
        // FNV-1a 64 of "a" is 0xaf63dc4c8601ec8c.
        Assert.Equal(0xaf63dc4c8601ec8cUL, Fnv.Text("a"));
        Assert.NotEqual(Fnv.Prefix("m.int", 5), Fnv.Prefix("b.int", 5));
    }

    [Fact]
    public void ExcerptsSnapToBars()
    {
        var s = new Song { BeatsPerBar = 4, FirstDownbeat = 1, EndBeat = 401 };
        var p = new Params();
        var (a, e) = Excerpts.Snap(s, 34.5, 50, p);          // 34.5 -> bar at 33; min 8 bars
        Assert.Equal(33, a);
        Assert.Equal(33 + 32, e);
        (a, e) = Excerpts.Snap(s, 10, 300, p);                // max 24 bars
        Assert.Equal(9, a);
        Assert.Equal(9 + 96, e);
        (a, e) = Excerpts.Snap(s, 390, 400, p);               // near the end: grows backwards
        Assert.Equal(401 - 32, a);
        Assert.Equal(401, e);
        Assert.Equal(0, (a - s.FirstDownbeat) % 4);
    }

    [Fact]
    public void LoopIdentityIsRotationInvariantAndPhaseAware()
    {
        var axis = Features.Identity([0, 21, 28, 15], [3, 3, 3, 3], 0, 0);
        var axisVi = Features.Identity([28, 15, 0, 21], [3, 3, 3, 3], 0, 0);
        var doowop = Features.Identity([0, 28, 15, 21], [3, 3, 3, 3], 0, 0);
        Assert.Equal(axis.C, axisVi.C);
        Assert.NotEqual(axis.CP, axisVi.CP);
        Assert.NotEqual(axis.C, doowop.C);
        // A fifth shift re-rotates: C-G-Am-F shifted by +7 is G-D-Em-C, a different cycle id.
        Assert.NotEqual(axis.C, Features.Identity([0, 21, 28, 15], [3, 3, 3, 3], 7, 0).C);
        Assert.Equal(21, Features.ShiftChord(0, 7));
        Assert.Equal(13, Features.ShiftChord(28, 7));   // Am + 7 = Em
    }

    [Fact]
    public void ParamsOverride()
    {
        var p = new Params();
        p.Set("KScreen=10");
        p.Set("zmin=3.5");
        Assert.Equal(10, p.KScreen);
        Assert.Equal(3.5, p.ZMin);
        Assert.Throws<ArgumentException>(() => p.Set("nope=1"));
        Assert.NotNull(p.ToJson()["StopCapBits"]);
        _ = CultureInfo.InvariantCulture;
    }
}
