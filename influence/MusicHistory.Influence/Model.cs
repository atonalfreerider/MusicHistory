using System.Globalization;

namespace MusicHistory.Influence;

internal enum Channel { Melody = 0, Bass = 1, Chord = 2, Loop = 3 }

internal static class Channels
{
    public const int Count = 4;
    public static readonly string[] Names = ["melody", "bass", "chord", "loop"];
    public static string Name(Channel c) => Names[(int)c];
}

/// <summary>A normalized note line (<c>melody_line</c>): lead or bass, rests absorbed, C/Am frame.</summary>
internal sealed class NoteLine
{
    public required double[] Onsets;
    public required double[] Durs;
    public required int[] Pitches;
    public required int[] Met;
    public int Count => Pitches.Length;
}

/// <summary><c>chord_seq</c> kind 'chg', level 'L1': root * 3 + q (0 maj, 1 min, 2 dim).</summary>
internal sealed class ChordLine
{
    public required int[] Tokens;
    public required double[] Starts;
    public required double[] Durs;
    public required int[] Downbeat;
    public int Count => Tokens.Length;
}

/// <summary>One <c>loop</c> row, kept in the song's own phase so it can be re-rotated after a shift.</summary>
internal sealed class LoopRow
{
    public int Family;
    public int[] Own = [];        // L1 cycle tokens starting where the song's loop starts
    public int[] OwnRhythm = [];  // duration classes in the same order
    public string? Roman;
    public double LoopBeats;
    public int Passes, Visits;
    public double Coverage;
    public double[] VisitStarts = [];

    /// <summary>Beats one visit of this family lasts (coverage spread over its visits).</summary>
    public double VisitBeats => Visits > 0 && Coverage > 0 ? Coverage / Visits : Math.Max(LoopBeats, 1);
}

/// <summary>One <c>key_region</c> row (analyze stage): a stretch of the song in one native key, C = 0.</summary>
internal readonly record struct KeyRegion(double Start, double End, int TonicPc, string Mode);

/// <summary>Key-region lookups for the walkthrough's key handoff (DESIGN.md §10 entry/exit keys).</summary>
internal static class KeyRegions
{
    /// <summary>How far before <c>excerpt_end_beat</c> the exit key is read ("just before the end").</summary>
    public const double ExitEpsilon = 1e-3;

    /// <summary>
    /// The region containing <paramref name="beat"/>, as <c>musichistory.identity.key.ShiftMap.region_at</c>
    /// (the rule the normalized MIDI was shifted by): the last region starting at or before the beat
    /// (1e-6 tolerance), else the first. Null when the song has no regions. <paramref name="regions"/>
    /// must be sorted by start.
    /// </summary>
    public static KeyRegion? At(KeyRegion[]? regions, double beat)
    {
        if (regions == null || regions.Length == 0) return null;
        int i = 0;
        for (int k = 1; k < regions.Length && regions[k].Start <= beat + 1e-6; k++) i = k;
        return regions[i];
    }

    /// <summary>
    /// The keys heard where an excerpt starts (region at <paramref name="start"/>) and ends (region at
    /// <paramref name="end"/> - <see cref="ExitEpsilon"/>). Each is null when that region is the song's
    /// home key (tonic_pc, mode) or the song has no key regions, so a viewer falls back to tonic_pc/mode.
    /// </summary>
    public static (int? EntryTonic, string? EntryMode, int? ExitTonic, string? ExitMode) Local(Song s, KeyRegion[]? regions, double start, double end)
    {
        var (et, em) = NonHome(s, At(regions, start));
        var (xt, xm) = NonHome(s, At(regions, Math.Max(start, end - ExitEpsilon)));
        return (et, em, xt, xm);
    }

    private static (int?, string?) NonHome(Song s, KeyRegion? r)
    {
        if (r is not { } k) return (null, null);
        int tonic = ((k.TonicPc % 12) + 12) % 12;
        string mode = k.Mode == "minor" ? "minor" : "major";
        return tonic == ((s.TonicPc % 12) + 12) % 12 && mode == s.Mode ? (null, null) : (tonic, mode);
    }
}

/// <summary>A selected, analyzed song with its work facts and identities.</summary>
internal sealed class Song
{
    public int Index;                 // 0-based position in node order; node id = Index + 1
    public string WorkId = "";
    public string Title = "", Artist = "";
    public string? OriginalArtist;
    public int Year;
    public string? ReleaseDate;       // work.work_date as stored
    public int? DatePrecision;        // work.work_date_precision as stored
    public string? FirstChartWeek;
    public DateOrder Date;
    public double TimeValue => Date.TimeValue;
    public int? CanonRank;

    public int TonicPc;
    public string Mode = "major";
    public int NormShift;
    public double NativeBpm = 120, BeatsPerBar = 4, FirstDownbeat;
    public string MidiPath = "";
    public string? NormalizedMidiPath;
    public string? MidiSource;
    public double? KeyConfidence, MelodyConfidence;
    public double IntervalEntropy;
    public bool Fifth;                // key_ambiguous_fifth
    public string? MainLoop;
    public string? SummaryJson;
    public double EndBeat, DurationS;
    public string? ResonanceCommit;

    public NoteLine? Melody, Bass;
    public ChordLine? Chords;
    public LoopRow[] Loops = [];

    public Features F = null!;

    public override string ToString() => $"{WorkId} {Title} ({Year})";
}

/// <summary>
/// Release timing and the order rule of DESIGN.md §8.1. <see cref="TimeValue"/> is the fractional
/// year: day precision <c>year + (doy - 0.5)/365.25</c>, month <c>year + (month - 0.5)/12</c>,
/// year <c>year + 0.5</c>. A year-precision song whose first Hot 100 week falls in the same year
/// takes that week as its day (an upper bound of the release), so that the chart-week rule can
/// order it without contradicting the time axis. A chart week from any other year is ignored
/// entirely (DESIGN.md §10: within-year ordering by first_chart_week needs both weeks in the
/// songs' shared year).
/// </summary>
internal readonly struct DateOrder
{
    public readonly int Year;
    public readonly int Precision;    // effective: 9 year, 10 month, 11 day
    public readonly int Month, Day;
    public readonly int ChartDay;     // days since 0001-01-01, or int.MinValue
    public readonly double TimeValue;

    public DateOrder(int year, int precision, int month, int day, int chartDay, double timeValue)
    {
        Year = year;
        Precision = precision;
        Month = month;
        Day = day;
        ChartDay = chartDay;
        TimeValue = timeValue;
    }

    public bool HasChart => ChartDay != int.MinValue;

    public static DateOrder Make(int year, string? date, int? precision, string? chartWeek)
    {
        int prec = 9, month = 0, day = 0;
        if (!string.IsNullOrWhiteSpace(date))
        {
            var parts = date.Trim().Split('-', 'T', ' ');
            if (parts.Length > 0 && int.TryParse(parts[0], NumberStyles.Integer, CultureInfo.InvariantCulture, out int y) && y == year)
            {
                int stated = precision ?? (parts.Length >= 3 ? 11 : parts.Length == 2 ? 10 : 9);
                if (stated >= 10 && parts.Length >= 2 && int.TryParse(parts[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out int m) && m is >= 1 and <= 12)
                {
                    prec = 10;
                    month = m;
                    if (stated >= 11 && parts.Length >= 3 && int.TryParse(parts[2], NumberStyles.Integer, CultureInfo.InvariantCulture, out int d)
                        && d >= 1 && d <= DateTime.DaysInMonth(year, m))
                    {
                        prec = 11;
                        day = d;
                    }
                }
            }
        }
        // A chart week only says something about the order within the song's own year. canon stores
        // the earliest week of *any* recording, which for a pre-Hot 100 or uncharted original is a
        // later reissue or cover (Please Please Me, 1963: 1964-02-01), so a week from another year is
        // dropped here: it neither orders the song (HasChart false) nor moves its time value.
        int chart = int.MinValue;
        DateOnly chartDate = default;
        if (!string.IsNullOrWhiteSpace(chartWeek) && DateOnly.TryParseExact(chartWeek.Trim()[..Math.Min(10, chartWeek.Trim().Length)], "yyyy-MM-dd",
                CultureInfo.InvariantCulture, DateTimeStyles.None, out chartDate) && chartDate.Year == year)
            chart = chartDate.DayNumber;

        double tv = prec switch
        {
            11 => year + (new DateOnly(year, month, day).DayOfYear - 0.5) / 365.25,
            10 => year + (month - 0.5) / 12.0,
            _ => chart != int.MinValue ? year + (chartDate.DayOfYear - 0.5) / 365.25 : year + 0.5,
        };
        tv = Math.Min(tv, year + 0.99999);
        return new DateOrder(year, prec, month, day, chart, tv);
    }

    /// <summary>
    /// A is earlier than B if the years differ, or both dates have month-or-better precision and
    /// differ, or both have a first chart week in their (shared) year and the weeks differ by more
    /// than 4 weeks (<see cref="Make"/> drops a week from another year). The result
    /// must also agree with the time axis (strictly smaller time value); otherwise the pair is
    /// contemporaneous and gets no edge.
    /// </summary>
    public static bool Earlier(in DateOrder a, in DateOrder b)
    {
        if (a.Year != b.Year) return a.Year < b.Year;
        int sign = 0;
        if (a.Precision >= 10 && b.Precision >= 10)
        {
            if (a.Month != b.Month) sign = a.Month < b.Month ? -1 : 1;
            else if (a.Precision == 11 && b.Precision == 11 && a.Day != b.Day) sign = a.Day < b.Day ? -1 : 1;
        }
        if (sign == 0 && a.HasChart && b.HasChart && Math.Abs(a.ChartDay - b.ChartDay) > 28)
            sign = a.ChartDay < b.ChartDay ? -1 : 1;
        return sign < 0 && a.TimeValue < b.TimeValue;
    }
}

/// <summary>Key names of DESIGN.md §3 (ASCII; flats for F Bb Eb Ab Db Gb major, D G C F Bb Eb minor).</summary>
internal static class Keys
{
    private static readonly string[] Sharp = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
    private static readonly string[] Flat = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"];
    private static readonly int[] FlatMajor = [5, 10, 3, 8, 1, 6];
    private static readonly int[] FlatMinor = [2, 7, 0, 5, 10, 3];

    public static string Name(int tonic, string mode)
    {
        int t = ((tonic % 12) + 12) % 12;
        bool minor = mode == "minor";
        bool flat = Array.IndexOf(minor ? FlatMinor : FlatMajor, t) >= 0;
        return $"{(flat ? Flat : Sharp)[t]} {(minor ? "minor" : "major")}";
    }
}
