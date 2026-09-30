using System.Globalization;
using System.Text.Json.Nodes;

namespace MusicHistory.Layout;

/// <summary>
/// Every knob of the lyric-themes layout (DESIGN.md §12). Ten anchors are pinned on a ring of
/// radius R in the x–z plane; each song i is a free point with, per iteration:
/// <code>
///   weights     w_ik = s_ik^γ / Σ_k s_ik^γ                                (γ = sharpen; Σ_k w_ik = 1)
///   springs     F += spring · Σ_k w_ik · (A_k − p_i)                       (zero rest length, all ten anchors)
///   repulsion   F += repulsion · Σ_j d / (|d|² + softening)^{3/2}          (d = p_i − p_j, all songs)
///               minus the value at |d| = cutoff, and 0 beyond it (cutoff > 0; 0 = unlimited range)
///   plane       y = 0 (slab = 0); or F_y −= spring · flatten · y and |y| ≤ slab/2
///   step        v = damping · v + F / (spring + 2 · repulsion · Σ_j (|d|² + softening)^{-3/2}),  |v| ≤ T(t) = T0 (1 − t)² + Tmin
/// </code>
/// Because Σ_k w_ik = 1 the springs add up to <c>spring · (b_i − p_i)</c> with b_i = Σ_k w_ik A_k,
/// the weighted barycentre, so without repulsion every song settles exactly on b_i (a song that is
/// all one theme sits on that anchor). The repulsion spreads songs that share a barycentre into a
/// cloud; its default is small enough that the mean displacement from b_i stays a few percent
/// of R. The per-song step is a Jacobi (diagonal) preconditioner: it changes how fast a song
/// moves, never where it settles.
/// </summary>
internal sealed class ThemesParams
{
    public int Iterations = 1500;
    /// <summary>Ring radius; null = themes_meta.ring_radius of the input, else <see cref="DefaultRadius"/>.</summary>
    public double? Radius;
    public const double DefaultRadius = 40.0;
    public double Sharpen = 2.0;
    public float Spring = 1.0f;
    public float Repulsion = 0.35f;
    public float Softening = 0.25f;
    public float Cutoff = 0f;
    public float Damping = 0.7f;
    public float StartTemperature = 2.0f;
    public float MinTemperature = 0.002f;
    /// <summary>Radius of the seeded start offset around each barycentre (breaks ties between identical score vectors).</summary>
    public float Jitter = 0.05f;
    public int Seed = 42;
    /// <summary>0: songs stay exactly in the plane y = 0. &gt; 0: a slab of this thickness (|y| ≤ slab/2).</summary>
    public float Slab = 0f;
    /// <summary>Vertical spring = spring · flatten (slab mode only).</summary>
    public float Flatten = 2f;
    public int Batch = 100;
    public string Device = "default";

    public void Validate()
    {
        static void Check(bool ok, string msg)
        {
            if (!ok) throw new UsageException(msg);
        }
        Check(Iterations >= 1, "--iterations must be >= 1");
        Check(Radius is null || (double.IsFinite(Radius.Value) && Radius.Value > 0), "--radius must be > 0");
        Check(double.IsFinite(Sharpen) && Sharpen > 0 && Sharpen <= 16, "--sharpen must be in (0, 16]");
        Check(float.IsFinite(Spring) && Spring > 0, "--spring must be > 0");
        foreach (var (name, v) in new (string, float)[]
                 {
                     ("repulsion", Repulsion), ("cutoff", Cutoff), ("jitter", Jitter), ("slab", Slab), ("flatten", Flatten),
                 })
            Check(float.IsFinite(v) && v >= 0, $"--{name} must be a finite number >= 0");
        Check(float.IsFinite(Softening) && Softening > 0, "--softening must be > 0 (it keeps coincident songs finite)");
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

    public JsonObject ToJson(double radius) => new()
    {
        ["iterations"] = Iterations,
        ["radius"] = radius,
        ["sharpen"] = Sharpen,
        ["spring"] = Spring,
        ["repulsion"] = Repulsion,
        ["softening"] = Softening,
        ["cutoff"] = Cutoff,
        ["damping"] = Damping,
        ["startTemperature"] = StartTemperature,
        ["minTemperature"] = MinTemperature,
        ["jitter"] = Jitter,
        ["seed"] = Seed,
        ["slab"] = Slab,
        ["flatten"] = Flatten,
        ["batch"] = Batch,
        ["anchor_plane"] = "xz",
        ["angle_unit"] = "degrees",
    };

    // ------------------------------------------------------------------ command-line options

    private sealed record Opt(string Name, string Arg, string Help, Action<ThemesParams, string> Set);

    private static readonly Opt[] Options =
    [
        new("iterations", "N", "force iterations (1500)", (p, v) => p.Iterations = Int(v, "iterations")),
        new("radius", "R", "ring radius of the ten anchors (themes_meta.ring_radius, else 40)", (p, v) => p.Radius = Dbl(v, "radius")),
        new("sharpen", "G", "spring stiffness exponent: w_k = score_k^G, normalized per song (2)", (p, v) => p.Sharpen = Dbl(v, "sharpen")),
        new("spring", "K", "total spring stiffness per song (1.0)", (p, v) => p.Spring = Flt(v, "spring")),
        new("repulsion", "Q", "song-song repulsion; 0 = every song exactly on its barycentre (0.35)", (p, v) => p.Repulsion = Flt(v, "repulsion")),
        new("softening", "S", "added to d^2 in the repulsion (0.25)", (p, v) => p.Softening = Flt(v, "softening")),
        new("cutoff", "D", "repulsion range, force shifted to 0 at D; 0 = unlimited (0)", (p, v) => p.Cutoff = Flt(v, "cutoff")),
        new("damping", "D", "velocity damping per iteration (0.7)", (p, v) => p.Damping = Flt(v, "damping")),
        new("startTemperature", "T", "initial step limit (2.0)", (p, v) => p.StartTemperature = Flt(v, "startTemperature")),
        new("minTemperature", "T", "final step limit (0.002)", (p, v) => p.MinTemperature = Flt(v, "minTemperature")),
        new("jitter", "J", "seeded start offset around each barycentre (0.05)", (p, v) => p.Jitter = Flt(v, "jitter")),
        new("seed", "N", "seed of the start offsets (42)", (p, v) => p.Seed = Int(v, "seed")),
        new("slab", "H", "0: songs in the plane y = 0; >0: a slab |y| <= H/2 (0)", (p, v) => p.Slab = Flt(v, "slab")),
        new("flatten", "F", "vertical spring = spring*F in a slab (2)", (p, v) => p.Flatten = Flt(v, "flatten")),
        new("batch", "N", "iterations per GPU submission (100)", (p, v) => p.Batch = Int(v, "batch")),
        new("device", "default|warp|<index>|<name>", "DirectX 12 device (default)", (p, v) => p.Device = v),
    ];

    public static string OptionHelp()
    {
        var lines = Options.Select(o => ($"--{o.Name}{(o.Arg.Length > 0 ? " " + o.Arg : "")}", o.Help)).ToList();
        int w = lines.Max(l => l.Item1.Length) + 2;
        return string.Join('\n', lines.Select(l => "    " + l.Item1.PadRight(w) + l.Help));
    }

    /// <summary>Applies one option if it is a themes layout option (case-insensitive, dashes ignored).</summary>
    public bool TryApply(string name, Func<string> value)
    {
        string key = LayoutParams.Normalize(name);
        var opt = Options.FirstOrDefault(o => LayoutParams.Normalize(o.Name) == key);
        if (opt == null) return false;
        opt.Set(this, opt.Arg.Length == 0 ? "" : value());
        return true;
    }

    private static int Int(string v, string name) =>
        int.TryParse(v, NumberStyles.Integer, CultureInfo.InvariantCulture, out int x) ? x : throw new UsageException($"--{name}: '{v}' is not an integer");

    private static float Flt(string v, string name) =>
        float.TryParse(v, NumberStyles.Float, CultureInfo.InvariantCulture, out float x) ? x : throw new UsageException($"--{name}: '{v}' is not a number");

    private static double Dbl(string v, string name) =>
        double.TryParse(v, NumberStyles.Float, CultureInfo.InvariantCulture, out double x) ? x : throw new UsageException($"--{name}: '{v}' is not a number");
}
