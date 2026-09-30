using System.Globalization;
using System.Reflection;
using System.Text.Json.Nodes;

namespace MusicHistory.Influence;

/// <summary>
/// Every tunable of the influence stage with its default. The evidence and decision (V8, empirical threshold) were
/// rebuilt from the calibration benchmark (influence/README.md, "V2 calibration") and then calibrated on the real
/// 1,012-song data after the analyze v2 rerun (README "V2.1 calibration on the real data"); each default says where it
/// comes from. Alignment scores (points, identity = +2) are only used for display passages and versions; the
/// kernels use integers scaled by <see cref="Scale"/>.
/// </summary>
internal sealed class Params
{
    public const int Scale = 20;

    // Evidence (V8, analytic corpus null over rare n-grams)
    public int DfCap = 8;                    // rare-only: n-grams whose pair df exceeds 8 weigh 0; 8 (not 5) keeps a shared idea of a cluster of up to 8 songs (the Pachelbel group: C U When U Get There -> Memories 25.0 -> 32.0, an edge) at the same 3e-5 FPR; 10 / 12 add nothing; <= 0: no cap
    public double SigmaFloor = 2;            // z = (S - mu) / sqrt(var + 2^2) (analytic.py floor)
    public int NullSizeAdjust = 1;           // P(A holds g) ~ p(g) |A| / mean |A|: mu and var scale by the earlier song's key-set size; off, dense lines made hubs (Star Dust subtree 178, Ode to Billie Joe 93 children at 1e-3)
    public int WindowBars = 16, WindowHop = 4;   // windows of the later song: 16 bars every 4 bars (pairlevel.py)
    public int RiffInBass = 1;               // the schema-filtered riff family (rs8) is part of the bass channel
    public int MinLineNotes = 8, MinChordChanges = 3;   // a window or song channel needs this many notes / chord changes
    public double LaneDupJaccard = 0.8;      // a lane whose melody n-grams overlap the song's lead line this much is the lead's own lane: not a lane
    public int KeyFreeDf = 2;                // 1: a key-dependent n-gram's weight (rarity, cap) uses the df of its transposition-invariant form (a stock progression in a non-home key is not rare); 2: also, rhythm-coded n-grams of the ProjScope channels weigh by the df of their pitch-only content (0 = V2)
    public int LineMinPc = 3;                // melody-family n-grams (lead line, lanes) need this many distinct pitch classes: two-note alternations are accompaniment (0 = off)
    public int FigPeriod = 3;                // melody-family n-grams whose pitch classes repeat with a period 2..3 (broken chords, arpeggio cycles) are figures, dropped (0 = off)
    public double FigShare = 1;              // ... repeat = at least this share of the notes equal the note FigPeriod earlier (1 = exactly periodic)
    public int MelRhythmFams = 1;            // 1: add the melody families mtype5 / mtype7 (interval x IOI-ratio class, 5 / 7 intervals): longer pitch-and-rhythm evidence (I Won't Back Down -> Stay with Me 23 -> 37)
    public int MelDropFams = 16;             // bitmask of melody families left out of the evidence (bit = Fam index: 0 int5, 1 int7, 2 deg6, 3 mtype4, 4 ivr4, 5 dp3); 16: ivr4, token for token the same n-gram as mtype4 (double counted in V2)
    public int ProjScope = 1;                // KeyFreeDf 2: channels whose rhythm-coded n-grams weigh by their pitch-only content (1 chords, 2 melody/lanes, 4 bass): a progression's specificity is its harmony

    // Transposition hedge: B's window also at +3, -3, +5, -5 semitones (key-dependent families only)
    public int Hedge = 1;
    public double ShiftPenalty = 12;         // subtracted from a shifted window's fused z: pair threshold 20.66 -> 22.34 (26.72 at 3), controls 6 -> 7 of 25

    // Decision: weighted Stouffer over channels, window max, empirical threshold
    public double WMelody = 1.0, WLanes = 0, WChord = 1.0, WBass = 0.7, WLoop = 0.2;   // benchmark grid (evaluate --tune); bass weaker, loop auxiliary; lanes off (WLanes 0 = no lane is indexed): on the real data the lanes channel matched accompaniment figures of dense lines (at 1e-3 it counted in 322 of 673 edges, Ode to Billie Joe had 93 children) and no known pair needs it
    public int FuseMode = 0;                 // 0: Stouffer over every available channel; 1 (experiment): over channels at z >= FuseMinZ (threshold 33.4, controls 6)
    public double FuseMinZ = 3;
    public double LoopAuxZ = 5;              // the loop channel joins the sum only beside a main channel at z >= 5
    public double TargetFpr = 3e-5;          // pair false-positive rate of the empirical threshold (3e-5 x 507,740 = 15 expected chance pairs; at 1e-3 508 of 673 edges were expected chance pairs)
    public int NullSample = 30000;           // time-ordered pairs sampled (deterministically) for the threshold
    public double CountFpr = 0.01;           // a channel "counts" at the winning window above its own (1 - CountFpr) sample quantile ...
    public double CountMinZ = 3;             // ... and at z >= 3 (a channel whose sample quantile is ~0, such as loops, needs real evidence)
    public double RiffFactor = 1.0;          // a pair whose only counting channel is bass needs its riff family alone at z > 1.0 x the fused threshold
    public double HubZ = 0;                  // two-sided hubness check (0 = off): min(zA, zB) >= HubZ over each song's pairs
    public double HubSdFloor = 2;
    public double StoreFpr = 0.01;           // pair_score keeps the null sample, pairs above its (1 - StoreFpr) quantile, and all decided pairs

    // Alignment (display passages only) and versions
    public double MelMatch = 2, MelNear = 0, MelMismatch = -1, MelOffMetric = 0.75;
    public double MelGapOpen = 3, MelGapExtend = 0.3, Consolidation = 0.2;
    public double ChordGapOpen = 2.5, ChordGapExtend = 0.75, ChordSameRoot = 0.25;
    public int Hits = 3;
    public double MinHit = 6;
    public double PmiMin = 0.60, ChordIdentityMin = 0.60, DurationRatioMin = 0.6, DurationRatioMax = 1.6;
    public int NwGapOpen = 12, NwGapExtend = 6;

    // Credit and tree
    public double CreditTie = 0.10, CreditCover = 0.5, ParentFraction = 0.5, KatzAlpha = 0.2;
    public double CreditMergeBeats = 32;     // a pair's shared passages closer than this (beats) form one passage for credit
    public double CreditMinBits = 12;        // a passage needs at least this much evidence (bits) to be credited
    public int MaxSecondary = 8;

    // Excerpts
    public int ExcerptMinBars = 8, ExcerptMaxBars = 24, ExcerptDefaultBars = 16;

    // Graph export: similarity = 1 - 2^(-S / SimilarityHalfBits)
    public double SimilarityHalfBits = 16;

    public int Threads = Math.Max(1, Environment.ProcessorCount);

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
