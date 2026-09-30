using System.Globalization;
using System.Reflection;
using System.Text.Json.Nodes;

namespace MusicHistory.Influence;

/// <summary>
/// Every tunable of DESIGN.md §8 with its default (mir-methods.md §2.5, §3.4, §5). Scores are in
/// alignment "points" (identity = +2) and in bits; the kernels use integers scaled by
/// <see cref="Scale"/> so that alignment is exact and deterministic. Values that differ from the
/// design's starting values, or settle something it leaves open, say so ("design: ...") and were
/// chosen on the planted-influence fixture (influence/README.md, "Calibration").
/// </summary>
internal sealed class Params
{
    public const int Scale = 20;

    // 3. rarity / 4. candidates
    public double StopFraction = 0.05;       // n-grams in more than 5 % of songs are stop-grams (not used for candidates)
    public int StopMinDf = 2;                // ... and in more than this many songs (tiny corpora)
    public int CandTop = 60;
    public double CandBits = 20;
    public int CandMax = 200;
    public int ContemporaneousMax = 20;      // same-time pairs checked for versions per song

    // 5. alignment (points)
    public double MelMatch = 2, MelNear = 0, MelMismatch = -1, MelOffMetric = 0.75;
    public double MelGapOpen = 3, MelGapExtend = 0.3, Consolidation = 0.2;
    public double ChordGapOpen = 2.5, ChordGapExtend = 0.75, ChordSameRoot = 0.25;
    public double FifthPenalty = 3;
    public int Hits = 3;
    public double MinHit = 6;                // a hit must score at least 3 identities

    // 6. evidence
    public double CapFraction = 0.02;        // n-grams in more than 2 % of songs are commonplace ...
    public double StopCapBits = 8;           // ... and score cheaply: at most 8 bits per channel per pair (mir-methods §5.1 schema cap; design: uncapped)
    public int EvidenceStopGrams = 1;        // experiment: 0 = commonplace n-grams do not score at all

    // 7. null model
    public int KScreen = 20, KConfirm = 100;
    public double ScreenZ = 1.5;
    public double SigmaFloor = 4;            // z = (E - mu) / max(sigma, 4 bits), about half of one rare n-gram (design: 0.5)
    public double MinEvidenceBits = 1.0;     // channels with E below max(this, ChannelZ * SigmaFloor) cannot reach z = 2: not tested
    public int LoopNullCorpus = 1;           // loop identities: null = random earlier songs' loops (0: loops found in Markov chord walks)
    public int SkipUnreachable = 1;          // no null for pairs whose E cannot pass the bits gate; they enter BH with p = 1
    public int NullDistinct = 0;             // experiment: Markov successors 0 all transitions, 1 distinct, 2 verbatim repeats once
    public int NullKeepRhythm = 0;           // experiment: 1 = surrogates keep B's rhythm in place

    // 8. decision
    public double ChannelZ = 2;
    public int ZOverCounting = 1;            // Stouffer over the counting channels (0: denominator over every available channel)
    public int LoopAuxiliary = 1;            // the loop channel (weight 0.1) counts only next to a counting melody/bass/chord channel
    public double WMelody = 0.6, WBass = 0.3, WChord = 0.3, WLoop = 0.1;
    public double EntropyHalf = 2.0, EntropyDrop = 1.5;
    public double ZMin = 3, QMax = 0.05;
    public double MelodyBits = 24, BassBits = 24, ChordBits = 16;
    public int MinLineNotes = 8, MinChordChanges = 3;

    // 9. versions
    public double PmiMin = 0.60, ChordIdentityMin = 0.60, DurationRatioMin = 0.6, DurationRatioMax = 1.6;
    public int NwGapOpen = 12, NwGapExtend = 6;

    // 10. credit and tree
    public double CreditTie = 0.10, CreditCover = 0.5, ParentFraction = 0.5, KatzAlpha = 0.2;
    public double CreditMergeBeats = 32;     // a pair's shared passages closer than this (beats) form one passage for credit
    public double CreditMinBits = 12;        // a passage needs at least this much evidence (bits) to be credited
    public int MaxSecondary = 8;

    // 11. excerpts
    public int ExcerptMinBars = 8, ExcerptMaxBars = 24, ExcerptDefaultBars = 16;

    // graph export: similarity = 1 - 2^(-S / SimilarityHalfBits)
    public double SimilarityHalfBits = 16;

    public int Threads = Math.Max(1, Environment.ProcessorCount);

    public double[] ChannelWeights => [WMelody, WBass, WChord, WLoop];

    /// <summary>Apply "--param name=value" overrides (field names, case-insensitive).</summary>
    public void Set(string assignment)
    {
        int eq = assignment.IndexOf('=');
        if (eq <= 0) throw new ArgumentException($"--param expects name=value, got '{assignment}'");
        string name = assignment[..eq].Trim(), value = assignment[(eq + 1)..].Trim();
        var field = typeof(Params).GetField(name, BindingFlags.Public | BindingFlags.Instance | BindingFlags.IgnoreCase)
                    ?? throw new ArgumentException($"unknown parameter '{name}'");
        if (field.FieldType == typeof(int)) field.SetValue(this, int.Parse(value, CultureInfo.InvariantCulture));
        else field.SetValue(this, double.Parse(value, CultureInfo.InvariantCulture));
    }

    public JsonObject ToJson()
    {
        var o = new JsonObject();
        foreach (var f in typeof(Params).GetFields(BindingFlags.Public | BindingFlags.Instance))
        {
            if (f.Name == nameof(Threads)) continue;
            object? v = f.GetValue(this);
            o[f.Name] = v switch { int i => JsonValue.Create(i), double d when double.IsFinite(d) => JsonValue.Create(d), _ => null };
        }
        return o;
    }

    // Integer kernel parameters.
    public int Pts(double points) => (int)Math.Round(points * Scale, MidpointRounding.AwayFromZero);
}
