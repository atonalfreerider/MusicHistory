using System.Globalization;

namespace MusicHistory.Influence;

/// <summary>Family kinds of the identity lineages (graph <c>identity_family.kind</c>).</summary>
internal enum FamilyKind { Schema = 0, Loop = 1, Progression = 2, Strong = 3 }

/// <summary>One loop row of a song as a member of a loop family: its rotation and rhythm in the family's canonical rotation.</summary>
internal sealed class LoopVar
{
    public int Phase;                 // rotation of the song's loop start within the family's canonical root cycle
    public int[] Tokens = [];         // L1 tokens, canonical rotation
    public int[] Rhythm = [];         // duration classes, canonical rotation
    public double Coverage, First, VisitBeats, LoopBeats;
    public double VisitEnd = double.NaN;  // end of the visit where the cycle was first heard (not stored)
}

/// <summary>A strong match's exact passage: its span in A and in B, its length (notes, or chord changes) and its channel name.</summary>
internal readonly record struct StrongSpan(double A0, double A1, double B0, double B1, int N, string Channel);

/// <summary>A song's membership of a family.</summary>
internal sealed class Member
{
    public int Song;
    public double Strength;           // share of the song's beats the family covers (capped)
    public double First, FirstEnd;    // first visit / occurrence (beats)
    public readonly List<LoopVar> Vars = [];   // loop families: one per loop row (best-covering first)
    public string? Variant;           // progression families: e.g. "12-bar" / "16-bar"
}

/// <summary>An identity family (DESIGN.md §8b): a loop cycle, a named schema, a progression schema or a strong match.</summary>
internal sealed class Family
{
    public int Id;
    public string Key = "";
    public FamilyKind Kind;
    public string Label = "";
    public string? Roman;             // C/Am frame
    public string Channel = "loop";   // graph channel name: loop | chord | bass | melody
    public string Channels = "loop";  // strong matches: the pair's counting channels
    public readonly List<Member> Members = [];
    public double Specificity;        // log2(N / |F|)
    public double Z, Q = double.NaN;  // strong matches: fused z and tail probability
    public int Size => Members.Count;

    private Dictionary<int, Member>? _bySong;
    public Member? Of(int song)
    {
        _bySong ??= Members.ToDictionary(m => m.Song);
        return _bySong.GetValueOrDefault(song);
    }

    public void Reindex() => _bySong = null;

    public static string KindName(FamilyKind k) => k switch
    {
        FamilyKind.Schema => "schema",
        FamilyKind.Loop => "loop",
        FamilyKind.Progression => "progression",
        _ => "strong",
    };

    public static FamilyKind ParseKind(string s) => s switch
    {
        "schema" => FamilyKind.Schema,
        "loop" => FamilyKind.Loop,
        "progression" => FamilyKind.Progression,
        "strong" => FamilyKind.Strong,
        _ => throw new FormatException($"unknown family kind '{s}'"),
    };

    public override string ToString() => $"{Id} {Label} ({Size})";
}

/// <summary>
/// Builds the identity families of DESIGN.md §8b from the analyze stage's identities and the strict evidence's
/// significant pairs: loop families (quality-tolerant root cycles covering >= 8 bars, named by the
/// <see cref="Catalogue"/>), progression schemas on the bar-level chord grid (<see cref="Progressions"/>) and strong
/// matches (one family per significant pair).
/// </summary>
internal static class Families
{
    /// <summary>Beats of a song from its first downbeat to its end (at least one bar).</summary>
    public static double SongBeats(Song s)
    {
        double bpb = s.BeatsPerBar > 0 ? s.BeatsPerBar : 4.0;
        double last = s.EndBeat;
        if (s.Chords is { Count: > 0 } ch) last = Math.Max(last, ch.Starts[^1] + ch.Durs[^1]);
        return Math.Max(bpb, last - s.FirstDownbeat);
    }

    /// <summary>
    /// A loop row as (family key, variant): roots collapsed where a chord repeats its root cyclically (C-Cm-F is I-IV),
    /// reduced to the primitive period, Booth least rotation of the roots. Null for a cycle of fewer than 2 roots.
    /// </summary>
    public static (string Key, LoopVar Var)? LoopKey(LoopRow row)
    {
        var own = row.Own;
        int n = own.Length;
        if (n < 2) return null;
        var toks = new List<int>();
        var rh = new List<int>();
        for (int i = 0; i < n; i++)
        {
            int r = i < row.OwnRhythm.Length ? row.OwnRhythm[i] : 0;
            if (toks.Count > 0 && Roman.Root(toks[^1]) == Roman.Root(own[i])) rh[^1] = Math.Max(rh[^1], r);
            else
            {
                toks.Add(own[i]);
                rh.Add(r);
            }
        }
        while (toks.Count > 1 && Roman.Root(toks[0]) == Roman.Root(toks[^1]))
        {
            rh[0] = Math.Max(rh[0], rh[^1]);
            toks.RemoveAt(toks.Count - 1);
            rh.RemoveAt(rh.Count - 1);
        }
        if (toks.Count < 2) return null;
        int m = toks.Count;
        int p = m;
        for (int q = 1; q < m; q++)
        {
            if (m % q != 0) continue;
            bool rep = true;
            for (int i = q; i < m && rep; i++) rep = Roman.Root(toks[i]) == Roman.Root(toks[i - q]);
            if (rep)
            {
                p = q;
                break;
            }
        }
        if (p < 2) return null;
        var t = toks.Take(p).ToArray();
        var r2 = rh.Take(p).ToArray();
        var roots = t.Select(Roman.Root).ToArray();
        int k = Features.Booth(roots);
        var v = new LoopVar
        {
            Phase = (p - k) % p, Tokens = Roman.Rotate(t, k), Rhythm = Roman.Rotate(r2, k), Coverage = row.Coverage,
            First = row.VisitStarts.Length > 0 ? row.VisitStarts.Min() : double.NaN, VisitBeats = row.VisitBeats, LoopBeats = row.LoopBeats,
        };
        return ("loop:" + string.Join(".", Roman.Rotate(roots, k)), v);
    }

    /// <summary>Union length of [start, end) spans.</summary>
    public static double Union(IEnumerable<(double S, double E)> spans)
    {
        double total = 0, cs = double.NaN, ce = double.NaN;
        foreach (var (s, e) in spans.Where(x => x.E > x.S).OrderBy(x => x.S).ThenBy(x => x.E))
        {
            if (double.IsNaN(cs) || s > ce)
            {
                if (!double.IsNaN(cs)) total += ce - cs;
                cs = s;
                ce = e;
            }
            else ce = Math.Max(ce, e);
        }
        if (!double.IsNaN(cs)) total += ce - cs;
        return total;
    }

    /// <summary>
    /// All families of the corpus, ids assigned (kind, then size descending, then key), specificity log2(N / |F|).
    /// <paramref name="strong"/> = the strict evidence's significant pairs (A earlier than B), with their segments.
    /// </summary>
    /// <param name="strongSpans">Where each strong pair's exact passage sounds and how long it is, from its shared rare n-grams
    /// (<see cref="StrongSpans"/>); a pair without an entry uses its strongest display segment.</param>
    public static List<Family> Build(Song[] songs, IEnumerable<PairResult> strong, LineageParams lp, int minorTonic = 9,
        IReadOnlyDictionary<(int A, int B), StrongSpan>? strongSpans = null)
    {
        var fams = new Dictionary<string, Family>(StringComparer.Ordinal);
        Family Get(string key, FamilyKind kind)
        {
            if (!fams.TryGetValue(key, out var f)) fams[key] = f = new Family { Key = key, Kind = kind };
            return f;
        }

        // Loop families. Pass 1: repeating loop rows (passes >= MinPasses) whose cycle is heard in the chord changes at
        // one of their visits (the first such visit is the row's first visit).
        var rowsBy = new List<(int Song, string RootKey, LoopVar V, LoopRow Row)>();
        foreach (var s in songs)
        {
            double bpb = s.BeatsPerBar > 0 ? s.BeatsPerBar : 4.0;
            foreach (var row in s.Loops)
            {
                if (row.Passes < lp.MinPasses || LoopKey(row) is not { } lk) continue;
                double? heard = null;
                foreach (double vs in row.VisitStarts.Order())
                {
                    heard = CycleMatch.Find(s.Chords, vs - 0.5, vs + Math.Max(row.VisitBeats, bpb) + 0.5, lk.Var.Tokens, bpb);
                    if (heard != null)
                    {
                        lk.Var.First = heard.Value;
                        lk.Var.VisitEnd = vs + row.VisitBeats;
                        break;
                    }
                }
                if (heard != null) rowsBy.Add((s.Index, lk.Key, lk.Var, row));
            }
        }
        // Pass 2: the family key. Quality-tolerant (the root cycle) for cycles of 4 or more chords whose qualities differ
        // from the group's majority in at most one chord (E vs Em transcription noise); otherwise the exact chords.
        var famKey = new string[rowsBy.Count];
        foreach (var grp in rowsBy.Select((x, i) => (x, i)).GroupBy(t => t.x.RootKey))
        {
            int n = grp.First().x.V.Tokens.Length;
            int[]? maj = null;
            if (n >= 4)
            {
                // Majority chord per position over the songs (each song's best-covering row).
                var best = grp.GroupBy(t => t.x.Song).Select(g => g.OrderByDescending(t => t.x.V.Coverage).ThenBy(t => t.i).First().x.V.Tokens).ToList();
                maj = Enumerable.Range(0, n).Select(p => best.GroupBy(t => t[p]).OrderByDescending(g => g.Count()).ThenBy(g => g.Key).First().Key).ToArray();
            }
            foreach (var (x, i) in grp)
            {
                bool tolerant = maj != null && Enumerable.Range(0, n).Count(p => x.V.Tokens[p] != maj[p]) <= 1;
                famKey[i] = tolerant ? x.RootKey : x.RootKey + "|" + string.Join(".", x.V.Tokens);
            }
        }
        // Pass 3: members, >= MinLoopBars of coverage in the song.
        var loopSpans = new List<(double S, double E)>[songs.Length];
        for (int i = 0; i < songs.Length; i++) loopSpans[i] = [];
        foreach (var grp in rowsBy.Select((x, i) => (x, Key: famKey[i])).GroupBy(t => (t.x.Song, t.Key)).OrderBy(g => g.Key.Song).ThenBy(g => g.Key.Key, StringComparer.Ordinal))
        {
            var s = songs[grp.Key.Song];
            double bpb = s.BeatsPerBar > 0 ? s.BeatsPerBar : 4.0;
            var rows = grp.Select(t => t.x).ToList();
            double cov = rows.Sum(x => x.V.Coverage);
            if (cov / bpb < lp.MinLoopBars - 1e-9) continue;
            var mem = new Member { Song = s.Index, Strength = Math.Min(lp.StrengthCap, cov / SongBeats(s)) };
            foreach (var x in rows.OrderByDescending(x => x.V.Coverage).ThenBy(x => x.V.First)) mem.Vars.Add(x.V);
            var firstVar = rows.Select(x => x.V).OrderBy(v => v.First).First();
            mem.First = firstVar.First;
            mem.FirstEnd = Math.Max(firstVar.VisitEnd, firstVar.First + bpb);
            Get(grp.Key.Key, FamilyKind.Loop).Members.Add(mem);
            foreach (var x in rows)
                foreach (double vs in x.Row.VisitStarts) loopSpans[s.Index].Add((vs, vs + x.Row.VisitBeats));
        }

        foreach (var s in songs)
        {
            double beats = SongBeats(s);
            double bpb = s.BeatsPerBar > 0 ? s.BeatsPerBar : 4.0;
            var spans = loopSpans[s.Index];
            // Progression schemas on the grid (cadences only outside the loop visits above).
            var grid = ChordGrid.Of(s.Chords, s.FirstDownbeat, bpb);
            double Inside(double a, double b) => Union(spans.Select(x => (Math.Max(a, x.S), Math.Min(b, x.E))));
            var occ = new List<Occurrence>();
            occ.AddRange(Progressions.Blues(grid, lp));
            var cad = Progressions.CadenceOccurrences(s.Chords, s.FirstDownbeat, bpb, minorTonic, Inside, s.ChordsL2);
            foreach (var grp in cad.GroupBy(o => o.Key))
                if (grp.Count() >= lp.ProgMinOcc) occ.AddRange(grp);
            int tonic = s.Mode == "minor" ? minorTonic : 0;
            occ.AddRange(Progressions.ChromaticBass(s.Bass, s.FirstDownbeat, bpb, tonic, lp));
            foreach (var grp in occ.GroupBy(o => o.Key).OrderBy(g => g.Key, StringComparer.Ordinal))
            {
                var list = grp.OrderBy(o => o.Start).ToList();
                var f = Get(grp.Key, FamilyKind.Progression);
                double cov = Union(list.Select(o => (o.Start, o.End)));
                string? variant = list.Select(o => o.Variant).Where(v => v != null).GroupBy(v => v).OrderByDescending(g => g.Count())
                    .ThenBy(g => g.Key, StringComparer.Ordinal).Select(g => g.Key).FirstOrDefault();
                f.Members.Add(new Member
                {
                    Song = s.Index, Strength = Math.Min(lp.StrengthCap, cov / beats), First = list[0].Start, FirstEnd = list[0].End, Variant = variant,
                });
            }
        }

        // Strong matches: one family per significant pair.
        foreach (var pr in strong.OrderBy(x => x.B).ThenBy(x => x.A))
        {
            var sa = songs[pr.A];
            var sb = songs[pr.B];
            var seg = Excerpts.Strongest(pr);
            double a0 = seg?.AStart ?? sa.FirstDownbeat, a1 = seg?.AEnd ?? a0 + 16 * sa.BeatsPerBar;
            double b0 = seg?.BStart ?? (double.IsNaN(pr.WinStart) ? sb.FirstDownbeat : pr.WinStart);
            double b1 = seg?.BEnd ?? (double.IsNaN(pr.WinEnd) ? b0 + 16 * sb.BeatsPerBar : pr.WinEnd);
            var ch = seg?.Channel ?? Channel.Melody;
            int n = seg?.N ?? 0;
            if (strongSpans != null && strongSpans.TryGetValue((pr.A, pr.B), out var sp))
            {
                (a0, a1, b0, b1, n) = (sp.A0, sp.A1, sp.B0, sp.B1, sp.N);
                ch = (Channel)Math.Max(0, Array.IndexOf(Channels.Names, sp.Channel));
            }
            var f = Get($"strong:{sa.WorkId}>{sb.WorkId}", FamilyKind.Strong);
            f.Channel = Channels.GraphName((int)ch);
            // The pair's counting channels, always including the passage's own (primary_channel is one of channels).
            var chs = GraphExport.EdgeChannels(pr).Csv.Split(',', StringSplitOptions.RemoveEmptyEntries).Append(f.Channel).ToHashSet(StringComparer.Ordinal);
            f.Channels = string.Join(",", new[] { "melody", "bass", "chord", "loop" }.Where(chs.Contains));
            f.Label = StrongLabel(ch, n);
            f.Z = pr.Zc;
            f.Q = pr.Q;
            f.Members.Add(new Member { Song = pr.A, Strength = Math.Min(lp.StrengthCap, Math.Max(0, a1 - a0) / SongBeats(sa)), First = a0, FirstEnd = a1 });
            f.Members.Add(new Member { Song = pr.B, Strength = Math.Min(lp.StrengthCap, Math.Max(0, b1 - b0) / SongBeats(sb)), First = b0, FirstEnd = b1 });
        }

        foreach (var f in fams.Values)
        {
            f.Members.Sort((x, y) => x.Song.CompareTo(y.Song));
            f.Specificity = Math.Log2(songs.Length / (double)Math.Max(1, f.Size));
            if (f.Kind is FamilyKind.Loop) NameLoop(f, songs, minorTonic);
            else if (f.Kind is FamilyKind.Progression) NameProgression(f, minorTonic);
        }
        UniqueLabels(fams.Values);
        var ordered = fams.Values.OrderBy(f => f.Kind).ThenByDescending(f => f.Size).ThenBy(f => f.Key, StringComparer.Ordinal).ToList();
        for (int i = 0; i < ordered.Count; i++) ordered[i].Id = i + 1;
        return ordered;
    }

    /// <summary>
    /// Where a strong pair's exact shared passage sounds, and how long it is: the shared rare n-grams of the winning window
    /// (<c>score-pairs --debug</c>) merged where they overlap in B; the longest such run (most notes, then bits) is the
    /// passage in B, its note count (chord changes for chords) the label's count; in A, the stretch (4 x the passage, A may
    /// be transcribed at another tempo, within 64..96 beats: an excerpt is at most 24 bars) holding occurrences of the most
    /// of that run's n-grams (by bits; ties: the earliest). Null when the pair shares no weighted n-gram at its window.
    /// </summary>
    public static StrongSpan? StrongSpans(V8Engine e, PairResult pr)
    {
        var grams = Program.SharedGrams(e, pr.A, pr.B, pr).Select(x => x!.AsObject()).Where(x => (double?)x["bits"] > 0)
            .Select(x => (B0: (double)x["b_beat"]!, B1: (double)x["b_end_beat"]!, Bits: (double)x["bits"]!, Ch: (string)x["channel"]!,
                A: x["a_beats"]!.AsArray().Select(o => (S: (double)o![0]!, E: (double)o[1]!)).ToList()))
            .OrderBy(x => x.B0).ThenBy(x => x.B1).ToList();
        if (grams.Count == 0) return null;
        var sb = e.Songs[pr.B];
        int Notes(string ch, double lo, double hi)
        {
            if (ch == "chord") return sb.Chords == null ? 0 : sb.Chords.Starts.Count(t => t >= lo - 1e-6 && t <= hi + 1e-6);
            var line = ch == "bass" ? sb.Bass : sb.Melody;
            return line == null ? 0 : line.Onsets.Count(t => t >= lo - 1e-6 && t <= hi + 1e-6);
        }
        // Runs of overlapping n-grams in B, per channel.
        var runs = new List<(string Ch, double Lo, double Hi, List<int> Idx)>();
        foreach (var grp in grams.Select((g, i) => (g, i)).GroupBy(t => t.g.Ch).OrderBy(g => g.Key, StringComparer.Ordinal))
            foreach (var (g, i) in grp)
            {
                int r = runs.FindLastIndex(x => x.Ch == g.Ch);
                if (r >= 0 && g.B0 <= runs[r].Hi + 1e-6) runs[r] = (g.Ch, runs[r].Lo, Math.Max(runs[r].Hi, g.B1), [.. runs[r].Idx, i]);
                else runs.Add((g.Ch, g.B0, g.B1, [i]));
            }
        // The passage is taken in a channel that counts for the pair's decision when one has a run.
        var counting = Enumerable.Range(0, Channels.Count).Where(c => pr.Counting[c]).Select(c => Channels.Names[c]).ToHashSet(StringComparer.Ordinal);
        var pool = runs.Where(x => counting.Contains(x.Ch)).ToList();
        if (pool.Count == 0) pool = runs;
        var best = pool.OrderByDescending(x => Notes(x.Ch, x.Lo, x.Hi)).ThenByDescending(x => x.Idx.Sum(i => grams[i].Bits)).ThenBy(x => x.Lo).First();
        int n = Notes(best.Ch, best.Lo, best.Hi);
        // In A: the stretch holding occurrences of the most of the run's n-grams.
        double len = Math.Min(96, Math.Max(64, 4 * (best.Hi - best.Lo)));
        var occ = best.Idx.SelectMany(i => grams[i].A.Select(o => (o.S, o.E, I: i))).OrderBy(o => o.S).ToList();
        if (occ.Count == 0) return null;
        double bestW = -1, a0 = 0, a1 = 0;
        foreach (var start in occ.Select(o => o.S).Distinct())
        {
            var inside = occ.Where(o => o.S >= start - 1e-6 && o.S <= start + len).ToList();
            double w = inside.Select(o => o.I).Distinct().Sum(i => grams[i].Bits);
            if (w > bestW + 1e-9)
            {
                bestW = w;
                a0 = start;
                a1 = inside.Max(o => o.E);
            }
        }
        return new StrongSpan(a0, Math.Max(a1, a0 + 1e-3), best.Lo, Math.Max(best.Hi, best.Lo + 1e-3), n, best.Ch);
    }

    public static string StrongLabel(Channel ch, int n) => ch switch
    {
        Channel.Bass => $"exact bass riff, {n} notes",
        Channel.Chord => $"exact chord passage, {n} changes",
        Channel.Loop => "exact loop",
        _ => $"exact melody passage, {n} notes",
    };

    /// <summary>
    /// A loop family's majority chord qualities (per canonical position, over its members' best-covering rows), the
    /// rotation most members start on, the mode most members are in, and from these its label and kind.
    /// </summary>
    private static void NameLoop(Family f, Song[] songs, int minorTonic)
    {
        int n = f.Members[0].Vars[0].Tokens.Length;
        var canon = new int[n];
        for (int i = 0; i < n; i++)
            canon[i] = f.Members.Select(m => m.Vars[0].Tokens[i]).GroupBy(t => t).OrderByDescending(g => g.Count()).ThenBy(g => g.Key).First().Key;
        int phase = f.Members.Select(m => m.Vars[0].Phase).GroupBy(x => x).OrderByDescending(g => g.Count()).ThenBy(g => g.Key).First().Key;
        bool minor = f.Members.Count(m => songs[m.Song].Mode == "minor") * 2 > f.Size;
        var (label, named) = Catalogue.Name(canon, phase, minor, minorTonic);
        f.Label = label;
        f.Kind = named ? FamilyKind.Schema : FamilyKind.Loop;
        f.Roman = Roman.Seq(Roman.Rotate(canon, phase));
        f.Channel = f.Channels = "loop";
    }

    private static void NameProgression(Family f, int minorTonic)
    {
        ProgressionSchema? ps = f.Key switch
        {
            _ when f.Key == Progressions.Blues12.Key => Progressions.Blues12,
            _ when f.Key == Progressions.Blues8A.Key => Progressions.Blues8A,
            _ when f.Key == Progressions.Blues8B.Key => Progressions.Blues8B,
            _ when f.Key.StartsWith("dcb:", StringComparison.Ordinal) => Progressions.ChromaticBassSchema(f.Key),
            _ => Progressions.Cadences(minorTonic).Select(c => c.Schema).FirstOrDefault(c => c.Key == f.Key),
        };
        if (ps == null) throw new InvalidOperationException($"unknown progression family {f.Key}");
        f.Label = ps.Label;
        f.Roman = ps.Roman;
        f.Channel = f.Channels = ps.Channel;
    }

    /// <summary>Labels of non-strong families are unique (the viewer finds an edge's family by its label): a clash gets the C/Am-frame numerals.</summary>
    private static void UniqueLabels(IEnumerable<Family> fams)
    {
        foreach (var grp in fams.Where(f => f.Kind != FamilyKind.Strong).GroupBy(f => f.Label, StringComparer.Ordinal).Where(g => g.Count() > 1))
            foreach (var f in grp.OrderBy(f => f.Key, StringComparer.Ordinal).Skip(1))
                f.Label = $"{f.Label} [C/Am {f.Roman ?? f.Key}]";
    }

    public static string F(double v) => v.ToString("R", CultureInfo.InvariantCulture);
}
