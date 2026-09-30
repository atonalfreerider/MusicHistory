namespace MusicHistory.Influence;

/// <summary>
/// The bar-level chord grid of a song (DESIGN.md §8b progression schemas): bars from <c>first_downbeat</c> every
/// <c>beats_per_bar</c>, split into half-bar cells when a bar has at least 4 beats (else one cell per bar). A cell holds
/// the L1 chord (<c>chord_seq</c> kind 'chg', normalized key) that sounds longest in it, if that is at least a quarter
/// of the cell; otherwise -1.
/// </summary>
internal sealed class ChordGrid
{
    public double Fdb, Bpb, CellBeats;
    public int CellsPerBar = 1;
    public int[] Cells = [];

    public int Bars => (Cells.Length + CellsPerBar - 1) / CellsPerBar;
    public double CellStart(int i) => Fdb + i * CellBeats;
    public double BarStart(int bar) => Fdb + bar * Bpb;

    /// <summary>Cell <paramref name="half"/> of bar <paramref name="bar"/> (-1 outside the grid).</summary>
    public int At(int bar, int half)
    {
        int i = bar * CellsPerBar + half;
        return i >= 0 && i < Cells.Length && half < CellsPerBar ? Cells[i] : -1;
    }

    public static int CellsFor(double bpb) => bpb >= 4 - 1e-9 ? 2 : 1;

    public static ChordGrid Of(ChordLine? ch, double firstDownbeat, double beatsPerBar)
    {
        double bpb = beatsPerBar > 0 ? beatsPerBar : 4.0;
        var g = new ChordGrid { Fdb = firstDownbeat, Bpb = bpb, CellsPerBar = CellsFor(bpb) };
        g.CellBeats = bpb / g.CellsPerBar;
        if (ch == null || ch.Count == 0) return g;
        double end = 0;
        for (int i = 0; i < ch.Count; i++) end = Math.Max(end, ch.Starts[i] + ch.Durs[i]);
        int n = Math.Max(0, (int)Math.Ceiling((end - firstDownbeat) / g.CellBeats - 1e-9));
        g.Cells = new int[n];
        int j = 0;
        for (int c = 0; c < n; c++)
        {
            double t0 = g.CellStart(c), t1 = t0 + g.CellBeats;
            while (j < ch.Count && ch.Starts[j] + ch.Durs[j] <= t0 + 1e-9) j++;
            int best = -1;
            double bestOv = 0;
            for (int k = j; k < ch.Count && ch.Starts[k] < t1 - 1e-9; k++)
            {
                double ov = Math.Min(ch.Starts[k] + ch.Durs[k], t1) - Math.Max(ch.Starts[k], t0);
                if (ov > bestOv + 1e-9)
                {
                    bestOv = ov;
                    best = ch.Tokens[k];
                }
            }
            g.Cells[c] = bestOv >= 0.25 * g.CellBeats - 1e-9 ? best : -1;
        }
        return g;
    }
}

/// <summary>
/// Whether a chord cycle is heard in the chord changes (<c>chord_seq</c> 'chg' L1, the normalized key) of a span: the
/// cycle played through once in some rotation (a two-chord vamp: twice, 4 chords; a three-chord cycle: through and back
/// to its first chord, 4 chords), roots equal, chord
/// qualities equal except one of a cycle of 4 or more chords; consecutive chords on one root count as one; a cycle of 4
/// or more chords may skip one ornament chord of at most half a bar. Loop rows are checked with it before they join a
/// family, so a family is only claimed where its chords sound (the analyze stage's loop tokens can disagree with its
/// chord sequence, e.g. across a key-region boundary).
/// </summary>
internal static class CycleMatch
{
    public static List<(int Tok, double Start, double Dur)> Changes(ChordLine ch, double t0, double t1)
    {
        var l = new List<(int, double, double)>();
        for (int k = 0; k < ch.Count; k++)
        {
            double s = ch.Starts[k], e = s + ch.Durs[k];
            if (e <= t0 + 1e-6 || s >= t1 - 1e-6) continue;
            if (l.Count > 0 && Roman.Root(l[^1].Item1) == Roman.Root(ch.Tokens[k]))
            {
                l[^1] = (l[^1].Item1, l[^1].Item2, l[^1].Item3 + ch.Durs[k]);
                continue;
            }
            l.Add((ch.Tokens[k], s, ch.Durs[k]));
        }
        return l;
    }

    /// <summary>Beat where the cycle is first heard in [t0, t1), or null.</summary>
    public static double? Find(ChordLine? ch, double t0, double t1, IReadOnlyList<int> cycle, double bpb)
    {
        if (ch == null || cycle.Count < 2) return null;
        var c = Changes(ch, t0, t1);
        int n = cycle.Count;
        int need = n == 2 ? 4 : n == 3 ? 4 : n;
        int maxSkip = n >= 4 ? 1 : 0, maxMis = n >= 4 ? 1 : 0;
        for (int s = 0; s < c.Count; s++)
            for (int r = 0; r < n; r++)
            {
                if (Roman.Root(c[s].Tok) != Roman.Root(cycle[r])) continue;
                int j = s, skips = 0, mis = 0;
                double shortest = double.MaxValue, longest = 0;
                bool ok = true;
                for (int i = 0; i < need && ok; i++)
                {
                    int e = cycle[(r + i) % n];
                    while (j < c.Count && Roman.Root(c[j].Tok) != Roman.Root(e) && skips < maxSkip && i > 0 && c[j].Dur <= bpb / 2 + 1e-9)
                    {
                        j++;
                        skips++;
                    }
                    if (j >= c.Count || Roman.Root(c[j].Tok) != Roman.Root(e)) ok = false;
                    else
                    {
                        if (i < n && c[j].Tok != e) mis++;
                        shortest = Math.Min(shortest, c[j].Dur);
                        longest = Math.Max(longest, c[j].Dur);
                        j++;
                    }
                }
                // A two-chord vamp alternates: neither chord is a short pickup to the other (shortest >= longest / 3); in a
                // three-chord loop a chord under a quarter of the longest is a passing chord, not a member of the loop.
                if (ok && n == 2 && shortest < longest / 3 - 1e-9) ok = false;
                if (ok && n == 3 && shortest < longest / 4 - 1e-9) ok = false;
                if (ok && mis <= maxMis) return Math.Max(t0, c[s].Start);
            }
        return null;
    }
}

/// <summary>One occurrence of a progression schema in a song: family key, span in beats, variant.</summary>
internal readonly record struct Occurrence(string Key, double Start, double End, string? Variant = null);

/// <summary>A progression schema's family facts: key, label, roman form, channel.</summary>
internal sealed record ProgressionSchema(string Key, string Label, string? Roman, string Channel);

/// <summary>
/// Progression schemas on the bar-level chord grid (DESIGN.md §8b): the 12-bar blues (a form whose tonic section lasts
/// 8 bars is its 16-bar variant) and two 8-bar blues forms, the ii-V-I and I-IV-V-I cadences (and their minor forms)
/// outside loop visits, and the descending chromatic bass line (from the bass line). Chords are matched by root; a
/// diminished triad also stands for the dominant seventh a major third below it (G7 transcribed as B dim), the most
/// common L1 folding of the transcriptions.
/// </summary>
internal static class Progressions
{
    public static readonly ProgressionSchema Blues12 = new("prog:blues12", "12-bar blues", "I-I-I-I-IV-IV-I-I-V-IV-I-I", "chord");
    public static readonly ProgressionSchema Blues8A = new("prog:blues8a", "8-bar blues I-V-IV-IV-I-V-I-V", "I-V-IV-IV-I-V-I-V", "chord");
    public static readonly ProgressionSchema Blues8B = new("prog:blues8b", "8-bar blues I-I-I-I-IV-IV-V-I", "I-I-I-I-IV-IV-V-I", "chord");

    // Allowed roots (semitones above the form's tonic) per bar.
    private static readonly int[][] T12 = [[0], [0, 5], [0], [0], [5], [5], [0], [0], [7], [5, 7], [0], [0, 7]];
    private static readonly int[] A12 = [0, 4, 8, 10];
    private static readonly int[][] T8A = [[0], [7], [5], [5], [0], [7], [0], [7]];
    private static readonly int[][] T8B = [[0], [0], [0], [0], [5], [5], [7], [0]];

    /// <summary>Whether a chord token's root (or, for a diminished triad, its dominant root a major third below) is in <paramref name="allowed"/> above <paramref name="tonic"/>.</summary>
    public static bool RootIn(int token, IReadOnlyCollection<int> allowed, int tonic)
    {
        if (token < 0) return false;
        int r = ((Roman.Root(token) - tonic) % 12 + 12) % 12;
        if (allowed.Contains(r)) return true;
        return Roman.Quality(token) == 2 && allowed.Contains(((r - 4) % 12 + 12) % 12);
    }

    private static int TonicOf(int token) => Roman.Quality(token) == 2 ? ((Roman.Root(token) - 4) % 12 + 12) % 12 : Roman.Root(token);

    /// <summary>First half of the bar on the template (strict = the second half too, or the next bar's chord, or the closing V).</summary>
    private static bool BarOn(ChordGrid g, int bar, int[][] tpl, int i, int tonic, bool strict)
    {
        if (!RootIn(g.At(bar, 0), tpl[i], tonic)) return false;
        if (!strict || g.CellsPerBar < 2) return true;
        var ok = new HashSet<int>(tpl[i]);
        if (i + 1 < tpl.Length) ok.UnionWith(tpl[i + 1]);
        else ok.Add(7);
        return RootIn(g.At(bar, 1), ok, tonic);
    }

    private static bool Matches(ChordGrid g, int k, int[][] tpl, int need, int[] anchors, int tonic, double fullShare)
    {
        int L = tpl.Length;
        if (k + L > g.Bars) return false;
        int first = 0, full = 0;
        for (int i = 0; i < L; i++)
        {
            bool f = BarOn(g, k + i, tpl, i, tonic, false);
            if (f) first++;
            else if (Array.IndexOf(anchors, i) >= 0) return false;
            if (f && BarOn(g, k + i, tpl, i, tonic, true)) full++;
        }
        return first >= need && full >= fullShare * L - 1e-9;
    }

    /// <summary>Blues forms, left to right without overlap: 12-bar (16-bar variant when 4 more bars of the tonic precede it), else 8-bar.</summary>
    public static List<Occurrence> Blues(ChordGrid g, LineageParams lp)
    {
        var outp = new List<Occurrence>();
        var eight = new List<Occurrence>();
        int lastEnd = 0;
        for (int k = 0; k < g.Bars;)
        {
            int c0 = g.At(k, 0);
            if (c0 < 0)
            {
                k++;
                continue;
            }
            int tonic = TonicOf(c0);
            if (Matches(g, k, T12, lp.BluesNeed, A12, tonic, lp.BluesFullShare))
            {
                int start = k;
                string variant = "12-bar";
                if (k - 4 >= lastEnd && Enumerable.Range(k - 4, 4).All(b => RootIn(g.At(b, 0), [0], tonic)))
                {
                    start = k - 4;
                    variant = "16-bar";
                }
                outp.Add(new Occurrence(Blues12.Key, g.BarStart(start), g.BarStart(k + 12), variant));
                k += 12;
                lastEnd = k;
                continue;
            }
            if (Matches(g, k, T8A, 8, [0], tonic, lp.BluesFullShare))
            {
                eight.Add(new Occurrence(Blues8A.Key, g.BarStart(k), g.BarStart(k + 8)));
                k += 8;
                lastEnd = k;
                continue;
            }
            if (Matches(g, k, T8B, 8, [0], tonic, lp.BluesFullShare))
            {
                eight.Add(new Occurrence(Blues8B.Key, g.BarStart(k), g.BarStart(k + 8)));
                k += 8;
                lastEnd = k;
                continue;
            }
            k++;
        }
        foreach (var grp in eight.GroupBy(o => o.Key))
            if (grp.Count() >= lp.Blues8MinOcc) outp.AddRange(grp);
        return outp;
    }

    /// <summary>A cadence as sets of allowed L1 tokens (normalized key), and whether it is in the minor frame.</summary>
    public sealed record Cadence(ProgressionSchema Schema, int[][] Degrees, bool Minor);

    /// <summary>ii-V-I and I-IV-V-I in the song's normalized key, with their minor forms (relative to the minor tonic).</summary>
    public static Cadence[] Cadences(int minorTonic) =>
    [
        new(new("prog:ii-V-I", "ii-V-I cadence", "ii-V-I", "chord"), [[Roman.Token(2, 1)], [Roman.Token(7, 0)], [Roman.Token(0, 0)]], false),
        new(new("prog:iio-V-i", "iio-V-i cadence", Roman.Seq([Roman.Token(minorTonic + 2, 2), Roman.Token(minorTonic + 7, 0), Roman.Token(minorTonic, 1)]), "chord"),
            [[Roman.Token(minorTonic + 2, 2)], [Roman.Token(minorTonic + 7, 0)], [Roman.Token(minorTonic, 1)]], true),
        new(new("prog:I-IV-V-I", "I-IV-V-I cadence", "I-IV-V-I", "chord"), [[Roman.Token(0, 0)], [Roman.Token(5, 0)], [Roman.Token(7, 0)], [Roman.Token(0, 0)]], false),
        new(new("prog:i-iv-V-i", "i-iv-V-i cadence", Roman.Seq([Roman.Token(minorTonic, 1), Roman.Token(minorTonic + 5, 1), Roman.Token(minorTonic + 7, 0), Roman.Token(minorTonic, 1)]), "chord"),
            [[Roman.Token(minorTonic, 1)], [Roman.Token(minorTonic + 5, 1)], [Roman.Token(minorTonic + 7, 0)], [Roman.Token(minorTonic, 1)]], true),
    ];

    /// <summary>
    /// Cadence occurrences on the chord changes (<c>chord_seq</c> 'chg' L1: every chord of at least 0.75 beat, repeats
    /// merged): the cadence's chords as consecutive changes, the last one (the resolution) starting on a bar line, skipping
    /// occurrences that lie mostly (>= half) inside <paramref name="insideLoop"/>. A diminished triad that is not itself
    /// part of the cadence stands for its dominant (viio = V7) and merges with it.
    /// </summary>
    public static List<Occurrence> CadenceOccurrences(ChordLine? ch, double fdb, double bpb, int minorTonic, Func<double, double, double> insideLoop,
        ChordLine? l2 = null)
    {
        var outp = new List<Occurrence>();
        if (ch == null || ch.Count == 0) return outp;
        bpb = bpb > 0 ? bpb : 4.0;
        foreach (var cad in Cadences(minorTonic))
        {
            var all = cad.Degrees.SelectMany(x => x).ToHashSet();
            int Map(int t)
            {
                if (t < 0 || all.Contains(t) || Roman.Quality(t) != 2) return t;
                int dom = Roman.Token(Roman.Root(t) - 4, 0);
                return all.Contains(dom) ? dom : t;
            }
            var runs = new List<(int Tok, double Start, double Dur)>();
            for (int k = 0; k < ch.Count; k++)
            {
                int t = Map(ch.Tokens[k]);
                if (runs.Count > 0 && runs[^1].Tok == t && Math.Abs(runs[^1].Start + runs[^1].Dur - ch.Starts[k]) < 1e-6)
                    runs[^1] = (t, runs[^1].Start, runs[^1].Dur + ch.Durs[k]);
                else runs.Add((t, ch.Starts[k], ch.Durs[k]));
            }
            int L = cad.Degrees.Length;
            for (int i = 0; i + L <= runs.Count;)
            {
                bool ok = true;
                for (int j = 0; j < L && ok; j++) ok = Array.IndexOf(cad.Degrees[j], runs[i + j].Tok) >= 0;
                // Consecutive in time (no gap without a chord), resolving on a bar line.
                for (int j = 1; j < L && ok; j++) ok = Math.Abs(runs[i + j - 1].Start + runs[i + j - 1].Dur - runs[i + j].Start) < 1e-6;
                double pos = (runs[i + L - 1].Start - fdb) / bpb;
                if (ok && Math.Abs(pos - Math.Round(pos)) > 1e-6) ok = false;
                if (!ok)
                {
                    i++;
                    continue;
                }
                double s = runs[i].Start, e = runs[i + L - 1].Start + runs[i + L - 1].Dur;
                // Variant = the harmonic rhythm (beats per chord, the resolution capped at a bar) and, with L2 chords, the chord
                // qualities with sevenths: "2.2.4|m7.7.maj7" (a jazz ii7-V7-Imaj7 is a different version from a triad ii-V-I).
                string variant = string.Join(".", Enumerable.Range(i, L).Select(x => Math.Round(x == i + L - 1 ? Math.Min(runs[x].Dur, bpb) : runs[x].Dur, 2)
                    .ToString(System.Globalization.CultureInfo.InvariantCulture)));
                if (l2 != null) variant += "|" + string.Join(".", Enumerable.Range(i, L).Select(x => L2Quality(l2, runs[x].Start + 0.01)));
                if (insideLoop(s, e) < 0.5 * (e - s)) outp.Add(new Occurrence(cad.Schema.Key, s, e, variant));
                i += L;
            }
        }
        return outp;
    }

    /// <summary>
    /// Descending chromatic bass lines: the bass pitch at each grid cell (the note sounding a sixteenth after the cell
    /// starts, rests absorbed as in <c>melody_line</c>), runs of equal pitch, chains of runs each one semitone below the
    /// previous, every run at most <c>DcbMaxCells</c> cells long (so the line moves at most every few bars and at least
    /// every half-bar), with at least <c>DcbMinSteps</c> steps. The family is
    /// keyed by the first four scale degrees relative to the song's tonic frame (1-7-b7-6 is the line cliche).
    /// </summary>
    public static List<Occurrence> ChromaticBass(NoteLine? bass, double fdb, double bpb, int tonic, LineageParams lp)
    {
        var outp = new List<Occurrence>();
        if (bass == null || bass.Count < lp.DcbMinSteps + 1) return outp;
        bpb = bpb > 0 ? bpb : 4.0;
        int cpb = ChordGrid.CellsFor(bpb);
        double cb = bpb / cpb;
        double end = bass.Onsets[^1] + cb;
        var cells = new List<int>();
        int j = -1;
        for (double t = fdb; t < end - 1e-9; t += cb)
        {
            while (j + 1 < bass.Count && bass.Onsets[j + 1] <= t + 0.25 + 1e-9) j++;
            cells.Add(j >= 0 ? bass.Pitches[j] : int.MinValue);
        }
        var runs = new List<(int P, int Start, int Len)>();
        for (int i = 0; i < cells.Count; i++)
        {
            if (runs.Count > 0 && runs[^1].P == cells[i]) runs[^1] = (cells[i], runs[^1].Start, runs[^1].Len + 1);
            else runs.Add((cells[i], i, 1));
        }
        for (int i = 0; i < runs.Count;)
        {
            if (runs[i].P == int.MinValue || runs[i].Len > lp.DcbMaxCells)
            {
                i++;
                continue;
            }
            int k = i;
            while (k + 1 < runs.Count && runs[k + 1].P != int.MinValue && runs[k + 1].P == runs[k].P - 1 && runs[k + 1].Len <= lp.DcbMaxCells) k++;
            if (k - i >= lp.DcbMinSteps)
            {
                string key = string.Join("-", Enumerable.Range(i, 4).Select(x => Roman.Degree(((runs[x].P % 12) + 12) % 12, tonic)));
                outp.Add(new Occurrence("dcb:" + key, fdb + runs[i].Start * cb, fdb + (runs[k].Start + runs[k].Len) * cb));
                i = k + 1;
            }
            else i++;
        }
        return outp;
    }

    private static readonly string[] L2Names = ["maj", "m", "7", "maj7", "m7", "dim"];

    /// <summary>The L2 quality (with sevenths: maj, m, 7, maj7, m7, dim) of the chord sounding at <paramref name="beat"/>, "-" if none.</summary>
    public static string L2Quality(ChordLine l2, double beat)
    {
        for (int k = 0; k < l2.Count; k++)
            if (l2.Starts[k] <= beat && beat < l2.Starts[k] + l2.Durs[k])
                return l2.Tokens[k] >= 0 && l2.Tokens[k] < 72 ? L2Names[l2.Tokens[k] % 6] : "-";
        return "-";
    }

    public static ProgressionSchema ChromaticBassSchema(string key) =>
        new(key, "descending chromatic bass " + key["dcb:".Length..], key["dcb:".Length..], "bass");
}
