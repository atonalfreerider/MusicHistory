namespace MusicHistory.Influence;

/// <summary>A shared passage: where the evidence of one channel lies in both songs.</summary>
internal sealed class Segment
{
    public Channel Channel;
    public double AStart, AEnd, BStart, BEnd, Bits;
    public int N;                 // tokens of B in the aligned hit (notes or chord changes)
    public string? Loop;          // loop channel: roman numerals of B's loop
    public bool SamePhase;        // loop channel: phase matched
}

/// <summary>Everything scored for one ordered pair (A earlier than B).</summary>
internal sealed class PairResult
{
    public int A, B;
    public readonly bool[] Avail = new bool[Channels.Count];
    public readonly double[] W = new double[Channels.Count];
    public readonly double[] E = new double[Channels.Count];
    public readonly double[] Mu = new double[Channels.Count];
    public readonly double[] Sigma = new double[Channels.Count];
    public readonly double[] Z = [double.NaN, double.NaN, double.NaN, double.NaN];
    public int K;                 // surrogates behind the z values (0, KScreen or KConfirm)
    public double Zc, P = 1, Q = double.NaN, S;
    public double Pmi = double.NaN, ChordId = double.NaN, DurationRatio = double.NaN;
    public int Shift;
    public bool Tested, Version, Significant, Contemporaneous;
    public string Relation = "none";
    public double CandBits;
    public int CandRank;
    public readonly List<Segment> Segments = [];

    public bool Counts(Channel c) => Avail[(int)c] && !double.IsNaN(Z[(int)c]) && Z[(int)c] >= 2;
    public double Excess(Channel c) => Avail[(int)c] && !double.IsNaN(Z[(int)c]) ? Math.Max(0, E[(int)c] - Mu[(int)c]) : 0;
}

/// <summary>Per-thread buffers of the scorer (nothing here is shared between threads).</summary>
internal sealed class Worker
{
    public readonly LocalAligner Aligner = new();
    public readonly GlobalIdentity Nw = new();
    public readonly WeightCache W = new();
    public readonly NgramScratch Scratch = new();
    public readonly Seq Sur = new();
    public readonly HashSet64 Tmp = new(2048), Counted = new(256);
    public readonly List<EvidenceItem> Items = [];
    public readonly List<EvidenceItem> Accepted = [];
    public readonly Dictionary<ulong, (int Start, int End)> AOcc = [];
    public List<(string Phase, EvidenceItem Item)>? Debug;   // when set, accepted evidence items are appended (diagnostics)
    public string DebugPhase = "";
    public bool[] Occupied = new bool[1024];
    public readonly Hit[][][] ShiftHits;          // [shift slot][channel][hit]
    public readonly int[][] ShiftHitCount;
    public readonly List<LoopId> SurLoops = [];
    public readonly Hit[] SurHits;
    public readonly Seq[] SurBatch = [new(), new(), new(), new(), new(), new(), new(), new()];
    public readonly int[] BatchIdx = new int[8];
    public readonly Best[] Bests = new Best[8];
    public readonly double[][] NullVals;
    public readonly double[] AccE, AccS;
    public readonly List<int> Touched = [];
    private readonly Dictionary<int, Features> _shifted = [];
    private Song? _b;
    public long Surrogates, SurrogatesAligned, PairsScored, PairsConfirmed;

    public Worker(int nSongs, Params p)
    {
        ShiftHits = new Hit[3][][];
        ShiftHitCount = new int[3][];
        for (int s = 0; s < 3; s++)
        {
            ShiftHits[s] = [new Hit[p.Hits], new Hit[p.Hits], new Hit[p.Hits]];
            ShiftHitCount[s] = new int[3];
        }
        SurHits = new Hit[p.Hits];
        NullVals = new double[Channels.Count][];
        for (int c = 0; c < Channels.Count; c++) NullVals[c] = new double[Math.Max(p.KConfirm, p.KScreen)];
        AccE = new double[nSongs];
        AccS = new double[nSongs];
    }

    public void SetB(Song b, Corpus corpus, bool skipStop)
    {
        if (ReferenceEquals(_b, b)) return;
        _b = b;
        _shifted.Clear();
        W.Reset(corpus, corpus.Cutoff[b.Index], skipStop);
    }

    public Features Shifted(Song b, int shift)
    {
        if (shift == 0) return b.F;
        if (!_shifted.TryGetValue(shift, out var f))
        {
            f = Features.Build(b, shift, Scratch, full: false);
            _shifted[shift] = f;
        }
        return f;
    }
}

internal struct EvidenceItem
{
    public byte Kind;
    public ulong Hash;
    public int Start, End, Hit;
    public int AStart, AEnd;      // an A occurrence of the same n-gram (segments only)
    public double W;
    public bool Stop;
}

/// <summary>
/// Scores one pair (DESIGN.md §8.5-9): alignment per channel (with the fifth-error hedge),
/// evidence bits inside the aligned regions, Markov-1 null (screen, then confirm), channel z,
/// Stouffer Z, S bits, and the version measures. Read-only shared state; per-thread buffers
/// live in <see cref="Worker"/>.
/// </summary>
internal sealed class PairScorer
{
    private readonly Params _p;
    private readonly Scoring _sc;
    private readonly Corpus _corpus;
    private readonly Song[] _songs;
    private static readonly int[] NoShift = [0];
    private static readonly int[] FifthShifts = [0, 5, 7];

    private readonly AlignParams _noteAp, _chordAp;

    public PairScorer(Params p, Corpus corpus, Song[] songs)
    {
        _p = p;
        _songs = songs;
        _sc = new Scoring(p);
        _noteAp = new AlignParams(_sc.MelSub, 48, _sc.MelOpen, _sc.MelExt, _sc.Cons, _sc.MinHit);
        _chordAp = new AlignParams(_sc.ChordSub, 36, _sc.ChordOpen, _sc.ChordExt, 0, _sc.MinHit);
        _corpus = corpus;
    }

    public Corpus Corpus => _corpus;

    private static double Entropy(Song s) => s.IntervalEntropy;

    public PairResult Score(Song a, Song b, Worker w)
    {
        w.SetB(b, _corpus, _p.EvidenceStopGrams == 0);
        w.PairsScored++;
        var p = _p;
        var r = new PairResult { A = a.Index, B = b.Index };
        double hMin = Math.Min(Entropy(a), Entropy(b));
        r.Avail[0] = a.F.Mel is { } am && b.F.Mel is { } bm && am.N >= p.MinLineNotes && bm.N >= p.MinLineNotes && hMin >= p.EntropyDrop;
        r.W[0] = hMin < p.EntropyHalf ? p.WMelody / 2 : p.WMelody;
        r.Avail[1] = a.F.Bass is { } ab && b.F.Bass is { } bb && ab.N >= p.MinLineNotes && bb.N >= p.MinLineNotes;
        r.W[1] = p.WBass;
        r.Avail[2] = a.F.Chord is { } ac && b.F.Chord is { } bc && ac.N >= p.MinChordChanges && bc.N >= p.MinChordChanges;
        r.W[2] = p.WChord;
        r.Avail[3] = a.F.Loops.Length > 0 && b.F.Loops.Length > 0;
        r.W[3] = p.WLoop;
        if (!r.Avail.Any(x => x)) return r;
        r.Tested = true;

        // Alignment, with the +5 / +7 hedge when either key is ambiguous by a fifth.
        int[] shifts = a.Fifth || b.Fifth ? FifthShifts : NoShift;
        long bestTotal = long.MinValue;
        int bestSlot = 0;
        for (int si = 0; si < shifts.Length; si++)
        {
            var bf = w.Shifted(b, shifts[si]);
            long total = shifts[si] == 0 ? 0 : -_sc.FifthPenalty;
            for (int c = 0; c < 3; c++)
            {
                w.ShiftHitCount[si][c] = 0;
                if (!r.Avail[c]) continue;
                int nh = AlignChannel((Channel)c, SeqOf(a.F, c)!, SeqOf(bf, c)!, w, w.ShiftHits[si][c]);
                w.ShiftHitCount[si][c] = nh;
                if (nh > 0) total += w.ShiftHits[si][c][0].Score;
            }
            if (total > bestTotal)
            {
                bestTotal = total;
                bestSlot = si;
            }
        }
        r.Shift = shifts[bestSlot];
        var bF = w.Shifted(b, r.Shift);

        // Evidence of the observed pair.
        w.DebugPhase = "obs";
        for (int c = 0; c < 3; c++)
            if (r.Avail[c])
                r.E[c] = Evidence(SeqOf(a.F, c)!, SeqOf(bF, c)!, w.ShiftHits[bestSlot][c].AsSpan(0, w.ShiftHitCount[bestSlot][c]), w,
                    r.Segments, (Channel)c);
        if (r.Avail[3]) r.E[3] = LoopEvidence(a, b, a.F.Loops, bF.Loops, w, r.Segments);

        // A pair whose observed bits cannot pass the bits gate (mu >= 0, so the excess is at most E)
        // can never be significant: its null is skipped and it enters BH with p = 1 (conservative
        // for every other pair's q).
        bool reachable = r.Avail[0] && r.E[0] >= p.MelodyBits || r.Avail[1] && r.E[1] >= p.BassBits || r.Avail[2] && r.E[2] >= p.ChordBits;
        if (!reachable && p.SkipUnreachable != 0)
        {
            r.Zc = double.NaN;
            r.S = double.NaN;
            r.P = 1;
            VersionMeasures(a, r, w, bF);
            return r;
        }

        // Null model: screen with KScreen surrogates, confirm with KConfirm when any z >= ScreenZ.
        var rngs = new Rng[Channels.Count];
        bool anyScreen = false;
        for (int c = 0; c < Channels.Count; c++)
        {
            if (!r.Avail[c] || r.E[c] < Math.Max(p.MinEvidenceBits, p.ChannelZ * p.SigmaFloor)) continue;
            if (c == 3 && p.LoopNullCorpus == 0 && (b.Chords == null || b.F.ChordMarkov == null)) continue;
            rngs[c] = new Rng(Rng.Seed(a.WorkId, b.WorkId, Channels.Names[c]));
            RunNull((Channel)c, a, b, r.Shift, ref rngs[c], 0, p.KScreen, w);
            SetZ(r, c, p.KScreen, w);
            anyScreen |= r.Z[c] >= p.ScreenZ;
        }
        r.K = r.Z.Any(z => !double.IsNaN(z)) ? p.KScreen : 0;
        // Confirm only pairs that can still pass the bits gate: skipping the others cannot change
        // which pairs are significant.
        if (anyScreen && reachable && p.KConfirm > p.KScreen)
        {
            w.PairsConfirmed++;
            for (int c = 0; c < Channels.Count; c++)
            {
                if (double.IsNaN(r.Z[c])) continue;
                RunNull((Channel)c, a, b, r.Shift, ref rngs[c], p.KScreen, p.KConfirm, w);
                SetZ(r, c, p.KConfirm, w);
            }
            r.K = p.KConfirm;
        }

        // Stouffer over the channels available in both songs; a channel counts if z >= 2.
        double num = 0, den = 0;
        bool anyMain = r.Counts(Channel.Melody) || r.Counts(Channel.Bass) || r.Counts(Channel.Chord);
        for (int c = 0; c < Channels.Count; c++)
        {
            if (!r.Avail[c]) continue;
            bool counts = r.Counts((Channel)c) && (c != 3 || anyMain || p.LoopAuxiliary == 0);
            if (p.ZOverCounting == 0 || counts) den += r.W[c] * r.W[c];
            if (counts) num += r.W[c] * r.Z[c];
            r.S += r.W[c] * r.Excess((Channel)c);
        }
        r.Zc = den > 0 ? num / Math.Sqrt(den) : 0;
        r.P = Stats.NormalSf(r.Zc);

        VersionMeasures(a, r, w, bF);
        return r;
    }

    /// <summary>Segments are stored rounded (JSON), so round them here and the tree built in memory equals the one rebuilt from the DB.</summary>
    private static double R4(double v) => Math.Round(v, 4);

    private static Seq? SeqOf(Features f, int c) => c switch { 0 => f.Mel, 1 => f.Bass, _ => f.Chord };

    private AlignParams Ap(Channel c) => c == Channel.Chord ? _chordAp : _noteAp;

    private int AlignChannel(Channel c, Seq a, Seq b, Worker w, Hit[] hits) => w.Aligner.Hits(a, b, Ap(c), hits);

    private void SetZ(PairResult r, int c, int k, Worker w)
    {
        var v = w.NullVals;
        double mu = 0;
        for (int i = 0; i < k; i++) mu += v[c][i];
        mu /= k;
        double var = 0;
        for (int i = 0; i < k; i++) var += (v[c][i] - mu) * (v[c][i] - mu);
        double sd = Math.Sqrt(var / k);
        r.Mu[c] = mu;
        r.Sigma[c] = sd;
        r.Z[c] = (r.E[c] - mu) / Math.Max(sd, _p.SigmaFloor);
    }

    private void RunNull(Channel ch, Song a, Song b, int shift, ref Rng rng, int from, int to, Worker w)
    {
        var vals = w.NullVals[(int)ch];
        bool keep = _p.NullKeepRhythm != 0;
        if (ch == Channel.Loop && _p.LoopNullCorpus != 0)
        {
            // Loop identities are not sequences: their null replaces A by a random earlier song
            // ("is this loop shared with A more than with any song released before B?").
            int cutoff = _corpus.Cutoff[b.Index];
            for (int k = from; k < to; k++)
            {
                w.Surrogates++;
                if (cutoff < 2)
                {
                    vals[k] = 0;
                    continue;
                }
                int other = rng.Below(cutoff - 1);
                if (other >= a.Index) other++;
                if (other >= cutoff) other = cutoff - 1;
                vals[k] = LoopEvidence(_songs[other], b, _songs[other].F.Loops, SeqLoops(b, shift, w), w, null);
            }
            return;
        }
        if (ch == Channel.Loop)
        {
            for (int k = from; k < to; k++)
            {
                w.Surrogates++;
                w.DebugPhase = "null" + k.ToString(System.Globalization.CultureInfo.InvariantCulture);
                Surrogates.Loops(b.Chords!, b.F.ChordMarkov!, shift, ref rng, w.Sur, w.SurLoops, keep);
                vals[k] = LoopEvidence(a, b, a.F.Loops, w.SurLoops, w, null);
            }
            return;
        }
        var aSeq = SeqOf(a.F, (int)ch)!;
        var aSet = ch switch { Channel.Melody => a.F.MelSet!, Channel.Bass => a.F.BassSet!, _ => a.F.ChordSet! };
        int pending = 0;
        for (int k = from; k < to; k++)
        {
            w.Surrogates++;
            var sur = w.SurBatch[pending];
            switch (ch)
            {
                case Channel.Melody:
                    Surrogates.Notes(b.Melody!, b.F.MelMarkov!, shift, ref rng, sur, melody: true, w.Scratch, keep);
                    break;
                case Channel.Bass:
                    Surrogates.Notes(b.Bass!, b.F.BassMarkov!, shift, ref rng, sur, melody: false, w.Scratch, keep);
                    break;
                default:
                    Surrogates.Chords(b.Chords!, b.F.ChordMarkov!, shift, ref rng, sur, keep);
                    break;
            }
            // E is 0 without alignment when the surrogate shares no n-gram with A at all.
            bool any = false;
            for (int i = 0; i < sur.NOcc && !any; i++) any = aSet.Contains(sur.Occs[i].Hash);
            if (!any)
            {
                vals[k] = 0;
                continue;
            }
            w.BatchIdx[pending++] = k;
            if (pending == w.SurBatch.Length)
            {
                FlushBatch(ch, aSeq, pending, vals, w);
                pending = 0;
            }
        }
        if (pending > 0) FlushBatch(ch, aSeq, pending, vals, w);
    }

    private static IReadOnlyList<LoopId> SeqLoops(Song b, int shift, Worker w) => w.Shifted(b, shift).Loops;

    /// <summary>Align A against up to 8 surrogates in lockstep, then take each lane's hits and evidence.</summary>
    private void FlushBatch(Channel ch, Seq aSeq, int count, double[] vals, Worker w)
    {
        var ap = Ap(ch);
        w.SurrogatesAligned += count;
        w.Aligner.Best8(aSeq, w.SurBatch, count, ap, w.Bests);
        for (int l = 0; l < count; l++)
        {
            w.DebugPhase = "null" + w.BatchIdx[l].ToString(System.Globalization.CultureInfo.InvariantCulture);
            int nh = w.Aligner.HitsFrom(aSeq, w.SurBatch[l], ap, w.Bests[l], w.SurHits);
            vals[w.BatchIdx[l]] = Evidence(aSeq, w.SurBatch[l], w.SurHits.AsSpan(0, nh), w, null, ch);
        }
    }

    /// <summary>
    /// DESIGN.md §8.6: bits of the n-grams shared by A and B whose occurrences lie inside the
    /// aligned regions (A side inside the hit's A span, B side inside its B span), greedy
    /// non-overlapping cover of B, longest then heaviest first. Each distinct n-gram counts once
    /// per pair, so a coincidence repeated by B's own chorus is not multiplied.
    /// </summary>
    public double Evidence(Seq aSeq, Seq bSeq, ReadOnlySpan<Hit> hits, Worker w, List<Segment>? segs, Channel ch)
    {
        var items = w.Items;
        items.Clear();
        bool withSegs = segs != null;
        for (int h = 0; h < hits.Length; h++)
        {
            var hit = hits[h];
            w.Tmp.Clear();
            if (withSegs) w.AOcc.Clear();
            for (int i = aSeq.LowerBound(hit.A0); i < aSeq.NOcc && aSeq.Occs[i].Start <= hit.A1; i++)
            {
                ref var ao = ref aSeq.Occs[i];
                if (ao.End > hit.A1) continue;
                w.Tmp.Add(ao.Hash);
                if (withSegs) w.AOcc.TryAdd(ao.Hash, (ao.Start, ao.End));
            }
            if (w.Tmp.Count == 0) continue;
            for (int i = bSeq.LowerBound(hit.B0); i < bSeq.NOcc && bSeq.Occs[i].Start <= hit.B1; i++)
            {
                ref var o = ref bSeq.Occs[i];
                if (o.End <= hit.B1 && w.Tmp.Contains(o.Hash))
                {
                    double wt = w.W.Get(o.Hash, out bool stop);
                    if (wt <= 0) continue;
                    var (a0, a1) = withSegs ? w.AOcc[o.Hash] : (-1, -1);
                    items.Add(new EvidenceItem { Kind = o.Kind, Hash = o.Hash, Start = o.Start, End = o.End, Hit = h, W = wt, Stop = stop, AStart = a0, AEnd = a1 });
                }
            }
        }
        if (items.Count == 0) return 0;
        items.Sort(static (x, y) =>
        {
            int lx = x.End - x.Start, ly = y.End - y.Start;
            if (lx != ly) return ly.CompareTo(lx);
            if (x.W != y.W) return y.W.CompareTo(x.W);
            if (x.Start != y.Start) return x.Start.CompareTo(y.Start);
            return x.Hash.CompareTo(y.Hash);
        });
        if (w.Occupied.Length < bSeq.N) w.Occupied = new bool[Math.Max(bSeq.N, w.Occupied.Length * 2)];
        var occ = w.Occupied;
        Array.Clear(occ, 0, bSeq.N);
        w.Counted.Clear();
        w.Accepted.Clear();
        double total = 0, stopBits = 0;
        foreach (var it in items)
        {
            if (w.Counted.Contains(it.Hash)) continue;
            bool free = true;
            for (int t = it.Start; t <= it.End && free; t++) free = !occ[t];
            if (!free) continue;
            for (int t = it.Start; t <= it.End; t++) occ[t] = true;
            w.Counted.Add(it.Hash);
            w.Debug?.Add((w.DebugPhase, it));
            if (withSegs) w.Accepted.Add(it);
            if (it.Stop) stopBits += it.W;
            else total += it.W;
        }
        // Commonplace n-grams (in more than CapFraction of songs) still score, but cheaply: at most
        // StopCapBits per channel of the pair.
        double scale = stopBits > _p.StopCapBits ? _p.StopCapBits / stopBits : 1.0;
        total += stopBits * scale;
        if (segs != null) AddSegments(aSeq, bSeq, w.Accepted, scale, segs, ch);
        return total;
    }

    /// <summary>
    /// The shared passages of one channel: the counted n-grams clustered along B (a gap of more than
    /// 8 tokens starts a new passage). Each passage carries its B and A beats, bits and the number
    /// of B tokens the n-grams cover. (With the design's cheap gaps a hit spans most of both songs,
    /// so the hit itself does not say where the shared material is.)
    /// </summary>
    private static void AddSegments(Seq aSeq, Seq bSeq, List<EvidenceItem> accepted, double stopScale, List<Segment> segs, Channel ch)
    {
        if (accepted.Count == 0) return;
        accepted.Sort(static (x, y) => x.Start != y.Start ? x.Start.CompareTo(y.Start) : x.End.CompareTo(y.End));
        int k = 0;
        while (k < accepted.Count)
        {
            int b0 = accepted[k].Start, b1 = accepted[k].End, a0 = int.MaxValue, a1 = -1, covered = 0;
            double bits = 0;
            int m = k;
            while (m < accepted.Count && (m == k || accepted[m].Start <= b1 + 8))
            {
                var it = accepted[m];
                b1 = Math.Max(b1, it.End);
                a0 = Math.Min(a0, it.AStart);
                a1 = Math.Max(a1, it.AEnd);
                covered += it.End - it.Start + 1;
                bits += it.Stop ? it.W * stopScale : it.W;
                m++;
            }
            if (bits > 0)
                segs.Add(new Segment
                {
                    Channel = ch, Bits = R4(bits), N = covered,
                    AStart = R4(aSeq.BeatStart(a0)), AEnd = R4(aSeq.BeatEnd(a1)),
                    BStart = R4(bSeq.BeatStart(b0)), BEnd = R4(bSeq.BeatEnd(b1)),
                });
            k = m;
        }
    }

    /// <summary>
    /// E_loop = best nested identity weight over B's loops that A shares: (cycle, phase, rhythm)
    /// or (cycle, phase) when the phase matches; a cross-phase cycle match counts 0.5 w(cycle).
    /// </summary>
    public double LoopEvidence(Song a, Song b, LoopId[] aLoops, IReadOnlyList<LoopId> bLoops, Worker w, List<Segment>? segs)
    {
        double best = 0;
        int bestA = -1, bestB = -1;
        bool bestSame = false;
        for (int j = 0; j < bLoops.Count; j++)
        {
            var lb = bLoops[j];
            for (int i = 0; i < aLoops.Length; i++)
            {
                var la = aLoops[i];
                if (la.C != lb.C) continue;
                double v;
                bool same = la.CP == lb.CP;
                if (same)
                {
                    v = w.W[lb.CP];
                    if (la.CPR == lb.CPR) v = Math.Max(v, w.W[lb.CPR]);
                }
                else v = 0.5 * w.W[lb.C];
                if (v > best)
                {
                    best = v;
                    bestA = la.Row;
                    bestB = lb.Row;
                    bestSame = same;
                }
            }
        }
        if (segs != null && best > 0 && bestA >= 0 && bestB >= 0)
        {
            var ra = a.Loops[bestA];
            var rb = b.Loops[bestB];
            double sa = ra.VisitStarts.Length > 0 ? ra.VisitStarts[0] : 0, sb = rb.VisitStarts.Length > 0 ? rb.VisitStarts[0] : 0;
            segs.Add(new Segment
            {
                Channel = Channel.Loop, Bits = R4(best), N = rb.Own.Length, Loop = rb.Roman, SamePhase = bestSame,
                AStart = R4(sa), AEnd = R4(sa + ra.VisitBeats), BStart = R4(sb), BEnd = R4(sb + rb.VisitBeats),
            });
        }
        return best;
    }

    /// <summary>
    /// DESIGN.md §8.9: duration ratio, then global chord identity, then melody PMI (each only when
    /// the previous test passes; PMI is filled in later for significant pairs for display).
    /// </summary>
    private void VersionMeasures(Song a, PairResult r, Worker w, Features bF)
    {
        var b = _songs[r.B];
        double da = a.DurationS > 0 && b.DurationS > 0 ? a.DurationS : a.EndBeat;
        double db = a.DurationS > 0 && b.DurationS > 0 ? b.DurationS : b.EndBeat;
        r.DurationRatio = da > 0 ? db / da : double.NaN;
        if (!(r.DurationRatio >= _p.DurationRatioMin && r.DurationRatio <= _p.DurationRatioMax)) return;
        if (a.F.Chord == null || bF.Chord == null) return;
        r.ChordId = w.Nw.Pid(a.F.Chord.Pitch.AsSpan(0, a.F.Chord.N), bF.Chord.Pitch.AsSpan(0, bF.Chord.N), _p.NwGapOpen, _p.NwGapExtend);
        if (r.ChordId < _p.ChordIdentityMin) return;
        FillPmi(r, w, bF);
        r.Version = r.Pmi >= _p.PmiMin;
    }

    /// <summary>Melody PMI of the pair at its alignment shift (Savage: pitch classes, global NW identity).</summary>
    public void FillPmi(PairResult r, Worker w, Features? bF = null)
    {
        if (!double.IsNaN(r.Pmi)) return;
        var a = _songs[r.A];
        if (bF == null)
        {
            w.SetB(_songs[r.B], _corpus, _p.EvidenceStopGrams == 0);
            bF = w.Shifted(_songs[r.B], r.Shift);
        }
        if (a.F.Mel == null || bF.Mel == null) return;
        Span<int> pa = PcBuffer(a.F.Mel, 0), pb = PcBuffer(bF.Mel, 1);
        r.Pmi = w.Nw.Pid(pa, pb, _p.NwGapOpen, _p.NwGapExtend);
    }

    [ThreadStatic] private static int[][]? _pcBuf;

    private static Span<int> PcBuffer(Seq s, int slot)
    {
        _pcBuf ??= [new int[256], new int[256]];
        if (_pcBuf[slot].Length < s.N) _pcBuf[slot] = new int[Math.Max(s.N, _pcBuf[slot].Length * 2)];
        var buf = _pcBuf[slot];
        for (int i = 0; i < s.N; i++) buf[i] = ((s.Pitch[i] % 12) + 12) % 12;
        return buf.AsSpan(0, s.N);
    }

    /// <summary>Version check for a same-time pair (no null model: such pairs never become edges).</summary>
    public PairResult ScoreContemporaneous(Song a, Song b, Worker w)
    {
        w.SetB(b, _corpus, _p.EvidenceStopGrams == 0);
        var r = new PairResult { A = a.Index, B = b.Index, Contemporaneous = true, Relation = "contemporaneous" };
        int[] shifts = a.Fifth || b.Fifth ? FifthShifts : NoShift;
        double best = double.NegativeInfinity;
        foreach (int s in shifts)
        {
            var bF = w.Shifted(b, s);
            var t = new PairResult { A = a.Index, B = b.Index, Shift = s };
            VersionMeasures(a, t, w, bF);
            double v = (double.IsNaN(t.ChordId) ? 0 : t.ChordId) + (double.IsNaN(t.Pmi) ? 0 : t.Pmi) - (s == 0 ? 0 : 1e-6);
            if (v > best)
            {
                best = v;
                r.Shift = s;
                r.ChordId = t.ChordId;
                r.Pmi = t.Pmi;
                r.DurationRatio = t.DurationRatio;
                r.Version = t.Version;
            }
        }
        if (r.Version) r.Relation = "version";
        return r;
    }
}
