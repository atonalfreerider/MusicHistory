using System.Globalization;
using System.Reflection;

namespace MusicHistory.Influence.Tests;

/// <summary>Synthetic songs, loops and families for the identity-lineage tests.</summary>
internal static class Lin
{
    // L1 tokens (C/Am frame): root * 3 + quality (0 major, 1 minor, 2 diminished).
    public const int C = 0, Cm = 1, D = 6, Dm = 7, E = 12, Em = 13, F = 15, Fm = 16, G = 21, Gm = 22, Ab = 24, A = 27, Am = 28, Bb = 30, Bbm = 31,
        Bm = 34, Bdim = 35, Eb = 9, Edim = 14, Gsdim = 26;

    public static Song Song(int i, int year, IEnumerable<(int Tok, double Beats)>? chords = null, string mode = "major", double bpb = 4,
        LoopRow[]? loops = null, NoteLine? bass = null, int? month = null, double endBeat = 0)
    {
        var list = (chords ?? []).ToList();
        double t = 0;
        var st = new List<double>();
        foreach (var c in list)
        {
            st.Add(t);
            t += c.Beats;
        }
        var s = new Song
        {
            Index = i, WorkId = "L" + i.ToString("D3", CultureInfo.InvariantCulture), Title = "Song " + i, Artist = "A", Year = year, Mode = mode,
            BeatsPerBar = bpb, FirstDownbeat = 0, EndBeat = Math.Max(t, endBeat),
            Chords = list.Count == 0 ? null : new ChordLine
            {
                Tokens = list.Select(c => c.Tok).ToArray(), Starts = [.. st], Durs = list.Select(c => c.Beats).ToArray(), Downbeat = new int[list.Count],
            },
            Loops = loops ?? [], Bass = bass,
        };
        s.Date = month is int m
            ? DateOrder.Make(year, $"{year}-{m.ToString("D2", CultureInfo.InvariantCulture)}", 10, null)
            : DateOrder.Make(year, year.ToString(CultureInfo.InvariantCulture), 9, null);
        return s;
    }

    public static IEnumerable<(int, double)> Rep(int times, params (int, double)[] pattern) => Enumerable.Repeat(pattern, times).SelectMany(x => x);

    /// <summary>A loop row: its own tokens and duration classes, one visit of <paramref name="passes"/> passes per start.</summary>
    public static LoopRow Loop(int[] own, int[] rhythm, double loopBeats, int passes, params double[] visitStarts) => new()
    {
        Family = 1, Own = own, OwnRhythm = rhythm, LoopBeats = loopBeats, Passes = passes, Visits = visitStarts.Length,
        Coverage = loopBeats * passes * visitStarts.Length, VisitStarts = visitStarts,
    };

    public static NoteLine Line(params (double On, int P)[] notes) => new()
    {
        Onsets = notes.Select(x => x.On).ToArray(), Pitches = notes.Select(x => x.P).ToArray(),
        Durs = notes.Select((x, i) => i + 1 < notes.Length ? notes[i + 1].On - x.On : 1).ToArray(), Met = notes.Select(_ => 0).ToArray(),
    };

    public static Family Fam(int id, FamilyKind kind, double spec, params (int Song, double Strength, string? Variant)[] members)
    {
        var f = new Family { Id = id, Key = "t:" + id, Kind = kind, Label = "family " + id, Specificity = spec, Channel = "chord", Channels = "chord" };
        foreach (var (s, st, v) in members.OrderBy(x => x.Song)) f.Members.Add(new Member { Song = s, Strength = st, First = 16, FirstEnd = 48, Variant = v });
        return f;
    }

    public static Song[] Plain(int n, int firstYear = 1950) => Enumerable.Range(0, n).Select(i => Song(i, firstYear + i, endBeat: 400)).ToArray();

    /// <summary>The label a loop family of these tokens (played in this order) gets.</summary>
    public static string NameOf(int[] played, bool minor, int minorTonic = 9)
    {
        var roots = played.Select(Roman.Root).ToArray();
        int k = Features.Booth(roots);
        var canon = Roman.Rotate(played, k);
        return Catalogue.Name(canon, (played.Length - k) % played.Length, minor, minorTonic).Label;
    }
}

/// <summary>
/// Identity lineages (DESIGN.md §8b): the roman numerals and the schema catalogue, loop keys, the bar grid and the
/// progression schemas on synthetic grids, the loop check against the chord changes, families, the score, credit and
/// the user's parent rule, excerpts, the degenerate-structure guard, and the run / export / retree round trip.
/// </summary>
public class LineageTests
{
    private static readonly LineageParams Lp = new();

    // ------------------------------------------------------------------------------------------------ roman numerals, catalogue
    [Fact]
    public void RomanNumeralsInTheCAndTonicFrames()
    {
        Assert.Equal("I", Roman.Of(Lin.C));
        Assert.Equal("vi", Roman.Of(Lin.Am));
        Assert.Equal("viio", Roman.Of(Lin.Bdim));
        Assert.Equal("bVII", Roman.Of(Lin.Bb));
        Assert.Equal("i", Roman.Of(Lin.Am, 9));
        Assert.Equal("bVI", Roman.Of(Lin.F, 9));
        Assert.Equal("bIII", Roman.Of(Lin.C, 9));
        Assert.Equal("V", Roman.Of(Lin.E, 9));
        Assert.Equal("7", Roman.Degree(8, 9));
        Assert.Equal("b7", Roman.Degree(7, 9));
        Assert.Equal("I-V-vi-IV", Roman.Seq([Lin.C, Lin.G, Lin.Am, Lin.F]));
    }

    [Fact]
    public void CatalogueNamesEveryListedSchema()
    {
        // Axis progression: every rotation is one family; the label shows the members' rotation, the tonic frame in minor.
        Assert.Equal("axis progression I-V-vi-IV", Lin.NameOf([Lin.C, Lin.G, Lin.Am, Lin.F], false));
        Assert.Equal("axis progression vi-IV-I-V", Lin.NameOf([Lin.Am, Lin.F, Lin.C, Lin.G], false));
        Assert.Equal("axis progression i-bVI-bIII-bVII", Lin.NameOf([Lin.Am, Lin.F, Lin.C, Lin.G], true));
        Assert.Equal("axis progression i-bVI-bIII-bVII", Lin.NameOf([Lin.C, Lin.G, Lin.Am, Lin.F], true));
        // Fixed schemas show their own rotation whatever the members start on.
        Assert.Equal("doo-wop progression I-vi-IV-V", Lin.NameOf([Lin.C, Lin.Am, Lin.F, Lin.G], false));
        Assert.Equal("doo-wop progression I-vi-IV-V", Lin.NameOf([Lin.F, Lin.G, Lin.C, Lin.Am], false));
        Assert.Equal("Pachelbel ground I-V-vi-iii-IV-I-IV-V", Lin.NameOf([Lin.C, Lin.G, Lin.Am, Lin.Em, Lin.F, Lin.C, Lin.F, Lin.G], false));
        // One chord quality may differ in a cycle of 4+ (E vs Em); the label shows what the members play.
        Assert.Equal("Pachelbel ground I-V-vi-III-IV-I-IV-V", Lin.NameOf([Lin.C, Lin.G, Lin.Am, Lin.E, Lin.F, Lin.C, Lin.F, Lin.G], false));
        Assert.Equal("Pachelbel ground (first half) I-V-vi-iii", Lin.NameOf([Lin.C, Lin.G, Lin.Am, Lin.Em], false));
        Assert.Equal("Andalusian cadence i-bVII-bVI-V", Lin.NameOf([Lin.Am, Lin.G, Lin.F, Lin.E], true));
        Assert.Equal("Andalusian cadence i-bVII-bVI-v", Lin.NameOf([Lin.Am, Lin.G, Lin.F, Lin.Em], true));
        Assert.Equal("Mixolydian vamp I-bVII-IV", Lin.NameOf([Lin.C, Lin.Bb, Lin.F], false));
        Assert.Equal("bVII-bVI shuttle I-bVII-bVI-bVII", Lin.NameOf([Lin.C, Lin.Bb, Lin.Ab, Lin.Bb], false));
        Assert.Equal("bVII-bVI shuttle i-bVII-bVI-bVII", Lin.NameOf([Lin.Am, Lin.G, Lin.F, Lin.G], true));
        Assert.Equal("Aeolian progression i-bVI-bVII", Lin.NameOf([Lin.Am, Lin.F, Lin.G], true));
        Assert.Equal("I-vi-ii-V turnaround", Lin.NameOf([Lin.C, Lin.Am, Lin.Dm, Lin.G], false));
        Assert.Equal("ii-V-I cadence loop", Lin.NameOf([Lin.Dm, Lin.G, Lin.C], false));
        Assert.Equal("two-chord vamp I-IV", Lin.NameOf([Lin.C, Lin.F], false));
        Assert.Equal("two-chord vamp i-iv", Lin.NameOf([Lin.Am, Lin.Dm], true));
        // A three-chord schema needs every quality; anything else is named by its numerals (tonic frame in minor).
        Assert.Equal("I-bvii-IV", Lin.NameOf([Lin.C, Lin.Bbm, Lin.F], false));
        Assert.Equal("I-III-IV", Lin.NameOf([Lin.C, Lin.E, Lin.F], false));
        Assert.Equal("i-iv-V", Lin.NameOf([Lin.Am, Lin.Dm, Lin.E], true));
        // Parallel normalization: the minor tonic is C.
        Assert.Equal("axis progression i-bVI-bIII-bVII", Lin.NameOf([Lin.Cm, Lin.Ab, Lin.Eb, Lin.Bb], true, minorTonic: 0));
    }

    [Fact]
    public void LoopKeyIsRotationInvariantByRootsWithPhaseAndRhythm()
    {
        var k1 = Families.LoopKey(Lin.Loop([Lin.Am, Lin.F, Lin.C, Lin.G], [3, 2, 3, 2], 16, 2, 0))!.Value;
        var k2 = Families.LoopKey(Lin.Loop([Lin.C, Lin.G, Lin.Am, Lin.F], [3, 2, 3, 2], 16, 2, 0))!.Value;
        Assert.Equal("loop:0.7.9.5", k1.Key);
        Assert.Equal(k1.Key, k2.Key);
        Assert.Equal(2, k1.Var.Phase);
        Assert.Equal(0, k2.Var.Phase);
        Assert.Equal([Lin.C, Lin.G, Lin.Am, Lin.F], k1.Var.Tokens);
        Assert.Equal([3, 2, 3, 2], k1.Var.Rhythm);
        // C-Cm-F is I-IV (a repeated root is one chord); a repeated half cycle is its primitive period; one root is no loop.
        Assert.Equal("loop:0.5", Families.LoopKey(Lin.Loop([Lin.C, Lin.Cm, Lin.F], [2, 2, 3], 8, 2, 0))!.Value.Key);
        Assert.Equal("loop:0.7", Families.LoopKey(Lin.Loop([Lin.C, Lin.G, Lin.C, Lin.G], [3, 3, 3, 3], 16, 2, 0))!.Value.Key);
        Assert.Null(Families.LoopKey(Lin.Loop([Lin.C, Lin.Cm], [3, 3], 8, 2, 0)));
    }

    // ------------------------------------------------------------------------------------------------ grid and progression schemas
    [Fact]
    public void ChordGridTakesTheLongestChordOfEachHalfBar()
    {
        var s = Lin.Song(0, 1960, [(Lin.C, 3), (Lin.F, 1), (Lin.G, 2), (Lin.Am, 2), (Lin.C, 0.5)]);
        var g = ChordGrid.Of(s.Chords, 0, 4);
        Assert.Equal(2, g.CellsPerBar);
        Assert.Equal([Lin.C, Lin.C, Lin.G, Lin.Am, Lin.C], g.Cells);
        var w = ChordGrid.Of(s.Chords, 0, 3);
        Assert.Equal(1, w.CellsPerBar);
        Assert.Equal(Lin.C, w.At(0, 0));
    }

    /// <summary>Bars of the 12-bar blues on <paramref name="tonic"/> (major chords; V as the dim triad a third above when asked).</summary>
    private static IEnumerable<(int, double)> Blues12(int tonic, bool quickChange = false, bool dimV = false)
    {
        int[] roots = [0, quickChange ? 5 : 0, 0, 0, 5, 5, 0, 0, 7, 5, 0, 7];
        foreach (int r in roots)
            yield return (r == 7 && dimV ? Roman.Token(tonic + 7 + 4, 2) : Roman.Token(tonic + r, 0), 4);
    }

    [Fact]
    public void TwelveBarBluesIsFoundInAnyKeyWithItsVariants()
    {
        var s = Lin.Song(0, 1960, Blues12(5, quickChange: true, dimV: true).Concat(Blues12(5)));
        var occ = Progressions.Blues(ChordGrid.Of(s.Chords, 0, 4), Lp);
        Assert.Equal(2, occ.Count);
        Assert.All(occ, o => Assert.Equal(Progressions.Blues12.Key, o.Key));
        Assert.Equal((0.0, 48.0, "12-bar"), (occ[0].Start, occ[0].End, occ[0].Variant));
        // Four more bars of the tonic before it: the 16-bar variant.
        var s16 = Lin.Song(1, 1960, Lin.Rep(4, (Lin.C, 4)).Concat(Blues12(0)));
        var o16 = Progressions.Blues(ChordGrid.Of(s16.Chords, 0, 4), Lp);
        Assert.Equal((0.0, 64.0, "16-bar"), (o16.Single().Start, o16.Single().End, o16.Single().Variant));
        // Not a blues: an axis loop, or the first 8 bars of the form only.
        Assert.Empty(Progressions.Blues(ChordGrid.Of(Lin.Song(2, 1960, Lin.Rep(6, (Lin.C, 4), (Lin.G, 4), (Lin.Am, 4), (Lin.F, 4))).Chords, 0, 4), Lp));
        Assert.Empty(Progressions.Blues(ChordGrid.Of(Lin.Song(3, 1960, Blues12(0).Take(8)).Chords, 0, 4), Lp));
    }

    [Fact]
    public void EightBarBluesNeedsTwoOccurrences()
    {
        (int, double)[] form = [(Lin.C, 4), (Lin.G, 4), (Lin.F, 4), (Lin.F, 4), (Lin.C, 4), (Lin.G, 4), (Lin.C, 4), (Lin.G, 4)];
        Assert.Empty(Progressions.Blues(ChordGrid.Of(Lin.Song(0, 1960, form).Chords, 0, 4), Lp));
        var two = Progressions.Blues(ChordGrid.Of(Lin.Song(1, 1960, form.Concat(form)).Chords, 0, 4), Lp);
        Assert.Equal(2, two.Count);
        Assert.All(two, o => Assert.Equal(Progressions.Blues8A.Key, o.Key));
    }

    [Fact]
    public void CadencesAreConsecutiveChangesResolvingOnABarLineOutsideLoops()
    {
        static double None(double a, double b) => 0;
        // Dm G | C C : a ii-V-I resolving on bar 2; a 1-beat B dim after G is the dominant (G7) and merges with it.
        var s = Lin.Song(0, 1960, [(Lin.Dm, 2), (Lin.G, 1), (Lin.Bdim, 1), (Lin.C, 4), (Lin.Am, 4), (Lin.Dm, 2), (Lin.G, 2), (Lin.C, 4)]);
        var occ = Progressions.CadenceOccurrences(s.Chords, 0, 4, 9, None);
        var ii = occ.Where(o => o.Key == "prog:ii-V-I").ToList();
        Assert.Equal(2, ii.Count);
        Assert.Equal((0.0, 8.0), (ii[0].Start, ii[0].End));
        Assert.Equal("2.2.4", ii[0].Variant);
        // Resolving in the middle of a bar: not a cadence.
        var mid = Lin.Song(1, 1960, [(Lin.C, 1), (Lin.Dm, 2), (Lin.G, 3), (Lin.C, 2)]);
        Assert.DoesNotContain(Progressions.CadenceOccurrences(mid.Chords, 0, 4, 9, None), o => o.Key == "prog:ii-V-I");
        // A passing chord between V and I breaks it (V-IV-I is another cadence).
        var pass = Lin.Song(2, 1960, [(Lin.Dm, 2), (Lin.G, 1), (Lin.F, 1), (Lin.C, 4)]);
        Assert.DoesNotContain(Progressions.CadenceOccurrences(pass.Chords, 0, 4, 9, None), o => o.Key == "prog:ii-V-I");
        // Inside a loop visit: skipped.
        Assert.Empty(Progressions.CadenceOccurrences(s.Chords, 0, 4, 9, (a, b) => b - a));
        // With L2 chords the variant carries the qualities with sevenths.
        var l2 = new ChordLine { Tokens = [2 * 6 + 4, 7 * 6 + 2, 0 * 6 + 3], Starts = [0, 2, 4], Durs = [2, 2, 4], Downbeat = [0, 0, 0] };
        var v = Progressions.CadenceOccurrences(Lin.Song(3, 1960, [(Lin.Dm, 2), (Lin.G, 2), (Lin.C, 4)]).Chords, 0, 4, 9, None, l2);
        Assert.Equal("2.2.4|m7.7.maj7", v.Single(o => o.Key == "prog:ii-V-I").Variant);
        // I-IV-V-I; minor forms are strict: iio (dim) - V (major) - i.
        Assert.Contains(Progressions.CadenceOccurrences(Lin.Song(4, 1960, [(Lin.C, 4), (Lin.F, 2), (Lin.G, 2), (Lin.C, 4)]).Chords, 0, 4, 9, None),
            o => o.Key == "prog:I-IV-V-I");
        Assert.Contains(Progressions.CadenceOccurrences(Lin.Song(5, 1960, [(Lin.Bdim, 2), (Lin.E, 2), (Lin.Am, 4)]).Chords, 0, 4, 9, None),
            o => o.Key == "prog:iio-V-i");
        Assert.DoesNotContain(Progressions.CadenceOccurrences(Lin.Song(6, 1960, [(Lin.Bm, 2), (Lin.Em, 2), (Lin.Am, 4)]).Chords, 0, 4, 9, None),
            o => o.Key == "prog:iio-V-i");
    }

    [Fact]
    public void DescendingChromaticBassIsKeyedByItsDegreesInTheTonicFrame()
    {
        // A G# G F# F, a half-bar each (the line cliche of a minor song).
        var bass = Lin.Line((0, 45), (2, 44), (4, 43), (6, 42), (8, 41), (10, 38));
        var minor = Progressions.ChromaticBass(bass, 0, 4, 9, Lp);
        Assert.Equal("dcb:1-7-b7-6", minor.Single().Key);
        Assert.Equal((0.0, 10.0), (minor[0].Start, minor[0].End));
        Assert.Equal("dcb:6-b6-5-#4", Progressions.ChromaticBass(bass, 0, 4, 0, Lp).Single().Key);
        Assert.Equal("descending chromatic bass 1-7-b7-6", Progressions.ChromaticBassSchema("dcb:1-7-b7-6").Label);
        // A fast chromatic run is a fill, not a bass line: sampled every half-bar it is not stepwise.
        var run = Lin.Line(Enumerable.Range(0, 16).Select(i => (i * 0.5, 50 - i)).ToArray());
        Assert.Empty(Progressions.ChromaticBass(run, 0, 4, 0, Lp));
        // Three steps are the minimum (4 notes).
        Assert.Empty(Progressions.ChromaticBass(Lin.Line((0, 45), (2, 44), (4, 43), (6, 38), (8, 36)), 0, 4, 9, Lp));
    }

    [Fact]
    public void CycleMatchHearsTheLoopOnlyWhenItSounds()
    {
        int[] vamp = [Lin.C, Lin.G];
        Assert.Equal(0, CycleMatch.Find(Lin.Song(0, 1960, Lin.Rep(2, (Lin.C, 4), (Lin.G, 4))).Chords, 0, 64, vamp, 4));
        // A vamp alternates twice; once there and back is not enough; a one-beat pickup is not a vamp.
        Assert.Null(CycleMatch.Find(Lin.Song(1, 1960, [(Lin.C, 4), (Lin.G, 4), (Lin.C, 4), (Lin.F, 4)]).Chords, 0, 64, vamp, 4));
        Assert.Null(CycleMatch.Find(Lin.Song(2, 1960, Lin.Rep(3, (Lin.C, 7), (Lin.G, 1))).Chords, 0, 64, vamp, 4));
        // Qualities: exact for 2-3 chords, one may differ in a longer cycle, which may also skip one short ornament chord.
        Assert.Null(CycleMatch.Find(Lin.Song(3, 1960, Lin.Rep(2, (Lin.C, 4), (Lin.Gm, 4))).Chords, 0, 64, vamp, 4));
        int[] axis = [Lin.C, Lin.G, Lin.Am, Lin.F];
        Assert.Equal(4, CycleMatch.Find(Lin.Song(4, 1960, [(Lin.Dm, 4), (Lin.C, 4), (Lin.G, 4), (Lin.A, 4), (Lin.F, 4)]).Chords, 0, 64, axis, 4));
        Assert.NotNull(CycleMatch.Find(Lin.Song(5, 1960, [(Lin.C, 4), (Lin.G, 2), (Lin.Em, 2), (Lin.Am, 4), (Lin.F, 4)]).Chords, 0, 64, axis, 4));
        Assert.Null(CycleMatch.Find(Lin.Song(6, 1960, [(Lin.C, 4), (Lin.Gm, 4), (Lin.A, 4), (Lin.F, 4)]).Chords, 0, 64, axis, 4));
        // Only inside the span.
        Assert.Null(CycleMatch.Find(Lin.Song(7, 1960, Lin.Rep(2, (Lin.C, 4), (Lin.G, 4))).Chords, 0, 12, vamp, 4));
    }

    // ------------------------------------------------------------------------------------------------ families
    [Fact]
    public void LoopFamiliesNeedRepeatingLoopsThatSoundAndCoverEightBars()
    {
        var chords = Lin.Rep(8, (Lin.C, 4), (Lin.G, 4)).ToArray();   // bars 1-16: C G
        var ok = Lin.Loop([Lin.C, Lin.G], [3, 3], 8, 4, 0);          // 32 beats = 8 bars
        var songs = new[]
        {
            Lin.Song(0, 1960, chords, loops: [ok]),
            Lin.Song(1, 1961, chords, loops: [Lin.Loop([Lin.C, Lin.G], [3, 3], 8, 1, 0, 16, 32, 48)]),   // passes 1: a phrase summary
            Lin.Song(2, 1962, Lin.Rep(8, (Lin.C, 4), (Lin.F, 4)), loops: [ok]),                        // the chords do not play it
            Lin.Song(3, 1963, chords, loops: [Lin.Loop([Lin.C, Lin.G], [3, 3], 8, 3, 0)]),             // 24 beats = 6 bars
            Lin.Song(4, 1964, [(Lin.Am, 8), .. chords], loops: [Lin.Loop([Lin.C, Lin.G], [3, 3], 8, 4, 4)]),
        };
        var fams = Families.Build(songs, [], Lp);
        var f = fams.Single(x => x.Key.StartsWith("loop:", StringComparison.Ordinal));
        Assert.Equal([0, 4], f.Members.Select(m => m.Song));
        Assert.Equal("two-chord vamp I-V", f.Label);
        Assert.Equal(FamilyKind.Schema, f.Kind);
        Assert.Equal("I-V", f.Roman);
        // The first visit is where the cycle is heard (song 4's visit starts inside the Am bar: heard from beat 8).
        Assert.Equal(8, f.Of(4)!.First);
        Assert.Equal(Math.Log2(5 / 2.0), f.Specificity, 9);
        Assert.Equal(32 / Families.SongBeats(songs[0]), f.Of(0)!.Strength, 9);
    }

    [Fact]
    public void ShortCyclesAreExactLongCyclesTolerateOneChordQuality()
    {
        Song S(int i, int[] cyc) => Lin.Song(i, 1960 + i, Lin.Rep(4, cyc.Select(t => (t, 4.0)).ToArray()),
            loops: [Lin.Loop(cyc, cyc.Select(_ => 3).ToArray(), 4 * cyc.Length, 4, 0)]);
        var songs = new[]
        {
            S(0, [Lin.C, Lin.Am]), S(1, [Lin.C, Lin.A]),                                       // I-vi and I-VI: two families
            S(2, [Lin.C, Lin.G, Lin.Am, Lin.F]), S(3, [Lin.C, Lin.G, Lin.A, Lin.F]),           // axis, one quality off: one family
            S(4, [Lin.C, Lin.G, Lin.Am, Lin.F]), S(5, [Lin.C, Lin.Gm, Lin.A, Lin.F]),          // two off: its own family
            S(6, [Lin.Am, Lin.F, Lin.C, Lin.G]),                                                // another rotation of the axis
        };
        var fams = Families.Build(songs, [], Lp).Where(f => f.Kind != FamilyKind.Strong).ToList();
        Assert.Equal([0], fams.Single(f => f.Label == "two-chord vamp I-vi").Members.Select(m => m.Song));
        Assert.Equal([1], fams.Single(f => f.Label == "two-chord vamp I-VI").Members.Select(m => m.Song));
        var axis = fams.Single(f => f.Label == "axis progression I-V-vi-IV");
        Assert.Equal([2, 3, 4, 6], axis.Members.Select(m => m.Song));
        Assert.Equal(2, axis.Of(6)!.Vars[0].Phase);
        Assert.Equal("loop:0.7.9.5", axis.Key);
        Assert.Single(fams, f => f.Members.Select(m => m.Song).SequenceEqual([5]));
        // Labels are unique (the viewer finds an edge's family by its label).
        Assert.Equal(fams.Count, fams.Select(f => f.Label).Distinct().Count());
    }

    [Fact]
    public void StrongMatchesAreTwoSongFamiliesNamedByTheirChannel()
    {
        var songs = Lin.Plain(3);
        var pr = new PairResult { A = 0, B = 2, Zc = 41.5, Q = 1e-5, WinStart = 64, WinEnd = 128 };
        pr.Counting[(int)Channel.Melody] = true;
        pr.Avail[(int)Channel.Melody] = true;
        pr.Z[(int)Channel.Melody] = 40;
        pr.Segments.Add(new Segment { Channel = Channel.Melody, AStart = 10, AEnd = 42, BStart = 64, BEnd = 128, Bits = 50, N = 13 });
        // Without a measured passage: the display segment (window) and its note count.
        var plain = Families.Build(songs, [pr], Lp).Single();
        Assert.Equal(FamilyKind.Strong, plain.Kind);
        Assert.Equal("exact melody passage, 13 notes", plain.Label);
        Assert.Equal((10.0, 42.0), (plain.Of(0)!.First, plain.Of(0)!.FirstEnd));
        // With the passage measured from the shared n-grams (StrongSpans): its spans, length and channel.
        var fams = Families.Build(songs, [pr], Lp, strongSpans: new Dictionary<(int, int), StrongSpan> { [(0, 2)] = new(12, 30, 70, 90, 9, "bass") });
        var f = fams.Single();
        Assert.Equal("exact bass riff, 9 notes", f.Label);
        Assert.Equal("bass", f.Channel);
        Assert.Equal("melody,bass", f.Channels);      // the counting channels and always the passage's own
        Assert.Equal(41.5, f.Z);
        Assert.Equal((12.0, 30.0), (f.Of(0)!.First, f.Of(0)!.FirstEnd));
        Assert.Equal((70.0, 90.0), (f.Of(2)!.First, f.Of(2)!.FirstEnd));
        Assert.Equal(Math.Log2(3 / 2.0), f.Specificity, 9);
    }

    [Fact]
    public void StrongSpansFindTheExactPassageInBothSongs()
    {
        // Song 20 quotes song 5's melody (beats 64-96) at beats 128-160 (the planted borrowing of DecisionTests).
        var mats = DecisionTests.Corpus(24, 101);
        DecisionTests.Put(mats[20].Mel, 128, 160, DecisionTests.Slice(mats[5].Mel, 64, 96));
        var songs = mats.Select((m, i) => DecisionTests.ToSong(i, m)).ToArray();
        var p = DecisionTests.V2();
        var e = V8Engine.Build(songs, p);
        var details = new PairDetails(e, p);
        var pr = details.From(e.ScoreB(20, e.NewWorker(), 5)[5], 5, 20);
        var sp = Families.StrongSpans(e, pr)!.Value;
        Assert.Equal("melody", sp.Channel);
        Assert.True(sp.B0 >= 128 - 1e-6 && sp.B1 < 160 + 1e-6, $"B {sp.B0}-{sp.B1}");
        Assert.True(sp.A0 >= 64 - 1e-6 && sp.A1 < 96 + 1e-6, $"A {sp.A0}-{sp.A1}");
        int quoted = songs[20].Melody!.Onsets.Count(t => t >= 128 && t < 160);
        Assert.True(sp.N >= quoted / 2 && sp.N <= quoted, $"{sp.N} of {quoted} quoted notes");
    }

    [Fact]
    public void ThreeChordLoopsHaveNoPassingChord()
    {
        int[] cyc = [Lin.Dm, Lin.G, Lin.C];
        Assert.NotNull(CycleMatch.Find(Lin.Song(0, 1960, Lin.Rep(2, (Lin.Dm, 4), (Lin.G, 2), (Lin.C, 2))).Chords, 0, 64, cyc, 4));
        Assert.Null(CycleMatch.Find(Lin.Song(1, 1960, Lin.Rep(2, (Lin.Dm, 4), (Lin.G, 3), (Lin.C, 0.75))).Chords, 0, 64, cyc, 4));
    }

    // ------------------------------------------------------------------------------------------------ score, credit, tree
    [Fact]
    public void ScoreIsSpecificityTimesAgreementTimesStrength()
    {
        var songs = Lin.Plain(3);
        var design = new LineageParams { FineAgreement = 0 };
        var f = Lin.Fam(1, FamilyKind.Progression, 3.0, (0, 0.36, "a"), (1, 0.16, "a"), (2, 0.25, "b"));
        var sc = LineageTree.Scores(songs, [f], design, TreeRule.Final);
        Assert.Equal(3.0 * 1.0 * Math.Sqrt(0.16), sc[1][0].Score, 9);           // same variant
        Assert.Equal(3.0 * 0.75 * Math.Sqrt(0.25), sc[2][0].Score, 9);          // other variant (progressions: 0.75)
        // The finer agreement multiplies in the closeness (for a progression, the strength ratio), squared by default.
        var fine = LineageTree.Scores(songs, [f], Lp, TreeRule.Final);
        Assert.Equal(3.0 * 1.0 * Math.Pow(0.16 / 0.36, 2) * Math.Sqrt(0.16), fine[1][0].Score, 9);
        var linear = LineageTree.Scores(songs, [f], new LineageParams { ClosenessPower = 1 }, TreeRule.Final);
        Assert.Equal(3.0 * 1.0 * (0.16 / 0.36) * Math.Sqrt(0.16), linear[1][0].Score, 9);
        // A strong match adds StrongWeight x z.
        var s = Lin.Fam(2, FamilyKind.Strong, 1.0, (0, 0.25, null), (2, 0.25, null));
        s.Z = 35;
        var st = LineageTree.Scores(songs, [f, s], Lp, TreeRule.Final);
        Assert.Equal(fine[2][0].Score + 1.0 * 0.5 + Lp.StrongWeight * 35, st[2][0].Score, 9);
        Assert.Same(s, st[2][0].Top);
    }

    [Fact]
    public void LoopAgreementUsesPhaseAndRhythm()
    {
        var songs = Lin.Plain(4);
        LoopVar V(int phase, int[] rhythm) => new() { Phase = phase, Tokens = [Lin.C, Lin.G, Lin.Am, Lin.F], Rhythm = rhythm, LoopBeats = 16, Coverage = 64 };
        var f = new Family { Id = 1, Kind = FamilyKind.Loop, Specificity = 2 };
        foreach (var (song, v) in new[] { (0, V(0, [3, 3, 3, 3])), (1, V(0, [3, 3, 3, 3])), (2, V(0, [2, 2, 3, 3])), (3, V(2, [3, 3, 3, 3])) })
        {
            var m = new Member { Song = song, Strength = 0.5 };
            m.Vars.Add(v);
            f.Members.Add(m);
        }
        var design = new LineageParams { FineAgreement = 0 };
        var sc = LineageTree.Scores(songs, [f], design, TreeRule.Final);
        double b = 2 * Math.Sqrt(0.5);
        Assert.Equal(b * 1.0, sc[1][0].Score, 9);    // same phase and rhythm
        Assert.Equal(b * 0.75, sc[2][0].Score, 9);   // same phase
        Assert.Equal(b * 0.5, sc[3][0].Score, 9);    // other phase
        Assert.Equal(1.0, sc[1][0].Close, 9);        // identical version
        Assert.True(sc[2][0].Close < 1);
    }

    [Fact]
    public void EdgesRunStrictlyEarlierToLaterAndRootsHaveNoEarlierMember()
    {
        // Songs 0 and 1 share a year (no month): no edge between them; song 2 is later.
        var songs = new[] { Lin.Song(0, 1960, endBeat: 400), Lin.Song(1, 1960, endBeat: 400), Lin.Song(2, 1961, endBeat: 400), Lin.Song(3, 1962, endBeat: 400) };
        var f = Lin.Fam(1, FamilyKind.Progression, 2, (0, 0.3, null), (1, 0.3, null), (2, 0.3, null));
        var r = LineageTree.Build(songs, [f], new Params(), Lp);
        Assert.Equal(-1, r.Parent[0]);
        Assert.Equal(-1, r.Parent[1]);
        Assert.Equal(-1, r.Parent[3]);              // no family: a root
        Assert.Equal(0, r.Parent[2]);               // tie between 0 and 1: the earliest
        Assert.All(r.Edges, e => Assert.True(e.Pair.A < e.Pair.B && DateOrder.Earlier(songs[e.Pair.A].Date, songs[e.Pair.B].Date)));
        Assert.DoesNotContain(r.Edges, e => e.Pair.A == 0 && e.Pair.B == 1);
    }

    [Fact]
    public void ParentIsTheMostReferencedStrongInfluencer()
    {
        // Y (0) is credited by C1 (1) and C2 (2); X (3) shares a rarer family with B (4) and scores highest for B,
        // but Y is a strong influencer of B (score >= 0.5 max) and the most referenced one: the parent.
        var songs = Lin.Plain(5);
        var shared = Lin.Fam(1, FamilyKind.Progression, 1.0, (0, 0.25, null), (1, 0.25, null), (2, 0.25, null), (4, 0.25, null));
        var rare = Lin.Fam(2, FamilyKind.Progression, 1.8, (3, 0.25, null), (4, 0.25, null));
        var r = LineageTree.Build(songs, [shared, rare], new Params(), Lp);
        Assert.Equal(2, r.RefCount[0]);
        Assert.Equal(3, r.Credit[4]);                // B credits its highest-scoring earlier song
        Assert.Equal(0, r.Parent[4]);                // but its parent is the most referenced strong influencer
        var tree = r.Edges.Single(e => e.Kind == "tree" && e.Pair.B == 4);
        Assert.Equal("family 1", tree.Evidence);
        Assert.Contains(r.Edges, e => e.Kind == "secondary" && e.Pair.A == 3 && e.Pair.B == 4);
        // With ParentFraction 0.6 Y is no longer a strong influencer.
        var r2 = LineageTree.Build(songs, [shared, rare], new Params { ParentFraction = 0.6 }, Lp);
        Assert.Equal(3, r2.Parent[4]);
    }

    [Fact]
    public void CreditTiesGoToTheClosestVersionThenTheEarliest()
    {
        var songs = Lin.Plain(4);
        // Songs 0 and 1 hold the family at strength 0.2 and 0.4; song 3 at 0.4. Design score: sqrt(min) ties (0.2 vs 0.4 ->
        // not a tie); make song 2 a second 0.4 version so 1 and 2 tie exactly.
        var f = Lin.Fam(1, FamilyKind.Progression, 2.0, (0, 0.2, null), (1, 0.4, null), (2, 0.4, null), (3, 0.3, null));
        var design = new LineageParams { FineAgreement = 0 };
        // Plain rule (no closeness): exact ties between 1 and 2 (both >= 0.3) go to the earliest.
        var plain = LineageTree.Build(songs, [f], new Params(), design, new TreeRule(true, true, false, false));
        Assert.Equal(1, plain.Credit[3]);
        // All three earlier songs: 0 scores sqrt(0.2) < sqrt(0.3) for 1 and 2 -> 1 (earliest of the tie).
        var close = LineageTree.Build(songs, [f], new Params(), design, new TreeRule(true, true, true, false));
        Assert.Equal(1, close.Credit[3]);            // 1 and 2 are equally close (strength ratio 0.75): the earliest
        // A closer version wins the tie.
        var g = Lin.Fam(1, FamilyKind.Progression, 2.0, (0, 0.2, null), (1, 0.6, null), (2, 0.35, null), (3, 0.3, null));
        Assert.Equal(2, LineageTree.Build(songs, [g], new Params(), design, new TreeRule(true, true, true, false)).Credit[3]);
        Assert.Equal(1, LineageTree.Build(songs, [g], new Params(), design, new TreeRule(true, true, false, false)).Credit[3]);
    }

    [Fact]
    public void SecondaryEdgesAreCappedAndStrongMatchesDominate()
    {
        var songs = Lin.Plain(12);
        var big = Lin.Fam(1, FamilyKind.Progression, 1.0, Enumerable.Range(0, 12).Select(i => (i, 0.3, (string?)null)).ToArray());
        var strong = Lin.Fam(2, FamilyKind.Strong, 2.5, (4, 0.1, null), (11, 0.1, null));
        strong.Z = 31;
        var r = LineageTree.Build(songs, [big, strong], new Params(), Lp);
        Assert.Equal(4, r.Parent[11]);               // the strong match dominates the score: the only strong influencer
        Assert.Equal("family 2", r.Edges.Single(e => e.Kind == "tree" && e.Pair.B == 11).Evidence);
        var r2 = LineageTree.Build(songs, [big], new Params { MaxSecondary = 3 }, Lp);
        Assert.All(Enumerable.Range(0, 12), b => Assert.True(r2.Edges.Count(e => e.Kind == "secondary" && e.Pair.B == b) <= 3));
        Assert.Equal(3, r2.Edges.Count(e => e.Kind == "secondary" && e.Pair.B == 11));
    }

    [Fact]
    public void ExcerptsAreTheCreditedFamilysFirstVisitSnappedToBars()
    {
        var songs = Lin.Plain(3);
        var f = Lin.Fam(1, FamilyKind.Progression, 2.0, (0, 0.3, null), (1, 0.3, null));
        f.Members[0].First = 17;
        f.Members[0].FirstEnd = 30;
        f.Members[1].First = 130;
        f.Members[1].FirstEnd = 250;
        var r = LineageTree.Build(songs, [f], new Params(), Lp);
        var e = r.Edges.Single();
        Assert.Equal((16.0, 48.0), (e.AStart, e.AEnd));        // bar 5 onwards, 8 bars minimum
        Assert.Equal((128.0, 224.0), (e.BStart, e.BEnd));      // 24 bars maximum
        Assert.Equal((e.BStart, e.BEnd), r.Excerpt[1]);        // a child's excerpt is its tree edge's
        Assert.Equal((e.AStart, e.AEnd), r.Excerpt[0]);        // a root's: the family its children share
        Assert.StartsWith("children", r.ExcerptSource[0], StringComparison.Ordinal);
        Assert.Equal("first bars", r.ExcerptSource[2]);
    }

    [Fact]
    public void FinerAgreementTurnsAOneFamilyStarIntoATreeOfVersions()
    {
        // One common family of 40 songs whose versions differ in strength and variant.
        var rng = new Rng(17);
        var members = Enumerable.Range(0, 40).Select(i => (i, 0.05 + 0.45 * rng.NextDouble(), (string?)("v" + rng.Below(3)))).ToArray();
        var songs = Lin.Plain(40);
        var f = Lin.Fam(1, FamilyKind.Progression, 1.5, members);
        LineageResult R(TreeRule rule, LineageParams? lp = null) => LineageTree.Build(songs, [f], new Params(), lp ?? Lp, rule);
        var plain = R(new TreeRule(false, false, false, false));
        var design = R(new TreeRule(true, true, true, false));
        var final = R(TreeRule.Final);
        Assert.Equal(39, plain.Children.Max());      // membership only: every later song under the first
        Assert.Equal(1, plain.Children.Count(c => c > 0));
        Assert.Equal(1, plain.Depth.Max());
        // Closeness in the score narrows each song's strong influencers to its close versions: several parents, depth.
        Assert.True(final.Children.Max() <= plain.Children.Max() / 2, $"final rule: {final.Children.Max()} children at most");
        Assert.True(final.Children.Max() < design.Children.Max(), $"design {design.Children.Max()}, final {final.Children.Max()}");
        Assert.True(final.Children.Count(c => c > 0) >= 5, "several versions are parents");
        Assert.True(final.Depth.Max() >= 3, "a tree of versions has depth");
        // Squared closeness (the default) is sharper than closeness alone.
        var p1 = R(TreeRule.Final, new LineageParams { ClosenessPower = 1 });
        Assert.True(final.Children.Max() <= p1.Children.Max());
    }

    // ------------------------------------------------------------------------------------------------ params
    [Fact]
    public void LineageParamsAreSeparateFromTheEvidenceParams()
    {
        var own = typeof(LineageParams).GetFields(BindingFlags.Public | BindingFlags.Instance).Select(f => f.Name.ToLowerInvariant()).ToHashSet();
        var ev = typeof(Params).GetFields(BindingFlags.Public | BindingFlags.Instance).Select(f => f.Name.ToLowerInvariant()).ToHashSet();
        Assert.Empty(own.Intersect(ev));             // --param routes by name; the evidence report lists only Params
        var lp = new LineageParams();
        Assert.True(LineageParams.Has("strongweight"));
        lp.Set("StrongWeight=12");
        lp.Set("MinPasses=1");
        Assert.Equal(12, lp.StrongWeight);
        Assert.Equal(1, lp.MinPasses);
        Assert.Throws<ArgumentException>(() => lp.Set("DfCap=3"));
        Assert.Equal(own.Count, lp.ToJson().Count);
    }
}
