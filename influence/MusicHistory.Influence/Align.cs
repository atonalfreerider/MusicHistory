namespace MusicHistory.Influence;

/// <summary>A local alignment hit: score (scaled points) and inclusive 0-based token spans.</summary>
internal struct Hit
{
    public int Score;
    public int A0, A1, B0, B1;
}

/// <summary>Integer substitution tables and gap costs of DESIGN.md §8.5, scaled by <see cref="Params.Scale"/>.</summary>
internal sealed class Scoring
{
    public readonly int[] MelSub;       // 48 x 48 over pc * 4 + metric class
    public readonly int[] ChordSub;     // 36 x 36 over L1 tokens
    public readonly int MelOpen, MelExt, Cons, ChordOpen, ChordExt, MinHit;

    public Scoring(Params p)
    {
        MelSub = new int[48 * 48];
        for (int a = 0; a < 48; a++)
            for (int b = 0; b < 48; b++)
            {
                int d = Math.Abs((a >> 2) - (b >> 2));
                d = Math.Min(d, 12 - d);
                double s = d == 0 ? p.MelMatch : d <= 2 ? p.MelNear : p.MelMismatch;
                if ((a & 3) != (b & 3)) s *= p.MelOffMetric;
                MelSub[a * 48 + b] = p.Pts(s);
            }
        ChordSub = new int[36 * 36];
        for (int a = 0; a < 36; a++)
            for (int b = 0; b < 36; b++)
            {
                int pa = Triad(a), pb = Triad(b);
                double j = (double)System.Numerics.BitOperations.PopCount((uint)(pa & pb)) /
                           System.Numerics.BitOperations.PopCount((uint)(pa | pb));
                double s = 3 * j - 1;
                if (a / 3 == b / 3 && a != b) s += p.ChordSameRoot;
                ChordSub[a * 36 + b] = p.Pts(s);
            }
        MelOpen = p.Pts(p.MelGapOpen);
        MelExt = p.Pts(p.MelGapExtend);
        Cons = p.Pts(p.Consolidation);
        ChordOpen = p.Pts(p.ChordGapOpen);
        ChordExt = p.Pts(p.ChordGapExtend);
        MinHit = p.Pts(p.MinHit);
    }

    /// <summary>Pitch-class set of an L1 token as a 12-bit mask (maj 0-4-7, min 0-3-7, dim 0-3-6).</summary>
    public static int Triad(int token)
    {
        int root = token / 3, q = token % 3;
        int third = q == 0 ? 4 : 3, fifth = q == 2 ? 6 : 7;
        return (1 << root) | (1 << ((root + third) % 12)) | (1 << ((root + fifth) % 12));
    }
}

/// <summary>Kernel parameters: scaled integer substitution table and gap / consolidation costs.</summary>
internal readonly struct AlignParams
{
    public readonly int[] Sub;
    public readonly int Alpha, Open, Ext, Cons, MinHit;

    public AlignParams(int[] sub, int alpha, int open, int ext, int cons, int minHit)
    {
        Sub = sub;
        Alpha = alpha;
        Open = open;
        Ext = ext;
        Cons = cons;
        MinHit = minHit;
    }
}

/// <summary>Best cell of a local alignment: score, start and end (1-based matrix coordinates).</summary>
internal struct Best
{
    public int Score, I0, J0, I, J;
}

/// <summary>
/// Smith-Waterman-Gotoh local alignment and its top non-overlapping hits (DESIGN.md §8.5).
/// Every cell carries the start of its alignment, so the best cell gives a whole hit. Further
/// hits follow Waterman-Eggert by masking: once a hit is taken, its rows and columns are closed,
/// so the next hit is the best alignment inside one of the four corner blocks around it (an
/// alignment cannot cross closed rows or columns). Melody/bass add the Mongeau-Sankoff
/// consolidation move: one note against a run of 2-3 same-pitch notes in the other line, at a
/// small cost. Scores are integers (x20). V2 aligns only to place display passages (a window of the
/// later song against the earlier song), so the AVX2 surrogate kernel of V1 is gone.
/// </summary>
internal sealed unsafe class LocalAligner
{
    private const int Neg = -(1 << 28);
    private const int Pad = 3;   // zero columns left of column 1 (consolidation looks back 3)

    private int[] _h = [], _s = [], _e = [], _se = [];
    private int _stride;
    private readonly List<(int R0, int R1, int C0, int C1, Best B)> _blocks = [];

    public long Cells;

    private void EnsureScalar(int m)
    {
        int stride = m + Pad + 1;
        if (_h.Length >= 4 * stride && _stride == stride) return;
        _stride = stride;
        if (_h.Length < 4 * stride)
        {
            _h = new int[4 * stride];
            _s = new int[4 * stride];
            _e = new int[stride];
            _se = new int[stride];
        }
    }

    /// <summary>Best local alignment inside rows r0..r1 and columns c0..c1 (1-based, inclusive).</summary>
    public Best BestIn(Seq a, Seq b, in AlignParams ap, int r0, int r1, int c0, int c1)
    {
        var best = new Best();
        if (r1 < r0 || c1 < c0) return best;
        EnsureScalar(b.N);
        Cells += (long)(r1 - r0 + 1) * (c1 - c0 + 1);
        int stride = _stride, open = ap.Open, ext = ap.Ext, cons = ap.Cons, alpha = ap.Alpha;
        // Columns c0-3 .. c1 of all four rows start at zero; E starts closed.
        for (int r = 0; r < 4; r++)
        {
            Array.Clear(_h, r * stride + c0, c1 - c0 + 1 + Pad);
            Array.Clear(_s, r * stride + c0, c1 - c0 + 1 + Pad);
        }
        Array.Fill(_e, Neg, c0 + Pad, c1 - c0 + 1);
        bool useCons = cons > 0;
        fixed (int* H = _h, S = _s, E0 = _e, SE0 = _se, sub0 = ap.Sub)
        fixed (byte* at = a.Tok, bt = b.Tok, ar = a.Run, br = b.Run)
        {
            int* E = E0 + Pad, SE = SE0 + Pad;
            for (int i = r0; i <= r1; i++)
            {
                int* hc = H + (i & 3) * stride + Pad, h1 = H + ((i - 1) & 3) * stride + Pad;
                int* sc = S + (i & 3) * stride + Pad, s1 = S + ((i - 1) & 3) * stride + Pad;
                int* h2 = H + ((i - 2) & 3) * stride + Pad, s2 = S + ((i - 2) & 3) * stride + Pad;
                int* h3 = H + ((i - 3) & 3) * stride + Pad, s3 = S + ((i - 3) & 3) * stride + Pad;
                int* subRow = sub0 + at[i - 1] * alpha;
                int kA = useCons ? Math.Min(ar[i - 1], i - r0 + 1) : 1;
                int* subA2 = kA >= 2 ? sub0 + at[i - 2] * alpha : null;
                int* subA3 = kA >= 3 ? sub0 + at[i - 3] * alpha : null;
                int pi = i << 16;
                hc[c0 - 1] = 0;
                sc[c0 - 1] = 0;
                int f = Neg, sf = 0;
                for (int j = c0; j <= c1; j++)
                {
                    // Vertical gap (a_i against nothing), from row i - 1.
                    int eExt = E[j] - ext, eOpen = h1[j] - open;
                    int e, se;
                    if (eOpen > eExt) { e = eOpen; se = s1[j]; } else { e = eExt; se = SE[j]; }
                    E[j] = e;
                    SE[j] = se;
                    // Horizontal gap (b_j against nothing), from column j - 1.
                    int fOpen = hc[j - 1] - open;
                    f -= ext;
                    if (fOpen > f) { f = fOpen; sf = sc[j - 1]; }
                    // Diagonal; a path that starts here takes (i, j) as its start.
                    int hd = h1[j - 1];
                    int h = hd + subRow[bt[j - 1]];
                    int s = hd > 0 ? s1[j - 1] : pi | j;
                    if (e > h) { h = e; s = se; }
                    if (f > h) { h = f; s = sf; }
                    if (useCons)
                    {
                        int kB = Math.Min(br[j - 1], j - c0 + 1);
                        if (kB >= 2)
                        {
                            // a_i against b_{j-1} b_j (same pitch)
                            int hp = h1[j - 2];
                            int c = hp + subRow[bt[j - 2]] - cons;
                            if (c > h) { h = c; s = hp > 0 ? s1[j - 2] : pi | (j - 1); }
                            if (kB >= 3)
                            {
                                hp = h1[j - 3];
                                c = hp + subRow[bt[j - 3]] - cons;
                                if (c > h) { h = c; s = hp > 0 ? s1[j - 3] : pi | (j - 2); }
                            }
                        }
                        if (subA2 != null)
                        {
                            int hp = h2[j - 1];
                            int c = hp + subA2[bt[j - 1]] - cons;
                            if (c > h) { h = c; s = hp > 0 ? s2[j - 1] : ((i - 1) << 16) | j; }
                            if (subA3 != null)
                            {
                                hp = h3[j - 1];
                                c = hp + subA3[bt[j - 1]] - cons;
                                if (c > h) { h = c; s = hp > 0 ? s3[j - 1] : ((i - 2) << 16) | j; }
                            }
                        }
                    }
                    if (h <= 0)
                    {
                        h = 0;
                        s = 0;
                    }
                    hc[j] = h;
                    sc[j] = s;
                    if (h > best.Score)
                    {
                        best.Score = h;
                        best.I0 = s >> 16;
                        best.J0 = s & 0xFFFF;
                        best.I = i;
                        best.J = j;
                    }
                }
            }
        }
        return best;
    }

    /// <summary>Top non-overlapping hits of a full alignment (scalar).</summary>
    public int Hits(Seq a, Seq b, in AlignParams ap, Span<Hit> hits)
    {
        if (a.N == 0 || b.N == 0) return 0;
        return HitsFrom(a, b, ap, BestIn(a, b, ap, 1, a.N, 1, b.N), hits);
    }

    /// <summary>
    /// Hits given the best cell of the full matrix. Each hit closes its rows and columns in every
    /// open block; hit k + 1 is the best alignment left in the open blocks (ties: lower end row,
    /// then column, then block order). Hits therefore never overlap in either sequence.
    /// </summary>
    public int HitsFrom(Seq a, Seq b, in AlignParams ap, Best first, Span<Hit> hits)
    {
        if (first.Score < ap.MinHit || hits.Length == 0) return 0;
        _blocks.Clear();
        _blocks.Add((1, a.N, 1, b.N, first));
        Span<(int, int)> rr = stackalloc (int, int)[2];
        Span<(int, int)> cc = stackalloc (int, int)[2];
        int found = 0;
        while (found < hits.Length)
        {
            int pick = -1;
            for (int k = 0; k < _blocks.Count; k++)
            {
                var c = _blocks[k].B;
                if (c.Score < ap.MinHit) continue;
                if (pick < 0) { pick = k; continue; }
                var p = _blocks[pick].B;
                if (c.Score > p.Score || c.Score == p.Score && (c.I < p.I || c.I == p.I && c.J < p.J)) pick = k;
            }
            if (pick < 0) break;
            var bb = _blocks[pick].B;
            hits[found++] = new Hit { Score = bb.Score, A0 = bb.I0 - 1, A1 = bb.I - 1, B0 = bb.J0 - 1, B1 = bb.J - 1 };
            if (found == hits.Length) break;
            // Close rows I0..I and columns J0..J everywhere: split every block they cross.
            int count = _blocks.Count;
            for (int k = 0; k < count; k++)
            {
                var (r0, r1, c0, c1, _) = _blocks[k];
                bool rows = r0 <= bb.I && bb.I0 <= r1, cols = c0 <= bb.J && bb.J0 <= c1;
                if (!rows && !cols) continue;
                _blocks[k] = (0, -1, 0, -1, default);   // removed below
                int nr = 0, ncc = 0;
                if (!rows) rr[nr++] = (r0, r1);
                else
                {
                    if (r0 <= bb.I0 - 1) rr[nr++] = (r0, bb.I0 - 1);
                    if (bb.I + 1 <= r1) rr[nr++] = (bb.I + 1, r1);
                }
                if (!cols) cc[ncc++] = (c0, c1);
                else
                {
                    if (c0 <= bb.J0 - 1) cc[ncc++] = (c0, bb.J0 - 1);
                    if (bb.J + 1 <= c1) cc[ncc++] = (bb.J + 1, c1);
                }
                for (int x = 0; x < nr; x++)
                    for (int y = 0; y < ncc; y++)
                        Add(a, b, ap, rr[x].Item1, rr[x].Item2, cc[y].Item1, cc[y].Item2);
            }
            _blocks.RemoveAll(x => x.R1 < x.R0);
        }
        return found;
    }

    private void Add(Seq a, Seq b, in AlignParams ap, int r0, int r1, int c0, int c1)
    {
        // A hit needs at least three positive moves, hence three rows and three columns.
        if (r1 - r0 < 2 || c1 - c0 < 2) return;
        _blocks.Add((r0, r1, c0, c1, BestIn(a, b, ap, r0, r1, c0, c1)));
    }
}

/// <summary>
/// Global Needleman-Wunsch identity alignment (Savage et al. 2018 PMI): match 1, mismatch 0,
/// a gap of length k costs open + k * extend (12 / 6). Returns identities / mean length.
/// </summary>
internal sealed class GlobalIdentity
{
    private const int Neg = -(1 << 28);
    private byte[] _tb = [];
    private int[] _m = [], _x = [], _y = [], _pm = [], _px = [], _py = [];

    public double Pid(ReadOnlySpan<int> a, ReadOnlySpan<int> b, int open, int ext)
    {
        int n = a.Length, m = b.Length;
        if (n == 0 || m == 0) return 0;
        if (_tb.Length < (n + 1) * (m + 1)) _tb = new byte[(n + 1) * (m + 1)];
        if (_m.Length < m + 1)
        {
            _m = new int[m + 1]; _x = new int[m + 1]; _y = new int[m + 1];
            _pm = new int[m + 1]; _px = new int[m + 1]; _py = new int[m + 1];
        }
        // Row 0.
        _pm[0] = 0; _px[0] = Neg; _py[0] = Neg;
        for (int j = 1; j <= m; j++) { _pm[j] = Neg; _px[j] = Neg; _py[j] = -(open + ext * j); _tb[j] = 2 << 4 | 2 << 2; }
        int w = m + 1;
        for (int i = 1; i <= n; i++)
        {
            _m[0] = Neg; _x[0] = -(open + ext * i); _y[0] = Neg;
            _tb[i * w] = 1 << 2 | 1 << 4;
            for (int j = 1; j <= m; j++)
            {
                int s = a[i - 1] == b[j - 1] ? 1 : 0;
                // M: from (i-1, j-1) best of M/X/Y.
                int dm = _pm[j - 1], dx = _px[j - 1], dy = _py[j - 1];
                int bm = 0, vm = dm;
                if (dx > vm) { vm = dx; bm = 1; }
                if (dy > vm) { vm = dy; bm = 2; }
                _m[j] = vm + s;
                // X: gap in b (consume a_i) from (i-1, j).
                int xo = _pm[j] - open - ext, xe = _px[j] - ext;
                int bx = xo >= xe ? 0 : 1;
                _x[j] = Math.Max(xo, xe);
                // Y: gap in a (consume b_j) from (i, j-1).
                int yo = _m[j - 1] - open - ext, ye = _y[j - 1] - ext;
                int by = yo >= ye ? 0 : 2;
                _y[j] = Math.Max(yo, ye);
                _tb[i * w + j] = (byte)(bm | bx << 2 | by << 4);
            }
            (_m, _pm) = (_pm, _m);
            (_x, _px) = (_px, _x);
            (_y, _py) = (_py, _y);
        }
        // Traceback from the best final state.
        int state = 0, best = _pm[m];
        if (_px[m] > best) { best = _px[m]; state = 1; }
        if (_py[m] > best) { state = 2; }
        int ii = n, jj = m, ident = 0;
        while (ii > 0 && jj > 0)
        {
            byte t = _tb[ii * w + jj];
            if (state == 0)
            {
                if (a[ii - 1] == b[jj - 1]) ident++;
                state = t & 3;
                ii--; jj--;
            }
            else if (state == 1)
            {
                state = (t >> 2) & 3;
                ii--;
            }
            else
            {
                state = (t >> 4) & 3;
                jj--;
            }
        }
        return ident / ((n + m) / 2.0);
    }
}
