namespace MusicHistory.Influence;

/// <summary>Normal tail probabilities and Benjamini-Hochberg q values.</summary>
internal static class Stats
{
    // Chebyshev coefficients of erfc (Numerical Recipes 3rd ed., §6.2.2), ~1e-16 relative accuracy.
    private static readonly double[] Cof =
    [
        -1.3026537197817094, 6.4196979235649026e-1, 1.9476473204185836e-2, -9.561514786808631e-3,
        -9.46595344482036e-4, 3.66839497852761e-4, 4.2523324806907e-5, -2.0278578112534e-5,
        -1.624290004647e-6, 1.303655835580e-6, 1.5626441722e-8, -8.5238095915e-8,
        6.529054439e-9, 5.059343495e-9, -9.91364156e-10, -2.27365122e-10,
        9.6467911e-11, 2.394038e-12, -6.886027e-12, 8.94487e-13,
        3.13092e-13, -1.12708e-13, 3.81e-16, 7.106e-15,
        -1.523e-15, -9.4e-17, 1.21e-16, -2.8e-17,
    ];

    private static double ErfcCheb(double z)
    {
        double t = 2.0 / (2.0 + z), ty = 4.0 * t - 2.0, d = 0, dd = 0;
        for (int j = Cof.Length - 1; j > 0; j--)
        {
            double tmp = d;
            d = ty * d - dd + Cof[j];
            dd = tmp;
        }
        return t * Math.Exp(-z * z + 0.5 * (Cof[0] + ty * d) - dd);
    }

    public static double Erfc(double x) => x >= 0 ? ErfcCheb(x) : 2.0 - ErfcCheb(-x);

    /// <summary>One-sided upper tail P(N(0,1) >= z).</summary>
    public static double NormalSf(double z) => 0.5 * Erfc(z / Math.Sqrt(2.0));

    /// <summary>
    /// Benjamini-Hochberg: q_(i) = min_{j >= i} p_(j) m / j over the given p values (ties keep
    /// input order, so the result is deterministic).
    /// </summary>
    public static double[] BenjaminiHochberg(IReadOnlyList<double> p)
    {
        int m = p.Count;
        var order = Enumerable.Range(0, m).ToArray();
        Array.Sort(order, (x, y) => p[x] != p[y] ? p[x].CompareTo(p[y]) : x.CompareTo(y));
        var q = new double[m];
        double run = 1.0;
        for (int k = m - 1; k >= 0; k--)
        {
            int i = order[k];
            run = Math.Min(run, p[i] * m / (k + 1));
            q[i] = Math.Min(1.0, run);
        }
        return q;
    }
}
