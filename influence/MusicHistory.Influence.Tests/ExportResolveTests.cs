namespace MusicHistory.Influence.Tests;

/// <summary>Key-region lookup and graph_meta settings resolution (sources, precedence, warnings).</summary>
public class ExportResolveTests
{
    [Fact]
    public void KeyRegionLookupFollowsTheAnalyzeShiftMap()
    {
        var s = new Song { TonicPc = 0, Mode = "major" };
        KeyRegion[] regs = [new(0, 32, 0, "major"), new(32, 96, 2, "major"), new(96, 200, 0, "major")];
        // Region "containing" a beat: last start <= beat (+1e-6), else the first region (identity/key.py ShiftMap).
        Assert.Equal(2, KeyRegions.At(regs, 32 - 5e-7)!.Value.TonicPc);
        Assert.Equal(0, KeyRegions.At(regs, 31.99)!.Value.TonicPc);
        Assert.Equal(0, KeyRegions.At(regs, -3)!.Value.TonicPc);
        Assert.Equal(0, KeyRegions.At(regs, 500)!.Value.TonicPc);
        Assert.Null(KeyRegions.At([], 10));
        Assert.Null(KeyRegions.At(null, 10));
        // Excerpt inside the D major stretch; ending exactly where home returns still exits in D major.
        Assert.Equal(((int?)2, (string?)"major", (int?)2, (string?)"major"), KeyRegions.Local(s, regs, 40, 96));
        // Starting where home returns enters home (NULL).
        Assert.Equal(((int?)null, (string?)null, (int?)null, (string?)null), KeyRegions.Local(s, regs, 96, 160));
        Assert.Equal(((int?)null, (string?)null, (int?)null, (string?)null), KeyRegions.Local(s, null, 0, 64));
    }

    private static Func<string, string?> Env(string? norm, string? bpm) => name => name switch
    {
        "MUSICHISTORY_NORMALIZATION" => norm,
        "MUSICHISTORY_TARGET_BPM" => bpm,
        _ => null,
    };

    private static ExportSettings Resolve(MiniPipeline mini, string name, IReadOnlyList<MiniSong> songs, Dictionary<string, string>? meta,
        Func<string, string?> env, params string[] extraSql)
    {
        string db = mini.Build(name, songs, meta, extraSql);
        using var c = PipelineDb.Open(db);
        return ExportSettings.Resolve(c, songs.Select(s => s.Id).ToHashSet(StringComparer.Ordinal), env);
    }

    [Fact]
    public void AnalyzeMetaWinsOverTheEnvironment()
    {
        using var mini = new MiniPipeline();
        var meta = new Dictionary<string, string> { ["analyze_normalization"] = "parallel", ["analyze_target_bpm"] = "100" };
        var st = Resolve(mini, "m", [new("RA", 0, "major", Normalization: "parallel", TargetBpm: 100), new("RB", 9, "minor", Normalization: "parallel", TargetBpm: 100)],
            meta, Env("relative", "120"));
        Assert.Equal(("parallel", 100.0, "C major / C minor"), (st.Normalization, st.TargetBpm, st.TargetKey));
        Assert.Contains("analyze_normalization", st.NormalizationSource);
        Assert.Contains("analyze_target_bpm", st.TargetBpmSource);
        Assert.Empty(st.Warnings);
    }

    [Fact]
    public void MetaContradictedBySongsWarns()
    {
        using var mini = new MiniPipeline();
        var meta = new Dictionary<string, string> { ["analyze_normalization"] = "relative", ["analyze_target_bpm"] = "120" };
        var st = Resolve(mini, "c", [new("RA", 0, "major", Normalization: "relative", TargetBpm: 120), new("RB", 9, "minor", Normalization: "parallel", TargetBpm: 100)],
            meta, Env(null, null));
        Assert.Equal(("relative", 120.0), (st.Normalization, st.TargetBpm));
        Assert.Equal(2, st.Warnings.Count);
        Assert.Contains(st.Warnings, w => w.Contains("song.normalization"));
        Assert.Contains(st.Warnings, w => w.Contains("song.target_bpm"));
    }

    [Fact]
    public void SongColumnsAreUsedWhenTheMetaIsMissing()
    {
        using var mini = new MiniPipeline();
        var st = Resolve(mini, "s", [new("RA", 0, "major", Normalization: "parallel", TargetBpm: 90), new("RB", 9, "minor", Normalization: "parallel", TargetBpm: 90)],
            null, Env("relative", "120"));
        Assert.Equal(("parallel", 90.0), (st.Normalization, st.TargetBpm));
        Assert.Empty(st.Warnings);

        // Mixed songs: majority label, with a warning.
        var mixed = Resolve(mini, "x", [new("RA", 0, "major", Normalization: "parallel", TargetBpm: 90), new("RB", 9, "minor", Normalization: "parallel", TargetBpm: 90),
            new("RC", 2, "major", Normalization: "relative", TargetBpm: 90)], null, Env(null, null));
        Assert.Equal(("parallel", 90.0), (mixed.Normalization, mixed.TargetBpm));
        Assert.Single(mixed.Warnings);
        Assert.Contains("mixed", mixed.Warnings[0]);

        // Songs outside the export do not count.
        string db = mini.Build("y", [new("RA", 0, "major", Normalization: "parallel", TargetBpm: 90), new("RZ", 2, "major", Normalization: "relative", TargetBpm: 80)]);
        using var c = PipelineDb.Open(db);
        var subset = ExportSettings.Resolve(c, new HashSet<string>(["RA"], StringComparer.Ordinal), Env(null, null));
        Assert.Equal(("parallel", 90.0), (subset.Normalization, subset.TargetBpm));
        Assert.Empty(subset.Warnings);
    }

    [Fact]
    public void LegacyFixtureKeyThenEnvironmentThenDefault()
    {
        using var mini = new MiniPipeline();
        // Old make-fixture DB: meta 'normalization' only.
        var legacy = Resolve(mini, "l", [new("RA", 0, "major")], new Dictionary<string, string> { ["normalization"] = "parallel" }, Env("relative", "100"));
        Assert.Equal("parallel", legacy.Normalization);
        Assert.Contains("legacy", legacy.NormalizationSource);
        Assert.Equal(100.0, legacy.TargetBpm);          // nothing recorded for the BPM: environment, with a warning
        Assert.Single(legacy.Warnings);
        Assert.Contains("MUSICHISTORY_TARGET_BPM", legacy.Warnings[0]);

        // Nothing recorded at all: environment (warned), else relative / 120 (warned).
        var env = Resolve(mini, "e", [new("RA", 0, "major")], null, Env("parallel", "96.5"));
        Assert.Equal(("parallel", 96.5), (env.Normalization, env.TargetBpm));
        Assert.Equal(2, env.Warnings.Count);
        var none = Resolve(mini, "n", [new("RA", 0, "major")], null, Env(null, "not a number"));
        Assert.Equal(("relative", 120.0, "default", "default"), (none.Normalization, none.TargetBpm, none.NormalizationSource, none.TargetBpmSource));
        Assert.Equal(2, none.Warnings.Count);
    }

    [Fact]
    public void OldPipelineDbWithoutTheMigratedColumnsStillReadsTheMeta()
    {
        using var mini = new MiniPipeline();
        var meta = new Dictionary<string, string> { ["analyze_normalization"] = "parallel", ["analyze_target_bpm"] = "110" };
        var st = Resolve(mini, "o", [new("RA", 0, "major")], meta, Env(null, null),
            "ALTER TABLE song DROP COLUMN normalization", "ALTER TABLE song DROP COLUMN target_bpm");
        Assert.Equal(("parallel", 110.0), (st.Normalization, st.TargetBpm));
        Assert.Empty(st.Warnings);
        // Invalid meta values are ignored with a warning.
        var bad = Resolve(mini, "b", [new("RA", 0, "major")], new Dictionary<string, string> { ["analyze_normalization"] = "sideways", ["analyze_target_bpm"] = "-3" },
            Env(null, null));
        Assert.Equal(("relative", 120.0), (bad.Normalization, bad.TargetBpm));
        Assert.Equal(4, bad.Warnings.Count);
    }
}
