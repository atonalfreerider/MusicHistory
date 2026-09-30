namespace MusicHistory.Influence;

/// <summary>
/// Roman numerals of L1 chord tokens (root * 3 + q; q 0 major, 1 minor, 2 diminished) relative to a tonic, ASCII:
/// "I", "bVII", "vi", "viio" (the convention of the analyze stage's <c>loop.roman</c>).
/// </summary>
internal static class Roman
{
    private static readonly string[] Deg = ["I", "bII", "II", "bIII", "III", "IV", "#IV", "V", "bVI", "VI", "bVII", "VII"];
    private static readonly string[] Scale = ["1", "b2", "2", "b3", "3", "4", "#4", "5", "b6", "6", "b7", "7"];

    public static int Root(int token) => token / 3;
    public static int Quality(int token) => token % 3;
    public static int Token(int root, int quality) => ((root % 12) + 12) % 12 * 3 + quality;

    /// <summary>The numeral of <paramref name="token"/> with <paramref name="tonic"/> as I.</summary>
    public static string Of(int token, int tonic = 0)
    {
        if (token < 0) return "-";
        string s = Deg[((token / 3 - tonic) % 12 + 12) % 12];
        return (token % 3) switch { 0 => s, 1 => s.ToLowerInvariant(), _ => s.ToLowerInvariant() + "o" };
    }

    public static string Seq(IEnumerable<int> tokens, int tonic = 0) => string.Join("-", tokens.Select(t => Of(t, tonic)));

    /// <summary>Scale degree of a pitch class relative to <paramref name="tonic"/>: "1", "b7", "#4".</summary>
    public static string Degree(int pc, int tonic) => Scale[((pc - tonic) % 12 + 12) % 12];

    /// <summary>The token sequence rotated to start at <paramref name="k"/>.</summary>
    public static int[] Rotate(IReadOnlyList<int> seq, int k)
    {
        int n = seq.Count;
        var r = new int[n];
        for (int i = 0; i < n; i++) r[i] = seq[((k + i) % n + n) % n];
        return r;
    }
}

/// <summary>
/// A named chord cycle of the catalogue: <see cref="Degrees"/> are (scale degree, quality) of each chord relative to
/// the tonic of its <see cref="Minor"/> frame (C for major, the minor tonic of the normalization for minor), in the
/// order that names the schema. <see cref="Template"/> builds the label from the roman numerals of the members'
/// rotation ({r}); a <see cref="Fixed"/> schema always shows its own rotation.
/// </summary>
internal sealed record NamedCycle(string Template, (int Deg, int Q)[] Degrees, bool Minor, bool Fixed)
{
    public int[] Tokens(int minorTonic)
    {
        int tonic = Minor ? minorTonic : 0;
        return Degrees.Select(d => Roman.Token(d.Deg + tonic, d.Q)).ToArray();
    }
}

/// <summary>
/// The catalogue of named loop schemas (DESIGN.md §8b) and the naming rule of a loop family. A loop family is keyed
/// by its root cycle (rotation-invariant, quality-tolerant); it takes a catalogue name when its root cycle is the
/// schema's and its majority chord qualities agree with the schema's (all of a 3-chord cycle, all but one of a longer
/// one: E vs Em noise). Two-chord cycles are "two-chord vamps". Everything else is named by its roman numerals in the
/// C/Am frame, shown in the tonic frame (i = the minor tonic) when most of the family's songs are in minor.
/// </summary>
internal static class Catalogue
{
    private const int M = 0, m = 1, d = 2;

    public static readonly NamedCycle[] Cycles =
    [
        new("axis progression {r}", [(0, M), (7, M), (9, m), (5, M)], false, false),                 // I-V-vi-IV and its rotations
        new("axis progression {r}", [(0, m), (8, M), (3, M), (10, M)], true, true),                  // i-bVI-bIII-bVII (minor frame)
        new("doo-wop progression {r}", [(0, M), (9, m), (5, M), (7, M)], false, true),               // I-vi-IV-V
        new("Pachelbel ground {r}", [(0, M), (7, M), (9, m), (4, m), (5, M), (0, M), (5, M), (7, M)], false, true),
        new("Pachelbel ground (first half) {r}", [(0, M), (7, M), (9, m), (4, m)], false, true),     // I-V-vi-iii
        new("Andalusian cadence {r}", [(0, m), (10, M), (8, M), (7, M)], true, true),                // i-bVII-bVI-V
        new("Mixolydian vamp {r}", [(0, M), (10, M), (5, M)], false, true),                           // I-bVII-IV
        new("bVII-bVI shuttle {r}", [(0, M), (10, M), (8, M), (10, M)], false, true),                // I-bVII-bVI-bVII
        new("bVII-bVI shuttle {r}", [(0, m), (10, M), (8, M), (10, M)], true, true),                 // i-bVII-bVI-bVII
        new("Aeolian progression {r}", [(0, m), (8, M), (10, M)], true, true),                       // i-bVI-bVII
        new("{r} turnaround", [(0, M), (9, m), (2, m), (7, M)], false, true),                        // I-vi-ii-V
        new("{r} cadence loop", [(2, m), (7, M), (0, M)], false, true),                              // ii-V-I
    ];

    /// <summary>Booth least rotation of a cycle's roots, as a key ("0.7.9.5").</summary>
    public static string RootKey(IReadOnlyList<int> tokens)
    {
        var roots = tokens.Select(Roman.Root).ToArray();
        int k = Features.Booth(roots);
        return string.Join(".", Roman.Rotate(roots, k));
    }

    /// <summary>
    /// The label of a loop family. <paramref name="canon"/> = the family's majority tokens in its canonical rotation
    /// (Booth least rotation of the roots), <paramref name="displayPhase"/> = the rotation most members start on,
    /// <paramref name="minorMajority"/> = most members are minor-mode songs. Returns (label, kind is a named schema).
    /// </summary>
    public static (string Label, bool Named) Name(int[] canon, int displayPhase, bool minorMajority, int minorTonic)
    {
        int n = canon.Length;
        string key = RootKey(canon);
        // Minor-frame entries first for a minor family (the same cycle may have a major-frame name, e.g. the axis).
        var order = Cycles.OrderBy(c => c.Minor == minorMajority ? 0 : 1).ToList();
        foreach (var c in order)
        {
            var toks = c.Tokens(minorTonic);
            if (toks.Length != n || RootKey(toks) != key) continue;
            // Align the schema to the family's canonical rotation and count agreeing qualities.
            int best = -1, bestShift = 0;
            for (int s = 0; s < n; s++)
            {
                var rot = Roman.Rotate(toks, s);
                if (!rot.Select(Roman.Root).SequenceEqual(canon.Select(Roman.Root))) continue;
                int agree = Enumerable.Range(0, n).Count(i => rot[i] == canon[i]);
                if (agree > best)
                {
                    best = agree;
                    bestShift = s;
                }
            }
            if (best < 0 || best < (n >= 4 ? n - 1 : n)) continue;
            int tonic = c.Minor ? minorTonic : 0;
            // Fixed schemas show their own rotation (with the family's qualities); others the members' rotation.
            int start = c.Fixed ? ((n - bestShift) % n + n) % n : displayPhase;
            var shown = Roman.Rotate(canon, start);
            if (!c.Fixed && c.Minor != minorMajority) tonic = minorMajority ? minorTonic : 0;
            if (!c.Fixed && minorMajority) shown = StartOnTonic(shown, minorTonic);
            return (c.Template.Replace("{r}", Roman.Seq(shown, tonic), StringComparison.Ordinal), true);
        }
        int frame = minorMajority ? minorTonic : 0;
        var disp = Roman.Rotate(canon, displayPhase);
        if (minorMajority) disp = StartOnTonic(disp, minorTonic);
        string r = Roman.Seq(disp, frame);
        return n == 2 ? ($"two-chord vamp {r}", true) : (r, false);
    }

    /// <summary>A minor-frame display starts on the minor tonic chord when the cycle holds it (i-bVI-bIII-bVII).</summary>
    private static int[] StartOnTonic(int[] seq, int minorTonic)
    {
        int i = Array.IndexOf(seq, Roman.Token(minorTonic, 1));
        return i > 0 ? Roman.Rotate(seq, i) : seq;
    }
}
