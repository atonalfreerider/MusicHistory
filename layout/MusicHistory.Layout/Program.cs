using System.Diagnostics;
using System.Globalization;
using System.Text.Json.Nodes;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Layout;

/// <summary>
/// MusicHistory layout stage (DESIGN.md §9), a new version of GPU-FDG: lays the influence forest
/// of a §10 graph database out on the GPU with release time pinned to one axis, and writes the
/// positions, node_layout_metadata and a layout_run row back into the same file.
///
/// Exit codes: 0 ok, 1 unexpected error, 2 bad command line, 3 the graph breaks a §10 invariant,
/// 4 no usable GPU / GPU failure, 5 the layout produced non-finite positions (nothing written),
/// 6 <c>check</c> found a problem. GPU-FDG ended with Environment.Exit(0), which turned every
/// crash into success; here every failure reaches the exit code.
/// </summary>
internal static class Program
{
    private static readonly string Usage = $"""
        MusicHistory.Layout <graph.db> [options]              lay the graph out in place (same as 'run')
        MusicHistory.Layout run <graph.db> [options] [--out <copy.db>] [--dry-run] [--trace N] [--json]
        MusicHistory.Layout demo --out <db> [--nodes 1000] [--seed 42] [--roots 0.04] [--generated-at <iso>] [--no-layout] [options]
                            write a synthetic DESIGN.md section 10 graph (then lay it out unless --no-layout)
        MusicHistory.Layout validate <graph.db> [--lenient]  check the section 10 invariants only
        MusicHistory.Layout check <graph.db> [--json]        measure a laid-out graph (time axis, NaN, clustering)
        MusicHistory.Layout devices                          list DirectX 12 devices
        MusicHistory.Layout themes <themes_graph.db> [options]   lay the lyric themes graph out (section 12; 'themes --help')
        MusicHistory.Layout themes-demo --out <db> [--songs 1012] [--from <music_graph.db>]   synthetic section 12 themes graph

        Layout options (DESIGN.md section 9):
        {LayoutParams.OptionHelp()}

        Run options:
            --out <copy.db>    copy the input to this file and write the layout there (input untouched)
            --dry-run          compute and report, write nothing
            --trace N          print mean/max move every N iterations
            --json             print the result as one JSON object on stdout
            --quiet            no progress lines
        """;

    private static readonly HashSet<string> ProgramFlags = ["dryrun", "json", "quiet", "nolayout", "help", "h"];

    public static int Main(string[] args) => Run(args, Console.Out, Console.Error);

    public static int Run(string[] args, TextWriter stdout, TextWriter log)
    {
        CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture;
        CultureInfo.CurrentCulture = CultureInfo.InvariantCulture;
        try
        {
            if (args.Length == 0)
            {
                log.WriteLine(Usage);
                return 2;
            }
            var cmd = args[0].ToLowerInvariant();
            if (cmd is "themes" or "themes-demo") return ThemesCommand.Run(cmd, args[1..], stdout, log);
            string[] rest = cmd is "run" or "demo" or "validate" or "check" or "devices" or "help" or "-h" or "--help" ? args[1..] : args;
            if (cmd is "help" or "-h" or "--help")
            {
                stdout.WriteLine(Usage);
                return 0;
            }
            var p = new LayoutParams();
            var opts = Parse(rest, p);
            if (opts.Flags.Contains("help") || opts.Flags.Contains("h"))
            {
                stdout.WriteLine(Usage);
                return 0;
            }
            p.Validate();
            return cmd switch
            {
                "demo" => Demo(opts, p, stdout, log),
                "validate" => Validate(opts, p, stdout),
                "check" => Check(opts, stdout),
                "devices" => Devices(stdout),
                _ => Layout(opts, p, stdout, log),
            };
        }
        catch (UsageException ex)
        {
            log.WriteLine($"error: {ex.Message}\n\n{Usage}");
            return 2;
        }
        catch (InvalidGraphException ex)
        {
            log.WriteLine($"invalid graph: {ex.Message}");
            return 3;
        }
        catch (GpuException ex)
        {
            log.WriteLine($"GPU error: {ex.Message}");
            return 4;
        }
        catch (LayoutFailedException ex)
        {
            log.WriteLine($"layout failed: {ex.Message}");
            return 5;
        }
        catch (SqliteException ex)
        {
            log.WriteLine($"SQLite error: {ex.Message}");
            return 1;
        }
        catch (Exception ex)
        {
            log.WriteLine($"unexpected error: {ex}");
            return 1;
        }
    }

    private sealed class Options
    {
        public List<string> Positional { get; } = [];
        public Dictionary<string, string> Values { get; } = [];
        public HashSet<string> Flags { get; } = [];
        public string? Get(string name) => Values.TryGetValue(name, out var v) ? v : null;

        public int Int(string name, int fallback) =>
            Get(name) is not { } v ? fallback
            : int.TryParse(v, NumberStyles.Integer, CultureInfo.InvariantCulture, out int x) ? x
            : throw new UsageException($"--{name}: '{v}' is not an integer");
    }

    /// <summary>Layout options go into <paramref name="p"/>; the rest are program options. Accepts --name value and --name=value.</summary>
    private static Options Parse(string[] args, LayoutParams p)
    {
        var o = new Options();
        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i];
            if (!a.StartsWith("--", StringComparison.Ordinal) && !(a.StartsWith('-') && a.Length == 2 && char.IsLetter(a[1])))
            {
                o.Positional.Add(a);
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
            if (ProgramFlags.Contains(key))
            {
                o.Flags.Add(key);
                continue;
            }
            if (key is "out" or "nodes" or "seed" or "roots" or "trace" or "generatedat")
            {
                o.Values[key] = Next();
                continue;
            }
            throw new UsageException($"unknown option {name}");
        }
        return o;
    }

    private static string OnePath(Options o, string what)
    {
        if (o.Positional.Count != 1)
            throw new UsageException(o.Positional.Count == 0 ? $"{what} needs a graph database path" : $"unexpected argument '{o.Positional[1]}'");
        return Path.GetFullPath(o.Positional[0]);
    }

    private static int Layout(Options o, LayoutParams p, TextWriter stdout, TextWriter log)
    {
        string input = OnePath(o, "run");
        if (!File.Exists(input)) throw new UsageException($"graph database not found: {input}");
        string target = input;
        if (o.Get("out") is { } outPath)
        {
            target = Path.GetFullPath(outPath);
            if (string.Equals(target, input, StringComparison.OrdinalIgnoreCase))
                throw new UsageException("--out must differ from the input (omit --out to write in place)");
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            File.Copy(input, target, overwrite: true);
        }
        var result = LayoutFile(target, p, o.Int("trace", 0), o.Flags.Contains("dryrun"), o.Flags.Contains("quiet"), o.Flags.Contains("json"),
            stdout, log);
        return result;
    }

    /// <summary>Loads, validates, lays out, writes and reports one graph database.</summary>
    private static int LayoutFile(string path, LayoutParams p, int trace, bool dryRun, bool quiet, bool json, TextWriter stdout, TextWriter log)
    {
        var wall = Stopwatch.StartNew();
        var g = GraphInput.Load(path, p.Lenient);
        foreach (string w in g.Warnings) log.WriteLine($"warning: {w}");
        if (!quiet)
            log.WriteLine(FormattableString.Invariant(
                $"graph: {g.Count} nodes, {g.Edges.Length} edges ({g.TreeEdges} tree, {g.Edges.Length - g.TreeEdges} secondary), {g.Roots} roots, time {g.MinTime:F2}..{g.MaxTime:F2}"));
        var r = TemporalGraph.Run(g, p, trace, quiet ? null : log);
        var stats = new JsonObject
        {
            ["nodes"] = g.Count,
            ["edges"] = g.Edges.Length,
            ["tree_edges"] = g.TreeEdges,
            ["roots"] = g.Roots,
            ["max_descendants"] = g.Descendants.Max(),
            ["pinned_node"] = r.PinnedNode >= 0 ? r.PinnedNode + 1 : null,
            ["ms_per_iteration"] = Math.Round(r.LoopMs / r.Iterations, 5),
            ["batch"] = TemporalGraph.EffectiveBatch(p, g.Count, g.Edges.Length),
            ["final_mean_move"] = r.FinalMeanMove,
            ["final_max_move"] = r.FinalMaxMove,
            ["residual_mean"] = r.ResidualMean,
            ["residual_max"] = r.ResidualMax,
            ["version"] = typeof(Program).Assembly.GetName().Version?.ToString(3),
        };
        if (!quiet)
            log.WriteLine(FormattableString.Invariant(
                $"layout: {r.Device}, {r.Iterations} iterations in {r.LoopMs:F1} ms ({r.LoopMs / r.Iterations:F4} ms/iteration); final mean move {r.FinalMeanMove:E2}, max {r.FinalMaxMove:E2}; residual mean {r.ResidualMean:E2}, max {r.ResidualMax:E2}"));
        long? runId = null;
        if (dryRun)
        {
            if (r.NaNCount > 0) throw new LayoutFailedException($"the layout produced {r.NaNCount} non-finite position(s)");
        }
        else
        {
            runId = GraphOutput.Write(path, g, p, r, stats, GraphOutput.UtcNow());
            if (!quiet) log.WriteLine($"wrote positions, node_layout_metadata and layout_run {runId} -> {path} ({wall.Elapsed.TotalSeconds:F2} s wall)");
        }
        if (json)
        {
            var o = runId != null ? LayoutQuality.Measure(LayoutQuality.Load(path)) : stats;
            stdout.WriteLine(o.ToJsonString());
        }
        else if (!quiet && runId != null)
        {
            var q = LayoutQuality.Measure(LayoutQuality.Load(path));
            log.WriteLine(FormattableString.Invariant(
                $"check: max time-axis error {q["max_time_axis_error"]}, non-finite {q["non_finite_positions"]}, mean free distance tree {q["mean_free_distance_tree_edges"]} / secondary {q["mean_free_distance_secondary_edges"]} / random {q["mean_free_distance_random_pairs"]}, radius p95 {q["free_radius_p95"]}"));
        }
        return 0;
    }

    private static int Demo(Options o, LayoutParams p, TextWriter stdout, TextWriter log)
    {
        if (o.Positional.Count > 0) throw new UsageException($"unexpected argument '{o.Positional[0]}' (demo takes --out <db>)");
        string path = Path.GetFullPath(o.Get("out") ?? throw new UsageException("demo needs --out <db>"));
        int n = o.Int("nodes", 1000);
        int seed = o.Int("seed", 42);
        if (n < 1) throw new UsageException("--nodes must be >= 1");
        var sw = Stopwatch.StartNew();
        double roots = o.Get("roots") is { } rv
            ? double.TryParse(rv, NumberStyles.Float, CultureInfo.InvariantCulture, out double x) ? x : throw new UsageException($"--roots: '{rv}' is not a number")
            : 0.04;
        var s = DemoGraph.Write(path, n, seed, o.Get("generatedat") ?? GraphOutput.UtcNow(), roots);
        bool quiet = o.Flags.Contains("quiet");
        if (!quiet)
            log.WriteLine(FormattableString.Invariant(
                $"demo: {s.Nodes} songs {s.MinTime:F2}..{s.MaxTime:F2}, {s.Edges} edges ({s.TreeEdges} tree, {s.SecondaryEdges} secondary), {s.Roots} roots, largest subtree {s.MaxDescendants}, depth {s.MaxDepth} -> {path} ({sw.ElapsedMilliseconds} ms)"));
        if (o.Flags.Contains("nolayout")) return 0;
        return LayoutFile(path, p, o.Int("trace", 0), false, quiet, o.Flags.Contains("json"), stdout, log);
    }

    private static int Validate(Options o, LayoutParams p, TextWriter stdout)
    {
        var g = GraphInput.Load(OnePath(o, "validate"), p.Lenient);
        foreach (string w in g.Warnings) stdout.WriteLine($"warning: {w}");
        stdout.WriteLine(FormattableString.Invariant(
            $"ok: {g.Count} nodes, {g.Edges.Length} edges ({g.TreeEdges} tree), {g.Roots} roots, max depth {g.Depth.Max()}, largest subtree {g.Descendants.Max()}, time {g.MinTime:F3}..{g.MaxTime:F3}"));
        return 0;
    }

    private static int Check(Options o, TextWriter stdout)
    {
        var q = LayoutQuality.Measure(LayoutQuality.Load(OnePath(o, "check")));
        stdout.WriteLine(o.Flags.Contains("json") ? q.ToJsonString() : LayoutQuality.Format(q));
        bool ok = q["non_finite_positions"]!.GetValue<int>() == 0 && q["max_time_axis_error"]!.GetValue<double>() == 0;
        return ok ? 0 : 6;
    }

    private static int Devices(TextWriter stdout)
    {
        try
        {
            foreach (string d in Gpu.List()) stdout.WriteLine(d);
            stdout.WriteLine($"default: {Gpu.Describe(Gpu.Select("default"))}");
        }
        catch (Exception ex) when (ex is not GpuException)
        {
            throw new GpuException($"cannot enumerate DirectX 12 devices ({ex.Message})", ex);
        }
        return 0;
    }
}
