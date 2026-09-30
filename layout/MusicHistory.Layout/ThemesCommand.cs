using System.Diagnostics;
using System.Globalization;
using System.Text.Json.Nodes;

namespace MusicHistory.Layout;

/// <summary>
/// The <c>themes</c> and <c>themes-demo</c> subcommands (DESIGN.md §12). Exceptions propagate to
/// <see cref="Program.Run"/>, which maps them to the usual exit codes (2 command line, 3 the file
/// breaks §12, 4 GPU, 5 non-finite positions).
/// </summary>
internal static class ThemesCommand
{
    public static readonly string Usage = $"""
        MusicHistory.Layout themes <themes_graph.db> [options] [--out <copy.db>] [--dry-run] [--trace N] [--json] [--quiet]
                            lay the lyric themes graph out (DESIGN.md section 12) and write it in place
        MusicHistory.Layout themes-demo --out <db> [--songs 1012] [--seed 42] [--from <music_graph.db>] [--generated-at <iso>] [--no-layout] [options]
                            write a synthetic section 12 themes graph (then lay it out unless --no-layout)

        Themes layout options (DESIGN.md section 12):
        {ThemesParams.OptionHelp()}

        Run options:
            --out <copy.db>    themes: copy the input to this file and write there (input untouched); themes-demo: the file to write
            --dry-run          compute and report, write nothing
            --trace N          print mean/max move every N iterations
            --json             print the result as one JSON object on stdout
            --quiet            no progress lines
            --songs N          themes-demo: number of songs (1012)
            --from <graph.db>  themes-demo: borrow year and playback columns (MIDI path, excerpt, key, tempo, meter) from a
                               section 10 music graph, song k from its node k; titles and scores stay synthetic
        """;

    private static readonly HashSet<string> Flags = ["dryrun", "json", "quiet", "nolayout", "help", "h"];
    private static readonly HashSet<string> Values = ["out", "songs", "trace", "generatedat", "from"];

    public static int Run(string cmd, string[] args, TextWriter stdout, TextWriter log)
    {
        var p = new ThemesParams();
        var (positional, values, flags) = Parse(args, p);
        if (flags.Contains("help") || flags.Contains("h"))
        {
            stdout.WriteLine(Usage);
            return 0;
        }
        p.Validate();
        return cmd == "themes-demo" ? Demo(positional, values, flags, p, stdout, log) : Layout(positional, values, flags, p, stdout, log);
    }

    private static (List<string> Positional, Dictionary<string, string> Values, HashSet<string> Flags) Parse(string[] args, ThemesParams p)
    {
        var positional = new List<string>();
        var values = new Dictionary<string, string>();
        var flags = new HashSet<string>();
        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i];
            if (!a.StartsWith("--", StringComparison.Ordinal) && !(a.StartsWith('-') && a.Length == 2 && char.IsLetter(a[1])))
            {
                positional.Add(a);
                continue;
            }
            string name = a, value = "";
            bool hasInline = false;
            int eq = a.IndexOf('=');
            if (eq > 0)
            {
                name = a[..eq];
                value = a[(eq + 1)..];
                hasInline = true;
            }
            string key = LayoutParams.Normalize(name);
            int at = i;
            string Next()
            {
                if (hasInline) return value;
                if (at + 1 >= args.Length) throw new UsageException($"{name} needs a value");
                i = at + 1;
                return args[i];
            }
            if (p.TryApply(name, Next)) continue;
            if (Flags.Contains(key))
            {
                flags.Add(key);
                continue;
            }
            if (Values.Contains(key))
            {
                values[key] = Next();
                continue;
            }
            throw new UsageException($"unknown option {name} (see 'themes --help')");
        }
        return (positional, values, flags);
    }

    private static int Int(Dictionary<string, string> values, string name, int fallback) =>
        !values.TryGetValue(name, out var v) ? fallback
        : int.TryParse(v, NumberStyles.Integer, CultureInfo.InvariantCulture, out int x) ? x
        : throw new UsageException($"--{name}: '{v}' is not an integer");

    private static int Layout(List<string> positional, Dictionary<string, string> values, HashSet<string> flags, ThemesParams p,
        TextWriter stdout, TextWriter log)
    {
        if (positional.Count != 1)
            throw new UsageException(positional.Count == 0 ? "themes needs a themes graph database path" : $"unexpected argument '{positional[1]}'");
        string input = Path.GetFullPath(positional[0]);
        if (!File.Exists(input)) throw new UsageException($"themes graph database not found: {input}");
        string target = input;
        if (values.TryGetValue("out", out var outPath))
        {
            target = Path.GetFullPath(outPath);
            if (string.Equals(target, input, StringComparison.OrdinalIgnoreCase))
                throw new UsageException("--out must differ from the input (omit --out to write in place)");
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            File.Copy(input, target, overwrite: true);
        }
        return LayoutFile(target, p, Int(values, "trace", 0), flags.Contains("dryrun"), flags.Contains("quiet"), flags.Contains("json"),
            stdout, log);
    }

    /// <summary>Loads, validates, lays out, measures, writes and reports one themes graph.</summary>
    public static int LayoutFile(string path, ThemesParams p, int trace, bool dryRun, bool quiet, bool json, TextWriter stdout, TextWriter log)
    {
        var wall = Stopwatch.StartNew();
        var g = ThemesInput.Load(path);
        foreach (string w in g.Warnings) log.WriteLine($"warning: {w}");
        double radius = ThemesLayout.RadiusFor(g, p);
        if (!quiet)
            log.WriteLine(FormattableString.Invariant(
                $"themes: {g.Count} songs ({g.TextSource.Count(s => s == "lyrics")} lyrics, {g.TextSource.Count(s => s == "title")} title only), ring radius {radius}, sharpen {p.Sharpen}, repulsion {p.Repulsion}"));
        var r = ThemesLayout.Run(g, p, trace, quiet ? null : log);
        var stats = new JsonObject
        {
            ["ms_per_iteration"] = Math.Round(r.LoopMs / r.Iterations, 5),
            ["batch"] = ThemesLayout.EffectiveBatch(p, g.Count),
            ["final_mean_move"] = r.FinalMeanMove,
            ["final_max_move"] = r.FinalMaxMove,
            ["residual_mean"] = r.ResidualMean,
            ["residual_max"] = r.ResidualMax,
            ["version"] = typeof(Program).Assembly.GetName().Version?.ToString(3),
        };
        foreach (var kv in ThemesLayout.Measure(g, r)) stats[kv.Key] = kv.Value?.DeepClone();
        if (!quiet)
        {
            log.WriteLine(FormattableString.Invariant(
                $"layout: {r.Device}, {r.Iterations} iterations in {r.LoopMs:F1} ms ({r.LoopMs / r.Iterations:F4} ms/iteration); final mean move {r.FinalMeanMove:E2}, max {r.FinalMaxMove:E2}; residual mean {r.ResidualMean:E2}, max {r.ResidualMax:E2}"));
            log.WriteLine(FormattableString.Invariant(
                $"check: displacement from barycentre mean {stats["mean_displacement"]} ({(double)stats["mean_displacement_over_radius"]! * 100:F2} % of R), median {stats["median_displacement"]}, p95 {stats["p95_displacement"]}, max {stats["max_displacement"]}; outside ring {stats["outside_ring"]}; crossed sector {stats["crossed_sector"]}/{stats["crossed_sector_eligible"]}; spacing mean {stats["nn_mean"]}, p5 {stats["nn_p5"]}, min {stats["nn_min"]}; peaked songs {stats["peaked_songs"]} at mean {stats["peaked_mean_distance_to_anchor"]} / max {stats["peaked_max_distance_to_anchor"]} from their anchor; max |y| {stats["max_abs_y"]}; non-finite {stats["non_finite_positions"]}"));
        }
        long? runId = null;
        if (dryRun)
        {
            if (r.NaNCount > 0) throw new LayoutFailedException($"the themes layout produced {r.NaNCount} non-finite position(s)");
        }
        else
        {
            runId = ThemesOutput.Write(path, g, p, r, stats, GraphOutput.UtcNow());
            if (!quiet) log.WriteLine($"wrote anchors, theme_song positions and themes_layout_run {runId} -> {path} ({wall.Elapsed.TotalSeconds:F2} s wall)");
        }
        if (json)
        {
            var o = new JsonObject { ["path"] = path, ["run_id"] = runId, ["device"] = r.Device, ["params"] = p.ToJson(radius), ["stats"] = stats };
            stdout.WriteLine(o.ToJsonString());
        }
        return 0;
    }

    private static int Demo(List<string> positional, Dictionary<string, string> values, HashSet<string> flags, ThemesParams p,
        TextWriter stdout, TextWriter log)
    {
        if (positional.Count > 0) throw new UsageException($"unexpected argument '{positional[0]}' (themes-demo takes --out <db>)");
        string path = Path.GetFullPath(values.GetValueOrDefault("out") ?? throw new UsageException("themes-demo needs --out <db>"));
        int n = Int(values, "songs", 1012);
        if (n < 1) throw new UsageException("--songs must be >= 1");
        string? from = values.GetValueOrDefault("from");
        if (from != null && !File.Exists(from)) throw new UsageException($"--from graph database not found: {from}");
        var sw = Stopwatch.StartNew();
        var s = ThemesDemo.Write(path, n, p.Seed, values.GetValueOrDefault("generatedat") ?? GraphOutput.UtcNow(),
            p.Radius ?? ThemesParams.DefaultRadius, from);
        bool quiet = flags.Contains("quiet");
        if (!quiet)
            log.WriteLine(FormattableString.Invariant(
                $"themes-demo: {s.Songs} songs ({s.Lyrics} lyrics, {s.Titles} title only; {s.Peaked} peaked, {s.Other} other, {s.Mixed} mixed, {s.Flat} flat), top anchors [{string.Join(", ", s.TopCounts)}], singers {string.Join(", ", s.Genders.Select(kv => $"{kv.Key} {kv.Value}"))}{(s.Borrowed ? ", playback borrowed from " + from : "")} -> {path} ({sw.ElapsedMilliseconds} ms)"));
        if (flags.Contains("nolayout")) return 0;
        return LayoutFile(path, p, Int(values, "trace", 0), false, quiet, flags.Contains("json"), stdout, log);
    }
}
