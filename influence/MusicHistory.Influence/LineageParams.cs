using System.Globalization;
using System.Reflection;
using System.Text.Json.Nodes;

namespace MusicHistory.Influence;

/// <summary>
/// Tunables of the identity-lineage mode (DESIGN.md §8b, <c>run --mode lineage</c>). They live apart from
/// <see cref="Params"/> so the strict evidence mode's report (which lists every <see cref="Params"/> field) stays
/// byte-identical. The rules shared with the evidence tree come from <see cref="Params"/>: <c>ParentFraction</c> (0.5,
/// the user's rule), <c>MaxSecondary</c> (8), <c>KatzAlpha</c> and the excerpt bounds (8..24 bars). Set with
/// <c>--param Name=value</c> like the others (names are distinct from <see cref="Params"/>'s).
/// </summary>
internal sealed class LineageParams
{
    // Families
    public double MinLoopBars = 8;          // a loop family counts for a song when its loops cover >= 8 bars of it (DESIGN §8b)
    public int MinPasses = 2;               // ... counting loop rows whose cycle repeats within a visit (passes >= 2); a passes-1 row is a
                                            // non-repeating phrase whose "cycle" is a reduced chord summary (Tennessee Waltz's I-V row is I I II V)
    public double StrengthCap = 1;          // strength = the family's share of the song's beats, capped
    public int ProgMinOcc = 2;              // ii-V-I / I-IV-V-I cadences: >= 2 occurrences outside loop visits
    public int BluesNeed = 11;              // 12-bar blues: >= 11 of 12 bars' first half-bars on the template (anchors bars 1, 5, 9, 11)
    public double BluesFullShare = 0.667;   // ... and >= 2/3 of the bars matched in both halves (the second half may anticipate the next bar)
    public int Blues8MinOcc = 2;            // 8-bar blues (a short form): >= 2 occurrences
    public int DcbMinSteps = 3;             // descending chromatic bass: >= 3 semitone steps (4 notes), one per half-bar or longer
    public int DcbMaxCells = 8;             // ... each note held at most 8 grid cells (4 bars in 4/4)

    // Score (DESIGN §8b): sum over shared families of specificity x agreement x min(strength)^0.5, plus the strong term
    public double AgreeSame = 1, AgreePhase = 0.75, AgreeOther = 0.5;   // same phase and rhythm / same phase / otherwise
    public double StrongWeight = 10;        // a strong match adds StrongWeight x its fused z (z >= 30: >= 300 bits, above any family sum)

    // The degenerate-structure guard. Closeness = the finer agreement of two members of a family: for loops the shares
    // of equal chord qualities and duration classes, the loop-length ratio and the strength ratio; for progressions the
    // strength ratio; 1 for strong matches.
    public int FineAgreement = 1;           // 1: the score's agreement factor is multiplied by the closeness, so a family's strong influencers are its close versions (0: design factor only)
    public int ClosenessProduct = 1;        // closeness = product of its parts (1) or their mean (0)
    public double ClosenessPower = 2;       // the score's agreement factor is multiplied by closeness ^ ClosenessPower: squared, a version's strong influencers (score >= 0.5 max) are its close versions (real data: largest hub 41 -> 24 children, ii-V-I family 25 -> 36 parents; 3: 19 children)
    public int CreditCloseness = 1;         // an exact score tie is credited to the closest version, then the earliest (0: the earliest)

    // Export: similarity = 1 - 2^(-score / LineageHalfBits)
    public double LineageHalfBits = 8;

    public static bool Has(string name) =>
        typeof(LineageParams).GetField(name, BindingFlags.Public | BindingFlags.Instance | BindingFlags.IgnoreCase) != null;

    /// <summary>Apply "name=value" (field names, case-insensitive).</summary>
    public void Set(string assignment)
    {
        int eq = assignment.IndexOf('=');
        if (eq <= 0) throw new ArgumentException($"--param expects name=value, got '{assignment}'");
        string name = assignment[..eq].Trim(), value = assignment[(eq + 1)..].Trim();
        var field = typeof(LineageParams).GetField(name, BindingFlags.Public | BindingFlags.Instance | BindingFlags.IgnoreCase)
                    ?? throw new ArgumentException($"unknown parameter '{name}'");
        if (field.FieldType == typeof(int)) field.SetValue(this, int.Parse(value, CultureInfo.InvariantCulture));
        else field.SetValue(this, double.Parse(value, CultureInfo.InvariantCulture));
    }

    public JsonObject ToJson()
    {
        var o = new JsonObject();
        foreach (var f in typeof(LineageParams).GetFields(BindingFlags.Public | BindingFlags.Instance))
        {
            object? v = f.GetValue(this);
            o[f.Name] = v switch { int i => JsonValue.Create(i), double d when double.IsFinite(d) => JsonValue.Create(d), _ => null };
        }
        return o;
    }
}
