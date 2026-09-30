namespace MusicHistory.Influence;

/// <summary>A shared passage: where the evidence of one channel lies in both songs.</summary>
internal sealed class Segment
{
    public Channel Channel;
    public double AStart, AEnd, BStart, BEnd, Bits;
    public double Z = double.NaN; // the channel's z at the winning window
    public int N;                 // B notes (or chord changes) covered by the shared rare n-grams
    public string? Loop;          // loop channel: roman numerals of B's loop
    public bool SamePhase;        // loop channel: phase matched
}

/// <summary>Everything decided for one ordered pair (A earlier than B, or a same-time pair checked for versions).</summary>
internal sealed class PairResult
{
    public int A, B;
    public readonly bool[] Avail = new bool[Channels.Count];
    public readonly double[] W = new double[Channels.Count];          // fusion weights
    public readonly double[] E = new double[Channels.Count];          // S_c: shared rare IDF bits at the winning window
    public readonly double[] Mu = new double[Channels.Count];
    public readonly double[] Sigma = new double[Channels.Count];
    public readonly double[] Z = Enumerable.Repeat(double.NaN, Channels.Count).ToArray();
    public readonly double[] ZMax = Enumerable.Repeat(double.NaN, Channels.Count).ToArray();
    public readonly bool[] Counting = new bool[Channels.Count];
    public double Zc = double.NaN;      // fused z at the winning window
    public double P = 1;                // empirical tail probability of Zc among the null sample (stored as q)
    public double Q = double.NaN;
    public double S;                    // sum over channels of w_c max(0, S_c - mu_c) at the winning window (bits)
    public double RiffZ = double.NaN, RiffMax = double.NaN;
    public int Window = -1;
    public double WinStart = double.NaN, WinEnd = double.NaN;
    public int Shift;
    public int LaneSrc = -1, LaneIdx;
    public double Pmi = double.NaN, ChordId = double.NaN, DurationRatio = double.NaN;
    public bool Tested, Version, Significant, Contemporaneous, BassAlone, BassAloneRejected, HubRejected, InSample;
    public double HubZA = double.NaN, HubZB = double.NaN;
    public string Relation = "none";
    public readonly List<Segment> Segments = [];

    public bool Counts(Channel c) => Counting[(int)c];
    public double Excess(Channel c) => Avail[(int)c] && !double.IsNaN(Z[(int)c]) ? Math.Max(0, E[(int)c] - Mu[(int)c]) : 0;
}

/// <summary>
/// Turns the engine's per-pair record into a <see cref="PairResult"/>, and adds what only decided pairs need:
/// display passages (the winning window of B, located in A by local alignment), version measures (DESIGN §8.9)
/// and the melody PMI. Read-only shared state; one instance per thread.
/// </summary>
internal sealed class PairDetails
{
    private readonly V8Engine _e;
    private readonly Params _p;
    private readonly Scoring _sc;
    private readonly AlignParams _noteAp, _chordAp;
    private readonly LocalAligner _al = new();
    private readonly GlobalIdentity _nw = new();
    private readonly Dictionary<(int, int), Features> _shifted = [];
    private readonly Dictionary<int, List<V8Window>> _windows = [];

    public PairDetails(V8Engine e, Params p)
    {
        _e = e;
        _p = p;
        _sc = new Scoring(p);
        _noteAp = new AlignParams(_sc.MelSub, 48, _sc.MelOpen, _sc.MelExt, _sc.Cons, _sc.MinHit);
        _chordAp = new AlignParams(_sc.ChordSub, 36, _sc.ChordOpen, _sc.ChordExt, 0, _sc.MinHit);
    }

    public List<V8Window> WindowsOf(int b)
    {
        if (!_windows.TryGetValue(b, out var w))
        {
            if (_windows.Count > 64) _windows.Clear();
            _windows[b] = w = _e.Windows(_e.Songs[b]);
        }
        return w;
    }

    private Features FeaturesAt(int song, int shift)
    {
        if (!_shifted.TryGetValue((song, shift), out var f))
        {
            if (_shifted.Count > 256) _shifted.Clear();
            _shifted[(song, shift)] = f = Features.Build(_e.Songs[song], shift);
        }
        return f;
    }

    /// <summary>A result carrying the engine's values at the winning window.</summary>
    public PairResult From(in PairRec r, int a, int b)
    {
        var pr = new PairResult { A = a, B = b, Tested = true, Zc = r.Z, Window = r.Win, Shift = r.Shift, LaneSrc = r.LaneSrc, LaneIdx = r.LaneIdx };
        for (int c = 0; c < Channels.Count; c++)
        {
            pr.W[c] = _e.ChanW[c];
            pr.ZMax[c] = r.ZMax[c];
            if (!r.Has(c) || float.IsNaN(r.Z)) continue;
            pr.Avail[c] = true;
            pr.Z[c] = r.Zc[c];
            pr.E[c] = r.Sc[c];
            pr.Mu[c] = r.Mu[c];
            pr.Sigma[c] = r.Sd[c];
        }
        if (r.Has(Ch.Riff)) pr.RiffZ = r.Zc[Ch.Riff];
        pr.RiffMax = r.ZMax[Ch.Riff];
        if (!float.IsNaN(r.Z))
        {
            var wins = WindowsOf(b);
            if (r.Win >= 0 && r.Win < wins.Count)
            {
                pr.WinStart = wins[r.Win].T0;
                pr.WinEnd = wins[r.Win].T1;
            }
            double wsum = 0;
            for (int c = 0; c < Channels.Count; c++) wsum = Math.Max(wsum, pr.W[c]);
            for (int c = 0; c < Channels.Count; c++)
                if (pr.Avail[c] && wsum > 0) pr.S += pr.W[c] / wsum * pr.Excess((Channel)c);
        }
        return pr;
    }

    /// <summary>Replace the winning window by <paramref name="ev"/> (the bass-alone re-scan picked another window).</summary>
    public void SetWindow(PairResult pr, WindowEval ev)
    {
        pr.Zc = ev.Fused;
        pr.Window = ev.Window;
        pr.WinStart = ev.T0;
        pr.WinEnd = ev.T1;
        pr.Shift = ev.Shift;
        pr.LaneSrc = ev.LaneSrc;
        pr.LaneIdx = ev.LaneIdx;
        pr.S = 0;
        double wsum = pr.W.Max();
        for (int c = 0; c < Channels.Count; c++)
        {
            pr.Avail[c] = (ev.Avail & (1 << c)) != 0;
            pr.Z[c] = pr.Avail[c] ? ev.Z[c] : double.NaN;
            pr.E[c] = pr.Avail[c] ? ev.S[c] : 0;
            pr.Mu[c] = pr.Avail[c] ? ev.Mu[c] : 0;
            pr.Sigma[c] = pr.Avail[c] ? ev.Sd[c] : 0;
            if (pr.Avail[c] && wsum > 0) pr.S += pr.W[c] / wsum * pr.Excess((Channel)c);
        }
        pr.RiffZ = (ev.Avail & (1 << Ch.Riff)) != 0 ? ev.Z[Ch.Riff] : double.NaN;
    }

    // ------------------------------------------------------------------------------------------------ passages
    /// <summary>
    /// Display passages of a decided pair: for each counting channel at the winning window (or the channel with
    /// the largest weighted z when none counts), B's span is the window and A's span the top local-alignment hit
    /// of the window's line (or chords) against A at the window's shift (the hull of A's occurrences of the shared
    /// rare n-grams when no hit reaches the minimum). Bits = the channel's shared rare bits S_c.
    /// </summary>
    public void AddSegments(PairResult pr)
    {
        pr.Segments.Clear();
        if (pr.Window < 0) return;
        var wins = WindowsOf(pr.B);
        if (pr.Window >= wins.Count) return;
        var win = wins[pr.Window];
        var chans = Enumerable.Range(0, Channels.Count).Where(c => pr.Avail[c] && pr.Counting[c] && c != (int)Channel.Loop).ToList();
        if (chans.Count == 0)
        {
            int best = Enumerable.Range(0, Channels.Count).Where(c => pr.Avail[c] && c != (int)Channel.Loop)
                .OrderByDescending(c => pr.W[c] * pr.Z[c]).ThenBy(c => c).DefaultIfEmpty(-1).First();
            if (best >= 0) chans.Add(best);
        }
        foreach (int c in chans)
            if (Passage(pr, win, (Channel)c) is { } seg) pr.Segments.Add(seg);
        if (pr.Avail[(int)Channel.Loop] && pr.Counting[(int)Channel.Loop] && LoopPassage(pr) is { } ls) pr.Segments.Add(ls);
    }

    private static double R4(double v) => Math.Round(v, 4);

    private Segment? Passage(PairResult pr, V8Window win, Channel ch)
    {
        var sa = _e.Songs[pr.A];
        var sb = _e.Songs[pr.B];
        int s = pr.Shift;
        double fdbB = sb.FirstDownbeat, bpbB = sb.BeatsPerBar > 0 ? sb.BeatsPerBar : 4.0;
        double fdbA = sa.FirstDownbeat, bpbA = sa.BeatsPerBar > 0 ? sa.BeatsPerBar : 4.0;
        Seq? bSeq = null, aSeq = null;
        var bOcc = new List<GramOcc>();
        var aOcc = new List<GramOcc>();
        HashSet64? target = null;
        Grp grp;
        bool chord = false;
        switch (ch)
        {
            case Channel.Melody when win.Mel is { } mv && sa.Melody != null:
                bSeq = Features.LineSeq(mv, s);
                aSeq = FeaturesAt(pr.A, 0).Mel;
                Occ(true, mv, s, fdbB, bpbB, bOcc);
                Occ(true, LineView.Of(sa.Melody, double.NaN, 0), 0, fdbA, bpbA, aOcc);
                target = _e.Data[pr.A].Mel;
                grp = Grp.Mel;
                break;
            case Channel.Bass when win.Bass is { } bv && sa.Bass != null:
                bSeq = Features.LineSeq(bv, s);
                aSeq = FeaturesAt(pr.A, 0).Bass;
                Occ(false, bv, s, fdbB, bpbB, bOcc);
                Occ(false, LineView.Of(sa.Bass, double.NaN, 0), 0, fdbA, bpbA, aOcc);
                target = _e.Data[pr.A].Bass;
                grp = Grp.BassNpc2;
                break;
            case Channel.Chord when win.Chord is { } cv && sa.Chords != null:
                bSeq = Features.ChordSeq(cv, s);
                aSeq = FeaturesAt(pr.A, 0).Chord;
                Grams.Chords(cv.Tok, cv.Start, cv.Dur, fdbB, bpbB, s, false, bOcc);
                Grams.Chords(sa.Chords.Tokens, sa.Chords.Starts, sa.Chords.Durs, fdbA, bpbA, 0, false, aOcc);
                target = _e.Data[pr.A].Chord;
                grp = Grp.Chord;
                chord = true;
                break;
            case Channel.Lanes when pr.LaneSrc == 0 && win.Mel is { } mv2:
            {
                var lanes = _e.UsableLanes(sa).ToList();
                if (pr.LaneIdx >= lanes.Count) return null;
                var lv = LineView.Of(lanes[pr.LaneIdx], double.NaN, 0);
                bSeq = Features.LineSeq(mv2, s);
                aSeq = Features.LineSeq(lv, 0);
                Occ(true, mv2, s, fdbB, bpbB, bOcc);
                Occ(true, lv, 0, fdbA, bpbA, aOcc);
                target = _e.Data[pr.A].Lanes[pr.LaneIdx];
                grp = Grp.Mel;
                break;
            }
            case Channel.Lanes when pr.LaneSrc >= 1 && pr.LaneSrc - 1 < win.Lanes.Length && win.Lanes[pr.LaneSrc - 1] is { } bl && sa.Melody != null:
                bSeq = Features.LineSeq(bl, s);
                aSeq = FeaturesAt(pr.A, 0).Mel;
                Occ(true, bl, s, fdbB, bpbB, bOcc);
                Occ(true, LineView.Of(sa.Melody, double.NaN, 0), 0, fdbA, bpbA, aOcc);
                target = _e.Data[pr.A].Mel;
                grp = Grp.Mel;
                break;
            default:
                return null;
        }
        if (bSeq == null || aSeq == null || target == null) return null;
        // The shared rare n-grams (those that weigh anything for this pair).
        var docA = _e.Data[pr.A].DocSet;
        var docB = _e.Data[pr.B].DocSet;
        var shared = new HashSet<long>();
        var covered = new HashSet<int>();
        foreach (var o in bOcc)
        {
            if (!InChannel(o, grp)) continue;
            ulong u = unchecked((ulong)o.Key);
            if (!target.Contains(u)) continue;
            int inPair = (docA.Contains(u) ? 1 : 0) + (docB.Contains(u) ? 1 : 0);
            if (_e.Wt.W[_e.Wt.Dfp(_e.Df.Df(o.Key), inPair)] <= 0) continue;
            shared.Add(o.Key);
            for (int t = o.Start; t <= o.End; t++) covered.Add(t);
        }
        double a0 = double.NaN, a1 = double.NaN;
        Span<Hit> hits = stackalloc Hit[Math.Max(1, _p.Hits)];
        int nh = _al.Hits(aSeq, bSeq, chord ? _chordAp : _noteAp, hits);
        if (nh > 0)
        {
            a0 = aSeq.BeatStart(hits[0].A0);
            a1 = aSeq.BeatEnd(hits[0].A1);
        }
        else
        {
            foreach (var o in aOcc)
                if (shared.Contains(o.Key))
                {
                    a0 = double.IsNaN(a0) ? aSeq.BeatStart(o.Start) : Math.Min(a0, aSeq.BeatStart(o.Start));
                    a1 = double.IsNaN(a1) ? aSeq.BeatEnd(o.End) : Math.Max(a1, aSeq.BeatEnd(o.End));
                }
        }
        if (double.IsNaN(a0)) return null;
        return new Segment
        {
            Channel = ch, Bits = R4(pr.E[(int)ch]), Z = R4(pr.Z[(int)ch]), N = covered.Count,
            AStart = R4(a0), AEnd = R4(a1), BStart = R4(win.T0), BEnd = R4(win.T1),
        };
    }

    private bool InChannel(in GramOcc o, Grp grp) =>
        grp == Grp.BassNpc2 ? Grams.InGroup(o, Grp.BassNpc2) || _p.RiffInBass != 0 && Grams.InGroup(o, Grp.Riff) : Grams.InGroup(o, grp);

    private void Occ(bool melody, LineView v, int shift, double fdb, double bpb, List<GramOcc> outp) =>
        Grams.Line(melody, v.On, v.Pitch, fdb, bpb, shift, false, outp, melody ? _e.MelFilter : default);

    /// <summary>A counting loop channel: the best shared loop identity, spans from the loops' first visits.</summary>
    private Segment? LoopPassage(PairResult pr)
    {
        var fa = FeaturesAt(pr.A, 0);
        var fb = FeaturesAt(pr.B, pr.Shift);
        var sa = _e.Songs[pr.A];
        var sb = _e.Songs[pr.B];
        foreach (var lb in fb.Loops)
            foreach (var la in fa.Loops)
            {
                if (la.C != lb.C || la.Row < 0 || lb.Row < 0) continue;
                var ra = sa.Loops[la.Row];
                var rb = sb.Loops[lb.Row];
                double a0 = ra.VisitStarts.Length > 0 ? ra.VisitStarts[0] : 0, b0 = rb.VisitStarts.Length > 0 ? rb.VisitStarts[0] : 0;
                return new Segment
                {
                    Channel = Channel.Loop, Bits = R4(pr.E[(int)Channel.Loop]), Z = R4(pr.Z[(int)Channel.Loop]), N = rb.Own.Length,
                    Loop = rb.Roman, SamePhase = la.CP == lb.CP,
                    AStart = R4(a0), AEnd = R4(a0 + ra.VisitBeats), BStart = R4(b0), BEnd = R4(b0 + rb.VisitBeats),
                };
            }
        return null;
    }

    // ------------------------------------------------------------------------------------------------ versions
    /// <summary>
    /// DESIGN §8.9: duration ratio 0.6-1.6, then global chord identity, then melody PMI (each only when the
    /// previous test passes), at shift 0 and at the pair's V8 shift (the better of the two).
    /// </summary>
    public void VersionMeasures(PairResult r)
    {
        var a = _e.Songs[r.A];
        var b = _e.Songs[r.B];
        double da = a.DurationS > 0 && b.DurationS > 0 ? a.DurationS : a.EndBeat;
        double db = a.DurationS > 0 && b.DurationS > 0 ? b.DurationS : b.EndBeat;
        r.DurationRatio = da > 0 ? db / da : double.NaN;
        if (!(r.DurationRatio >= _p.DurationRatioMin && r.DurationRatio <= _p.DurationRatioMax)) return;
        var fa = FeaturesAt(r.A, 0);
        double bestCid = double.NaN, bestPmi = double.NaN;
        foreach (int s in r.Shift == 0 ? [0] : new[] { 0, r.Shift })
        {
            var fb = FeaturesAt(r.B, s);
            if (fa.Chord == null || fb.Chord == null) continue;
            double cid = _nw.Pid(fa.Chord.Pitch.AsSpan(0, fa.Chord.N), fb.Chord.Pitch.AsSpan(0, fb.Chord.N), _p.NwGapOpen, _p.NwGapExtend);
            double pmi = double.NaN;
            if (cid >= _p.ChordIdentityMin) pmi = Pmi(fa, fb);
            if (double.IsNaN(bestCid) || cid + (double.IsNaN(pmi) ? 0 : pmi) > bestCid + (double.IsNaN(bestPmi) ? 0 : bestPmi))
            {
                bestCid = cid;
                bestPmi = pmi;
            }
        }
        r.ChordId = bestCid;
        if (!double.IsNaN(bestPmi)) r.Pmi = bestPmi;
        r.Version = r.ChordId >= _p.ChordIdentityMin && r.Pmi >= _p.PmiMin;
    }

    /// <summary>Melody PMI of the pair at its shift (Savage: pitch classes, global NW identity), for display.</summary>
    public void FillPmi(PairResult r)
    {
        if (!double.IsNaN(r.Pmi)) return;
        r.Pmi = Pmi(FeaturesAt(r.A, 0), FeaturesAt(r.B, r.Shift));
    }

    private double Pmi(Features fa, Features fb)
    {
        if (fa.Mel == null || fb.Mel == null) return double.NaN;
        var pa = new int[fa.Mel.N];
        var pb = new int[fb.Mel.N];
        for (int i = 0; i < pa.Length; i++) pa[i] = ((fa.Mel.Pitch[i] % 12) + 12) % 12;
        for (int i = 0; i < pb.Length; i++) pb[i] = ((fb.Mel.Pitch[i] % 12) + 12) % 12;
        return _nw.Pid(pa, pb, _p.NwGapOpen, _p.NwGapExtend);
    }
}
