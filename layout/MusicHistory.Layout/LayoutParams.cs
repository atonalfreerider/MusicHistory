using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace MusicHistory.Layout;

internal enum TimeAxis { Y, Z }

internal enum TimeDirection { Up, Down }

/// <summary>Which edge column scales a spring: the influence stage's spring suggestion, the raw similarity, or neither.</summary>
internal enum SpringWeight { Weight, Similarity, None }

/// <summary>
/// Every knob of the temporal layout, with the defaults of DESIGN.md §9. Forces (per node i):
/// <code>
///   spring      F += spring · kindSpring · w_e · (|d| − restLength) · d/|d|      (d = p_j − p_i, both endpoints)
///   repulsion   F += repulsion · m_i · m_j · d / (|d|² + softening)^{3/2}         (all pairs, 3-D distance)
///   centering   F_free −= centering · m_i · p_free
///   edge clear. F += edgeRepulsion · away / (dist² + edgeClearance²)             (optional, O(N·E))
///   mass        m = 1 + log2(1 + descendants) [+ massOutDegree · log2(1 + out_degree)]
///   step        v = damping · v + F / (m + spring · Σ kindSpring·w_e + 1),  |v| ≤ T(t) = T0 (1 − t)² + Tmin
/// </code>
/// Only the two free axes integrate; the time coordinate is fixed at
/// <c>sign · (time_value − min_time) · yearScale</c>.
/// </summary>
internal sealed class LayoutParams
{
    public int Iterations = 1500;
    public double YearScale = 2.0;
    public TimeAxis Axis = TimeAxis.Y;
    public TimeDirection Direction = TimeDirection.Up;

    public float Spring = 0.08f;
    public float TreeSpring = 1.0f;
    public float SecondarySpring = 0.25f;
    public SpringWeight SpringWeight = SpringWeight.Weight;
    public float RestLength = 1.5f;
    public float Repulsion = 1.0f;
    public float Softening = 0.05f;
    public float Centering = 0.02f;
    public float Damping = 0.8f;
    public float EdgeRepulsion = 0f;
    public float EdgeClearance = 0.5f;
    public float MassOutDegree = 0f;

    public float StartTemperature = 3.0f;
    public float MinTemperature = 0.01f;

    public float RootSpacing = 6.0f;
    public float ChildSpacing = 2.0f;
    public bool PinLargestRoot = true;
    public double RadiusScale = 0.2;

    /// <summary>Iterations recorded into one GPU command list (one submission and one CPU wait per batch).</summary>
    public int Batch = 100;
    public string Device = "default";
    /// <summary>Downgrade mismatched derived tree columns (root, depth, descendants) to warnings.</summary>
    public bool Lenient;

    public float TimeSign => Direction == TimeDirection.Up ? 1f : -1f;

    public void Validate()
    {
        static void Check(bool ok, string msg)
        {
            if (!ok) throw new UsageException(msg);
        }
        Check(Iterations >= 1, "--iterations must be >= 1");
        Check(double.IsFinite(YearScale) && YearScale > 0, "--yearScale must be > 0");
        foreach (var (name, v) in new (string, float)[]
                 {
                     ("spring", Spring), ("treeSpring", TreeSpring), ("secondarySpring", SecondarySpring),
                     ("restLength", RestLength), ("repulsion", Repulsion), ("softening", Softening), ("centering", Centering),
                     ("edgeRepulsion", EdgeRepulsion), ("edgeClearance", EdgeClearance), ("massOutDegree", MassOutDegree),
                     ("rootSpacing", RootSpacing), ("childSpacing", ChildSpacing), ("radiusScale", (float)RadiusScale),
                 })
            Check(float.IsFinite(v) && v >= 0, $"--{name} must be a finite number >= 0");
        Check(Softening > 0, "--softening must be > 0 (it keeps coincident nodes finite)");
        Check(float.IsFinite(Damping) && Damping >= 0 && Damping < 1, "--damping must be in [0, 1)");
        Check(float.IsFinite(StartTemperature) && StartTemperature > 0, "--startTemperature must be > 0");
        Check(float.IsFinite(MinTemperature) && MinTemperature > 0 && MinTemperature <= StartTemperature,
            "--minTemperature must be in (0, startTemperature]");
        Check(Batch >= 1, "--batch must be >= 1");
    }

    /// <summary>Quadratic cooling of the per-iteration step limit, from T0 + Tmin down to Tmin.</summary>
    public float Temperature(int iteration)
    {
        float t = iteration / (float)Iterations;
        return StartTemperature * (1 - t) * (1 - t) + MinTemperature;
    }

    public JsonObject ToJson() => new()
    {
        ["iterations"] = Iterations,
        ["yearScale"] = YearScale,
        ["timeAxis"] = Axis == TimeAxis.Y ? "y" : "z",
        ["timeDirection"] = Direction == TimeDirection.Up ? "up" : "down",
        ["spring"] = Spring,
        ["treeSpring"] = TreeSpring,
        ["secondarySpring"] = SecondarySpring,
        ["springWeight"] = SpringWeight.ToString().ToLowerInvariant(),
        ["restLength"] = RestLength,
        ["repulsion"] = Repulsion,
        ["softening"] = Softening,
        ["centering"] = Centering,
        ["damping"] = Damping,
        ["edgeRepulsion"] = EdgeRepulsion,
        ["edgeClearance"] = EdgeClearance,
        ["massOutDegree"] = MassOutDegree,
        ["startTemperature"] = StartTemperature,
        ["minTemperature"] = MinTemperature,
        ["rootSpacing"] = RootSpacing,
        ["childSpacing"] = ChildSpacing,
        ["pinLargestRoot"] = PinLargestRoot,
        ["radiusScale"] = RadiusScale,
        ["batch"] = Batch,
    };

    // ------------------------------------------------------------------ command-line options

    private sealed record Opt(string Name, string Arg, string Help, Action<LayoutParams, string> Set);

    private static readonly Opt[] Options =
    [
        new("iterations", "N", "force iterations (1500)", (p, v) => p.Iterations = Int(v, "iterations")),
        new("yearScale", "U", "world units per year on the time axis (2)", (p, v) => p.YearScale = Dbl(v, "yearScale")),
        new("timeAxis", "y|z", "axis that carries time (y: a trunk; z: a fly-through corridor)",
            (p, v) => p.Axis = v.ToLowerInvariant() switch
            {
                "y" => TimeAxis.Y,
                "z" => TimeAxis.Z,
                _ => throw new UsageException($"--timeAxis must be y or z, not '{v}'"),
            }),
        new("timeDirection", "up|down", "up: oldest at 0 and newer songs at +axis; down: newer at -axis",
            (p, v) => p.Direction = v.ToLowerInvariant() switch
            {
                "up" => TimeDirection.Up,
                "down" => TimeDirection.Down,
                _ => throw new UsageException($"--timeDirection must be up or down, not '{v}'"),
            }),
        new("spring", "K", "global spring stiffness k (0.08)", (p, v) => p.Spring = Flt(v, "spring")),
        new("treeSpring", "W", "multiplier of tree edges (1.0)", (p, v) => p.TreeSpring = Flt(v, "treeSpring")),
        new("secondarySpring", "W", "multiplier of secondary edges (0.25)", (p, v) => p.SecondarySpring = Flt(v, "secondarySpring")),
        new("springWeight", "weight|similarity|none", "edge column that scales each spring (weight)",
            (p, v) => p.SpringWeight = v.ToLowerInvariant() switch
            {
                "weight" => SpringWeight.Weight,
                "similarity" => SpringWeight.Similarity,
                "none" => SpringWeight.None,
                _ => throw new UsageException($"--springWeight must be weight, similarity or none, not '{v}'"),
            }),
        new("restLength", "L", "spring rest length (1.5)", (p, v) => p.RestLength = Flt(v, "restLength")),
        new("repulsion", "R", "node-node repulsion, scaled by m_i*m_j (1.0)", (p, v) => p.Repulsion = Flt(v, "repulsion")),
        new("softening", "S", "added to d^2 in the repulsion (0.05)", (p, v) => p.Softening = Flt(v, "softening")),
        new("centering", "C", "pull toward the time axis, scaled by m_i (0.02)", (p, v) => p.Centering = Flt(v, "centering")),
        new("damping", "D", "velocity damping per iteration (0.8)", (p, v) => p.Damping = Flt(v, "damping")),
        new("edgeRepulsion", "E", "node-edge clearance force, O(N*E); 0 = off (0)", (p, v) => p.EdgeRepulsion = Flt(v, "edgeRepulsion")),
        new("edgeClearance", "C", "softening distance of the edge clearance (0.5)", (p, v) => p.EdgeClearance = Flt(v, "edgeClearance")),
        new("massOutDegree", "A", "extra mass A*log2(1+out_degree) (0)", (p, v) => p.MassOutDegree = Flt(v, "massOutDegree")),
        new("startTemperature", "T", "initial step limit (3.0)", (p, v) => p.StartTemperature = Flt(v, "startTemperature")),
        new("minTemperature", "T", "final step limit (0.01)", (p, v) => p.MinTemperature = Flt(v, "minTemperature")),
        new("rootSpacing", "U", "arc length between roots on the start ring (6)", (p, v) => p.RootSpacing = Flt(v, "rootSpacing")),
        new("childSpacing", "U", "start offset of children from their parent (2)", (p, v) => p.ChildSpacing = Flt(v, "childSpacing")),
        new("pinLargestRoot", "true|false", "hold the root of the largest tree on the time axis (true)",
            (p, v) => p.PinLargestRoot = Bool(v, "pinLargestRoot")),
        new("radiusScale", "U", "display_radius = radiusScale * sqrt(1 + descendants) (0.2)", (p, v) => p.RadiusScale = Dbl(v, "radiusScale")),
        new("batch", "N", "iterations per GPU submission (100)", (p, v) => p.Batch = Int(v, "batch")),
        new("device", "default|warp|<index>|<name>", "DirectX 12 device (default)", (p, v) => p.Device = v),
        new("lenient", "", "warn instead of failing on mismatched tree_root_node / tree_depth / descendants",
            (p, _) => p.Lenient = true),
    ];

    public static string OptionHelp()
    {
        var lines = Options.Select(o => ($"--{o.Name}{(o.Arg.Length > 0 ? " " + o.Arg : "")}", o.Help)).ToList();
        int w = lines.Max(l => l.Item1.Length) + 2;
        return string.Join('\n', lines.Select(l => "    " + l.Item1.PadRight(w) + l.Help));
    }

    /// <summary>
    /// Applies one option if it is a layout option. Names are matched case-insensitively and
    /// with dashes ignored, so <c>--yearScale</c>, <c>--yearscale</c> and <c>--year-scale</c> are one option.
    /// </summary>
    public bool TryApply(string name, Func<string> value)
    {
        string key = Normalize(name);
        var opt = Options.FirstOrDefault(o => Normalize(o.Name) == key);
        if (opt == null) return false;
        opt.Set(this, opt.Arg.Length == 0 ? "" : value());
        return true;
    }

    public static bool IsFlag(string name) => Options.Any(o => Normalize(o.Name) == Normalize(name) && o.Arg.Length == 0);

    public static string Normalize(string name) => name.TrimStart('-').Replace("-", "", StringComparison.Ordinal).ToLowerInvariant();

    private static int Int(string v, string name) =>
        int.TryParse(v, NumberStyles.Integer, CultureInfo.InvariantCulture, out int x) ? x : throw new UsageException($"--{name}: '{v}' is not an integer");

    private static float Flt(string v, string name) =>
        float.TryParse(v, NumberStyles.Float, CultureInfo.InvariantCulture, out float x) ? x : throw new UsageException($"--{name}: '{v}' is not a number");

    private static double Dbl(string v, string name) =>
        double.TryParse(v, NumberStyles.Float, CultureInfo.InvariantCulture, out double x) ? x : throw new UsageException($"--{name}: '{v}' is not a number");

    private static bool Bool(string v, string name) => v.ToLowerInvariant() switch
    {
        "true" or "1" or "yes" => true,
        "false" or "0" or "no" => false,
        _ => throw new UsageException($"--{name} must be true or false, not '{v}'"),
    };

    public static string Json(JsonNode node) => node.ToJsonString(new JsonSerializerOptions { WriteIndented = false });
}

/// <summary>Bad command line (exit code 2).</summary>
internal sealed class UsageException(string message) : Exception(message);

/// <summary>The input graph breaks a DESIGN.md §10 invariant (exit code 3).</summary>
internal sealed class InvalidGraphException(string message, IReadOnlyList<string> problems) : Exception(message)
{
    public IReadOnlyList<string> Problems { get; } = problems;
}

/// <summary>No usable DirectX 12 device, or the GPU failed (exit code 4).</summary>
internal sealed class GpuException(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>The layout produced unusable positions; nothing was written (exit code 5).</summary>
internal sealed class LayoutFailedException(string message) : Exception(message);
