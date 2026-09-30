using System.Globalization;
using System.Text;
using System.Text.Json;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Influence;

/// <summary>
/// <c>make-fixture</c>: a synthetic pipeline DB in the db.py schema with realistic sizes (lead
/// ~400 notes, bass ~300, ~150 chord changes, verse/chorus form with repeats) and planted truth
/// in <c>known_influence</c> (note JSON carries <c>"fixture": true</c>):
/// <list type="bullet">
/// <item>melody: a later song reuses an earlier song's 8-bar chorus melody (sometimes with its
///   pre-chorus), with transcription noise (altered pitches, split and dropped notes);</item>
/// <item>bass: a later song plays an earlier song's 2-bar bass riff through its verses;</item>
/// <item>loop: a later song's chorus uses an earlier song's rare chromatic 6-8 chord loop;</item>
/// <item>fifth: a melody plant whose child is mis-normalized by a fifth (key_ambiguous_fifth = 1);</item>
/// <item>version: a near-identical later rendition (expected relation 'version', no edge);</item>
/// <item>melody_same_year: a melody plant within one year at year precision (expected: no edge);</item>
/// <item>negatives: pairs sharing only ubiquitous loops (axis, doo-wop, blues, ...) and scale runs.</item>
/// </list>
/// Everything is already in the normalized C major / A minor frame, as the analyze stage writes it.
/// </summary>
internal static class Fixture
{
    private sealed class Note
    {
        public double On, Dur;
        public int Pitch;
    }

    private sealed class Harmony
    {
        public int[] Tokens = [];
        public double ChordBeats;
        public string Name = "";
        public bool Loop;
    }

    private sealed class Sec
    {
        public string Kind = "";
        public int Bars;
        public double Start;
    }

    private sealed class G
    {
        public int I;
        public string WorkId = "", Title = "", Artist = "";
        public string? OrigArtist;
        public int Year, Prec;
        public string? Date, Chart;
        public bool Minor, Fifth, Rap;
        public int Tonic, Shift, BassPattern;
        public double Bpm, Bpb, Fd, KeyConf, MelConf;
        public string Source = "lakh";
        public List<Sec> Secs = [];
        public Dictionary<string, Harmony> Harm = [];
        public Dictionary<string, List<Note>> Phrase = [];
        public List<Note>? Riff;
        public string Role = "";
        public List<(double S, double E, int Tok)> Chords = [];
        public List<Note> Mel = [], Bass = [];
        public double EndBeat;
    }

    // L1 tokens in the C / Am frame.
    private const int I = 0, ii = 7, iii = 13, IV = 15, V = 21, vi = 28, vii = 35, bVII = 30, bIII = 9, bVI = 24, iv = 16, II = 6, III = 12, VI = 27, bII = 3;

    private static readonly (string Name, int[] Tokens, double Weight)[] Common =
    [
        ("axis", [I, V, vi, IV], 14), ("axis_vi", [vi, IV, I, V], 8), ("axis_IV", [IV, I, V, vi], 2),
        ("doowop", [I, vi, IV, V], 10), ("I_IV_V_IV", [I, IV, V, IV], 5), ("I_IV", [I, IV], 5), ("I_V", [I, V], 3),
        ("mixolydian", [I, bVII, IV], 4), ("andalusian", [vi, V, IV, III], 4), ("fifties", [I, vi, ii, V], 4),
        ("ii_V_I", [ii, V, I], 3), ("vi_V_IV_V", [vi, V, IV, V], 3), ("I_vi", [I, vi], 2), ("I_iii_IV", [I, iii, IV], 2),
        ("royal_road", [IV, V, iii, vi], 2), ("i_iv", [vi, ii], 3), ("blues", [I, I, I, I, IV, IV, I, I, V, IV, I, V], 6),
    ];

    private static readonly int[] Diatonic = [I, ii, iii, IV, V, vi];
    private static readonly int[] Chromatic = [bVII, bIII, bVI, iv, II, III, VI, bII];
    private static readonly int[] MajorScale = [0, 2, 4, 5, 7, 9, 11];
    private static readonly int[] MinorScale = [0, 2, 3, 5, 7, 8, 10];

    private static readonly double[][] Rhythm4 =
    [
        [1, 1, 1, 1], [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5], [1, 0.5, 0.5, 1, 1], [1.5, 0.5, 1, 1], [0.5, 1, 0.5, 1, 1],
        [2, 1, 1], [1, 1, 2], [0.5, 0.5, 1, 0.5, 0.5, 1], [0.75, 0.25, 1, 1, 1], [1, 1, 1, 0.5, 0.5], [0.5, 0.5, 1, 2],
        [3, 1], [2, 2], [1.0 / 3, 1.0 / 3, 1.0 / 3, 1, 1, 1], [0.25, 0.25, 0.5, 1, 1, 1], [1.5, 1.5, 1],
    ];
    private static readonly double[][] Dense =
    [
        [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5], [0.5, 0.5, 1, 0.5, 0.5, 0.5, 0.5], [0.25, 0.25, 0.5, 0.5, 0.5, 1, 0.5, 0.5],
        [0.5, 0.25, 0.25, 0.5, 0.5, 0.5, 0.5, 1], [0.75, 0.25, 0.5, 0.5, 0.5, 0.5, 1],
    ];
    private static readonly double[][] Rhythm3 = [[1, 1, 1], [0.5, 0.5, 1, 1], [2, 1], [1, 0.5, 0.5, 1], [1.5, 0.5, 1], [0.5, 0.5, 0.5, 0.5, 0.5, 0.5]];

    public static void Make(string outPath, int n, int seed, TextWriter log)
    {
        if (n < 20) throw new ArgumentException("make-fixture needs at least 20 songs");
        var rng = new Rng(0x5EED0000UL + (ulong)seed);
        var gs = new List<G>();
        for (int i = 0; i < n; i++) gs.Add(NewSong(i, ref rng));
        foreach (var g in gs) Generate(g, ref rng);
        var known = Plant(gs, ref rng);
        foreach (var g in gs) Render(g);
        Write(outPath, gs, known, seed);
        log.WriteLine($"fixture: {n} songs, {known.Count(k => k.Kind == "control_positive")} planted positives, " +
                      $"{known.Count(k => k.Kind == "control_version")} versions, {known.Count(k => k.Kind == "control_negative")} negatives -> {outPath}");
        log.WriteLine($"  mean melody notes {gs.Average(g => g.Mel.Count):F0}, bass notes {gs.Average(g => g.Bass.Count):F0}, " +
                      $"chord changes {gs.Average(g => Chg(g).Tokens.Length):F0}");
    }

    // ------------------------------------------------------------------------------ song skeletons
    private static G NewSong(int i, ref Rng rng)
    {
        var g = new G { I = i };
        ulong h = Fnv.Text("fixture-work-" + i.ToString(CultureInfo.InvariantCulture));
        g.WorkId = "R" + (h & 0xFFFFFFFFFFFFUL).ToString("x12", CultureInfo.InvariantCulture);
        g.Title = $"Fixture Song {i + 1:D4}";
        g.Artist = $"Fixture Artist {rng.Below(Math.Max(3, i / 3 + 3)) + 1:D3}";
        g.Year = 1950 + rng.Below(71);
        double u = rng.NextDouble();
        g.Prec = u < 0.45 ? 11 : u < 0.6 ? 10 : 9;
        int month = rng.Below(12) + 1, day = rng.Below(DateTime.DaysInMonth(g.Year, month)) + 1;
        g.Date = g.Prec switch
        {
            11 => $"{g.Year:D4}-{month:D2}-{day:D2}",
            10 => $"{g.Year:D4}-{month:D2}",
            _ => $"{g.Year:D4}",
        };
        if (rng.NextDouble() < 0.6)
        {
            var start = new DateOnly(g.Year, month, g.Prec == 9 ? 1 : day);
            var chart = start.AddDays(rng.Below(60));
            if (g.Prec == 9) chart = new DateOnly(g.Year, 1, 1).AddDays(rng.Below(360));
            g.Chart = chart.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        }
        g.Minor = rng.NextDouble() < 0.3;
        g.Tonic = rng.Below(12);
        g.Bpm = Math.Round(70 + rng.NextDouble() * 90, 2);
        g.Bpb = rng.NextDouble() < 0.1 ? 3 : 4;
        g.Fd = rng.NextDouble() < 0.2 ? g.Bpb : 0;
        g.Rap = rng.NextDouble() < 0.04;
        g.Fifth = rng.NextDouble() < 0.1;
        g.KeyConf = Math.Round(0.55 + 0.4 * rng.NextDouble(), 4);
        g.MelConf = Math.Round(0.45 + 0.45 * rng.NextDouble(), 3);
        double s = rng.NextDouble();
        g.Source = s < 0.6 ? "lakh" : s < 0.85 ? "midicollection" : "freemidi";
        g.BassPattern = rng.Below(5);
        return g;
    }

    private static void Generate(G g, ref Rng rng)
    {
        // Form.
        bool pc = rng.NextDouble() < 0.6, bridge = rng.NextDouble() < 0.8, doubleEnd = rng.NextDouble() < 0.6;
        int vBars = rng.NextDouble() < 0.2 ? 8 : 16;
        var form = new List<(string, int)> { ("In", 4), ("V", vBars) };
        if (pc) form.Add(("PC", 4));
        form.Add(("C", 8));
        form.Add(("V", vBars));
        if (pc) form.Add(("PC", 4));
        form.Add(("C", 8));
        if (bridge) form.Add(("Br", 8));
        form.Add(("C", 8));
        if (doubleEnd) form.Add(("C", 8));
        form.Add(("Out", 4));
        g.Secs = form.Select(f => new Sec { Kind = f.Item1, Bars = f.Item2 }).ToList();

        // Harmony per section kind.
        g.Harm["V"] = PickLoop(g, ref rng);
        g.Harm["C"] = rng.NextDouble() < 0.25 ? g.Harm["V"] : PickLoop(g, ref rng);
        g.Harm["PC"] = Walk(g, 4, ref rng);
        g.Harm["Br"] = Walk(g, 8, ref rng);
        g.Harm["In"] = g.Harm["C"];
        g.Harm["Out"] = g.Harm["C"];

        // Melody phrases per section kind (reused on every visit, like a real form).
        var m = Phrase(g, "V", 2, ref rng);
        var line = Concat(g, m, Vary(g, m, "V", ref rng), m, Vary(g, m, "V", ref rng));
        g.Phrase["V"] = vBars == 16 ? Concat(g, line, line) : line;
        if (pc) g.Phrase["PC"] = Phrase(g, "PC", 4, ref rng);
        var hook = Phrase(g, "C", 2, ref rng);
        g.Phrase["C"] = Concat(g, hook, Vary(g, hook, "C", ref rng), hook, Phrase(g, "C", 2, ref rng, barOffset: 6));
        if (bridge) g.Phrase["Br"] = Phrase(g, "Br", 8, ref rng);
        if (rng.NextDouble() < 0.18) g.Riff = GenRiff(g, ref rng);
    }

    private static Harmony PickLoop(G g, ref Rng rng)
    {
        double total = Common.Sum(c => c.Weight) + 20;
        double u = rng.NextDouble() * total;
        foreach (var c in Common)
        {
            if ((u -= c.Weight) < 0)
            {
                double u2 = rng.NextDouble();
                double beats = c.Name == "blues" ? g.Bpb : u2 < 0.85 ? g.Bpb / 2 : u2 < 0.97 ? g.Bpb : g.Bpb * 2;
                return new Harmony { Tokens = c.Tokens, ChordBeats = beats, Name = c.Name, Loop = true };
            }
        }
        // A song-specific diatonic loop of 3-4 chords.
        int len = 3 + rng.Below(2);
        var toks = new int[len];
        for (int k = 0; k < len; k++)
            do toks[k] = Diatonic[rng.Below(Diatonic.Length)];
            while (k > 0 && toks[k] == toks[k - 1] || k == len - 1 && toks[k] == toks[0]);
        return new Harmony { Tokens = toks, ChordBeats = rng.NextDouble() < 0.85 ? g.Bpb / 2 : g.Bpb, Name = "own", Loop = true };
    }

    private static Harmony Walk(G g, int bars, ref Rng rng)
    {
        var toks = new List<int>();
        int cur = Diatonic[rng.Below(Diatonic.Length)];
        bool twice = rng.NextDouble() < 0.9;
        for (int b = 0; b < (twice ? 2 * bars : bars); b++)
        {
            toks.Add(cur);
            int next;
            do next = rng.NextDouble() < 0.12 ? Chromatic[rng.Below(Chromatic.Length)] : Diatonic[rng.Below(Diatonic.Length)];
            while (next == cur);
            cur = next;
        }
        return new Harmony { Tokens = [.. toks], ChordBeats = twice ? g.Bpb / 2 : g.Bpb, Name = "walk", Loop = false };
    }

    private static Harmony RareLoop(G g, ref Rng rng)
    {
        while (true)
        {
            int len = 6 + rng.Below(3);
            var t = new int[len];
            var pool = Diatonic.Concat(Chromatic).Concat(Chromatic).ToArray();
            for (int k = 0; k < len; k++) t[k] = pool[rng.Below(pool.Length)];
            // one chord returns (a branch point for the Markov walk)
            int a = rng.Below(len), b = (a + 2 + rng.Below(len - 3)) % len;
            t[b] = t[a];
            bool ok = true;
            for (int k = 0; k < len && ok; k++) ok = t[k] != t[(k + 1) % len];
            if (!ok || t.Count(x => Chromatic.Contains(x)) < 2 || t.Distinct().Count() != len - 1) continue;
            return new Harmony { Tokens = t, ChordBeats = g.Bpb / 2, Name = "rare", Loop = true };
        }
    }

    // ------------------------------------------------------------------------------ melody
    private static int DegPitch(G g, int d)
    {
        int oct = (int)Math.Floor(d / 7.0), k = d - oct * 7;
        return (g.Minor ? 57 : 60) + 12 * oct + (g.Minor ? MinorScale : MajorScale)[k];
    }

    private static int ChordAt(G g, string kind, double relBeat)
    {
        var h = g.Harm[kind];
        int idx = (int)Math.Floor(relBeat / h.ChordBeats + 1e-9);
        return h.Tokens[((idx % h.Tokens.Length) + h.Tokens.Length) % h.Tokens.Length];
    }

    private static List<Note> Phrase(G g, string kind, int bars, ref Rng rng, int barOffset = 0)
    {
        var notes = new List<Note>();
        var rhythms = g.Bpb == 3 ? Rhythm3 : Rhythm4;
        int deg = 4 + rng.Below(4) - 2;
        for (int bar = 0; bar < bars; bar++)
        {
            double t = bar * g.Bpb;
            bool run = !g.Rap && rng.NextDouble() < 0.12;
            var tmpl = run ? Enumerable.Repeat(0.5, (int)(g.Bpb * 2)).ToArray()
                : rng.NextDouble() < 0.35 && g.Bpb == 4 ? Dense[rng.Below(Dense.Length)] : rhythms[rng.Below(rhythms.Length)];
            int dir = rng.NextDouble() < 0.5 ? 1 : -1;
            foreach (double d in tmpl)
            {
                int step;
                if (g.Rap) step = rng.NextDouble() < 0.85 ? 0 : (rng.NextDouble() < 0.5 ? 1 : -1);
                else if (run) step = dir;
                else
                {
                    double u = rng.NextDouble();
                    int mag = u < 0.15 ? 0 : u < 0.57 ? 1 : u < 0.77 ? 2 : u < 0.87 ? 3 : u < 0.93 ? 4 : u < 0.96 ? 5 : 7;
                    step = rng.NextDouble() < 0.5 ? mag : -mag;
                }
                deg += step;
                if (deg < -2) deg = -2 + (-2 - deg);
                if (deg > 11) deg = 11 - (deg - 11);
                double pos = t - bar * g.Bpb;
                if (!run && !g.Rap && (pos < 1e-9 || Math.Abs(pos - 2) < 1e-9) && rng.NextDouble() < 0.6)
                    deg = SnapToChord(g, deg, ChordAt(g, kind, (bar + barOffset) * g.Bpb + pos));
                notes.Add(new Note { On = Math.Round(t * 12) / 12, Dur = d, Pitch = DegPitch(g, deg) });
                t += d;
            }
        }
        return notes;
    }

    private static int SnapToChord(G g, int deg, int chord)
    {
        int mask = Scoring.Triad(chord);
        for (int delta = 0; delta <= 3; delta++)
            foreach (int s in new[] { deg + delta, deg - delta })
                if ((mask >> (((DegPitch(g, s) % 12) + 12) % 12) & 1) != 0) return s;
        return deg;
    }

    /// <summary>The same motif with its last bar regenerated.</summary>
    private static List<Note> Vary(G g, List<Note> motif, string kind, ref Rng rng)
    {
        double len = BarsOf(g, motif) * g.Bpb;
        var keep = motif.Where(x => x.On < len - g.Bpb - 1e-9).Select(Clone).ToList();
        var tail = Phrase(g, kind, 1, ref rng);
        foreach (var x in tail) x.On += len - g.Bpb;
        keep.AddRange(tail);
        return keep;
    }

    private static int BarsOf(G g, List<Note> phrase) => phrase.Count == 0 ? 1 : (int)Math.Ceiling((phrase[^1].On + phrase[^1].Dur) / g.Bpb - 1e-9);

    private static List<Note> Concat(G g, params List<Note>[] parts)
    {
        var outp = new List<Note>();
        double off = 0;
        foreach (var p in parts)
        {
            foreach (var x in p) outp.Add(new Note { On = x.On + off, Dur = x.Dur, Pitch = x.Pitch });
            off += BarsOf(g, p) * g.Bpb;
        }
        return outp;
    }

    private static Note Clone(Note x) => new() { On = x.On, Dur = x.Dur, Pitch = x.Pitch };

    /// <summary>Transcription noise: pitch slips of 1-2 semitones, split notes (syllables), dropped notes.</summary>
    private static List<Note> Noisy(List<Note> phrase, double pAlter, double pSplit, double pDrop, int octave, ref Rng rng)
    {
        var outp = new List<Note>();
        foreach (var x in phrase)
        {
            double u = rng.NextDouble();
            var c = new Note { On = x.On, Dur = x.Dur, Pitch = x.Pitch + 12 * octave };
            if (u < pAlter) c.Pitch += (rng.NextDouble() < 0.5 ? 1 : -1) * (1 + rng.Below(2));
            else if (u < pAlter + pSplit && x.Dur >= 0.5)
            {
                double half = Math.Round(x.Dur / 2 * 12) / 12;
                outp.Add(new Note { On = c.On, Dur = half, Pitch = c.Pitch });
                outp.Add(new Note { On = c.On + half, Dur = x.Dur - half, Pitch = c.Pitch });
                continue;
            }
            else if (u < pAlter + pSplit + pDrop && outp.Count > 0) continue;
            outp.Add(c);
        }
        return outp;
    }

    private static List<Note> GenRiff(G g, ref Rng rng)
    {
        int[] pcs = [0, 3, 5, 7, 10, 0, 7, 6, 4];
        int root = g.Minor ? 9 : 0;
        var notes = new List<Note>();
        double t = 0, len = 2 * g.Bpb;
        double[] durs = [0.5, 0.5, 1, 0.75, 0.25, 0.5, 1.5];
        int last = -1;
        while (t < len - 1e-9)
        {
            double d = Math.Min(durs[rng.Below(durs.Length)], len - t);
            int p;
            do p = 33 + ((root + pcs[rng.Below(pcs.Length)]) % 12) + (rng.NextDouble() < 0.3 ? 12 : 0);
            while (p == last && rng.NextDouble() < 0.8);
            last = p;
            notes.Add(new Note { On = Math.Round(t * 12) / 12, Dur = d, Pitch = p });
            t += d;
        }
        return notes;
    }

    // ------------------------------------------------------------------------------ plants
    private sealed record Known(string Src, string Dst, string Kind, string Note);

    private static List<Known> Plant(List<G> gs, ref Rng rng)
    {
        int n = gs.Count;
        var known = new List<Known>();
        var used = new bool[n];
        int Count(double frac) => Math.Max(1, (int)Math.Round(frac * n));
        var bySource = new Dictionary<(int, string), int>();

        G? PickChild(ref Rng r, Func<G, bool> ok)
        {
            for (int tries = 0; tries < 2000; tries++)
            {
                var c = gs[r.Below(n)];
                if (!used[c.I] && ok(c)) return c;
            }
            return null;
        }

        G? PickSource(ref Rng r, G child, string type, bool sameYear)
        {
            // Half the time reuse a source of this type that has fewer than 3 children (hubs).
            if (!sameYear && r.NextDouble() < 0.5)
            {
                var reuse = bySource.Where(kv => kv.Key.Item2 == type && kv.Value < 3).Select(kv => gs[kv.Key.Item1])
                    .Where(s => s.Year < child.Year && s.Bpb == child.Bpb).OrderBy(s => s.I).ToList();
                if (reuse.Count > 0) return reuse[r.Below(reuse.Count)];
            }
            for (int tries = 0; tries < 4000; tries++)
            {
                var s = gs[r.Below(n)];
                if (used[s.I] && !bySource.ContainsKey((s.I, type))) continue;
                if (bySource.Keys.Any(k => k.Item1 == s.I && k.Item2 != type)) continue;
                if (s.Bpb != child.Bpb || s.I == child.I) continue;
                if (sameYear ? s.Year != child.Year : s.Year >= child.Year) continue;
                return s;
            }
            return null;
        }

        void Record(G s, G c, string kind, string plant, string expect)
        {
            used[s.I] = true;
            used[c.I] = true;
            c.Role = plant;
            bySource[(s.I, plant)] = bySource.GetValueOrDefault((s.I, plant)) + 1;
            known.Add(new Known(s.WorkId, c.WorkId, kind, $"{{\"fixture\":true,\"plant\":\"{plant}\",\"expect\":\"{expect}\"}}"));
        }

        int Octave(G s, G c) => (int)Math.Round((c.Phrase["C"].Average(x => x.Pitch) - s.Phrase["C"].Average(x => x.Pitch)) / 12.0);

        foreach (var (type, frac) in new[] { ("melody", 0.06), ("bass", 0.03), ("loop", 0.03), ("fifth", 0.01), ("version", 0.015), ("melody_same_year", 0.01) })
        {
            for (int k = 0; k < Count(frac); k++)
            {
                bool same = type == "melody_same_year";
                var c = PickChild(ref rng, x => !x.Rap && x.Year >= 1953);
                if (c == null) break;
                var s = PickSource(ref rng, c, type, same);
                if (s == null || s.Rap) continue;
                switch (type)
                {
                    case "melody":
                    case "fifth":
                    case "melody_same_year":
                        c.Phrase["C"] = Noisy(s.Phrase["C"], 0.05, 0.04, 0.03, Octave(s, c), ref rng);
                        if (s.Phrase.TryGetValue("PC", out var spc) && c.Phrase.ContainsKey("PC") && rng.NextDouble() < 0.3)
                            c.Phrase["PC"] = Noisy(spc, 0.05, 0.04, 0.03, Octave(s, c), ref rng);
                        if (type == "fifth")
                        {
                            c.Shift = 7;
                            c.Fifth = true;
                        }
                        if (same)
                        {
                            s.Prec = c.Prec = 9;
                            s.Date = $"{s.Year:D4}";
                            c.Date = $"{c.Year:D4}";
                            s.Chart = c.Chart = null;
                        }
                        Record(s, c, "control_positive", type, same ? "none" : "edge");
                        break;
                    case "bass":
                        s.Riff ??= GenRiff(s, ref rng);
                        c.Riff = Noisy(s.Riff, 0.08, 0, 0, 0, ref rng);
                        Record(s, c, "control_positive", type, "edge");
                        break;
                    case "loop":
                        if (!s.Harm["C"].Name.Equals("rare", StringComparison.Ordinal))
                        {
                            var rare = RareLoop(s, ref rng);
                            s.Harm["C"] = rare;
                            s.Harm["In"] = s.Harm["Out"] = rare;
                        }
                        var rl = s.Harm["C"];
                        c.Harm["C"] = new Harmony { Tokens = rl.Tokens, ChordBeats = rng.NextDouble() < 0.3 ? c.Bpb : c.Bpb / 2, Name = "rare", Loop = true };
                        c.Harm["In"] = c.Harm["Out"] = c.Harm["C"];
                        Record(s, c, "control_positive", type, "edge");
                        break;
                    case "version":
                        c.Secs = s.Secs.Select(x => new Sec { Kind = x.Kind, Bars = x.Bars }).ToList();
                        c.Harm = new Dictionary<string, Harmony>(s.Harm);
                        c.Phrase = [];
                        foreach (var kv in s.Phrase.OrderBy(kv => kv.Key, StringComparer.Ordinal))
                            c.Phrase[kv.Key] = Noisy(kv.Value, 0.03, 0.02, 0.01, 0, ref rng);
                        c.Riff = s.Riff == null ? null : Noisy(s.Riff, 0.03, 0, 0, 0, ref rng);
                        c.BassPattern = s.BassPattern;
                        c.Minor = s.Minor;
                        c.Fd = s.Fd;
                        c.Bpm = Math.Round(s.Bpm * (0.92 + 0.16 * rng.NextDouble()), 2);
                        c.Title = s.Title + " (version)";
                        c.OrigArtist = s.Artist;
                        Record(s, c, "control_version", type, "version");
                        break;
                }
            }
        }

        // Commonplace negatives: unplanted pairs from different years sharing a ubiquitous loop.
        var groups = gs.Where(x => !used[x.I]).SelectMany(x => new[] { (x, x.Harm["V"].Name), (x, x.Harm["C"].Name) })
            .Where(t => t.Name is not ("own" or "walk" or "rare")).Distinct().GroupBy(t => t.Name).OrderBy(g => g.Key, StringComparer.Ordinal);
        var negatives = new List<Known>();
        foreach (var grp in groups)
        {
            var members = grp.Select(t => t.x).OrderBy(x => x.I).ToList();
            for (int k = 0; k < 40 && members.Count > 1; k++)
            {
                var a = members[rng.Below(members.Count)];
                var b = members[rng.Below(members.Count)];
                if (a.Year == b.Year) continue;
                if (a.Year > b.Year) (a, b) = (b, a);
                if (negatives.Any(x => x.Src == a.WorkId && x.Dst == b.WorkId)) continue;
                negatives.Add(new Known(a.WorkId, b.WorkId, "control_negative", $"{{\"fixture\":true,\"group\":\"{grp.Key}\"}}"));
            }
        }
        known.AddRange(negatives.Take(300));
        return known;
    }

    // ------------------------------------------------------------------------------ rendering
    private static void Render(G g)
    {
        double t = g.Fd;
        foreach (var s in g.Secs)
        {
            s.Start = t;
            t += s.Bars * g.Bpb;
        }
        g.EndBeat = t;
        g.Chords.Clear();
        g.Mel.Clear();
        g.Bass.Clear();
        foreach (var s in g.Secs)
        {
            var h = g.Harm[s.Kind];
            double end = s.Start + s.Bars * g.Bpb;
            int k = 0;
            for (double c = s.Start; c < end - 1e-9; c += h.ChordBeats, k++)
                g.Chords.Add((c, Math.Min(end, c + h.ChordBeats), Features.ShiftChord(h.Tokens[k % h.Tokens.Length], g.Shift)));
            if (g.Phrase.TryGetValue(s.Kind, out var ph))
                foreach (var x in ph)
                    if (x.On < s.Bars * g.Bpb - 1e-9)
                        g.Mel.Add(new Note { On = s.Start + x.On, Dur = x.Dur, Pitch = x.Pitch + g.Shift });
            if (s.Kind == "V" && g.Riff != null)
            {
                for (double r0 = s.Start; r0 < end - 1e-9; r0 += 2 * g.Bpb)
                    foreach (var x in g.Riff)
                        if (r0 + x.On < end - 1e-9) g.Bass.Add(new Note { On = r0 + x.On, Dur = x.Dur, Pitch = x.Pitch + g.Shift });
            }
            else
            {
                for (int ci = 0; ci < g.Chords.Count; ci++)
                {
                    var (cs, ce, tok) = g.Chords[ci];
                    if (cs < s.Start - 1e-9 || cs >= end - 1e-9) continue;
                    int root = 34 + ((tok / 3 - 10) % 12 + 12) % 12;   // Bb1..A2
                    BassPattern(g, cs, ce, root, tok);
                }
            }
        }
        g.Mel = Line(g.Mel);
        g.Bass = Line(g.Bass);
    }

    private static void BassPattern(G g, double s, double e, int root, int tok)
    {
        int fifth = root + ((tok % 3) == 2 ? 6 : 7);
        (double Off, int P)[] pat = g.BassPattern switch
        {
            0 => [(0, root), (1, root), (2, root), (3, root)],
            1 => [(0, root), (2, root)],
            2 => [(0, root), (1, fifth), (2, root), (3, fifth)],
            3 => [(0, root), (0.5, root), (1, root), (1.5, root), (2, root), (2.5, root), (3, root), (3.5, root)],
            _ => [(0, root), (1.5, root), (2, fifth), (3, root + 12)],
        };
        for (double bar = s; bar < e - 1e-9; bar += 4)
            foreach (var (off, p) in pat)
                if (bar + off < e - 1e-9) g.Bass.Add(new Note { On = bar + off, Dur = 0.5, Pitch = p });
    }

    /// <summary>One monophonic line as the analyze stage stores it: sorted, one note per onset, each lasting until the next.</summary>
    private static List<Note> Line(List<Note> notes)
    {
        var sorted = notes.OrderBy(x => x.On).ToList();
        var outp = new List<Note>();
        foreach (var x in sorted)
        {
            double on = Math.Round(x.On * 12) / 12;
            if (outp.Count > 0 && Math.Abs(outp[^1].On - on) < 1e-9) continue;
            outp.Add(new Note { On = on, Dur = x.Dur, Pitch = x.Pitch });
        }
        for (int i = 0; i + 1 < outp.Count; i++) outp[i].Dur = outp[i + 1].On - outp[i].On;
        return outp;
    }

    private static int Met(G g, double on)
    {
        double pos = ((on - g.Fd) % g.Bpb + g.Bpb) % g.Bpb;
        if (pos < 1e-3 || g.Bpb - pos < 1e-3) return 0;
        double r1 = pos % 1.0;
        if (r1 < 1e-3 || 1 - r1 < 1e-3) return 1;
        double r2 = pos % 0.5;
        if (r2 < 1e-3 || 0.5 - r2 < 1e-3) return 2;
        return 3;
    }

    private sealed record ChgSeq(int[] Tokens, double[] Starts, double[] Durs, int[] Down);

    private static ChgSeq Chg(G g)
    {
        var rows = new List<(int Tok, double S, double E)>();
        foreach (var (s, e, t) in g.Chords)
        {
            if (rows.Count > 0 && rows[^1].Tok == t) rows[^1] = (t, rows[^1].S, e);
            else rows.Add((t, s, e));
        }
        return new ChgSeq(rows.Select(r => r.Tok).ToArray(), rows.Select(r => Math.Round(r.S, 4)).ToArray(),
            rows.Select(r => Math.Round(r.E - r.S, 4)).ToArray(), rows.Select(r => Met(g, r.S) == 0 ? 1 : 0).ToArray());
    }

    // ------------------------------------------------------------------------------ loops (as identity/loops.py)
    private static readonly string[] MajorDegrees = ["I", "bII", "II", "bIII", "III", "IV", "#IV", "V", "bVI", "VI", "bVII", "VII"];
    private static readonly string[] MinorDegrees = ["I", "bII", "II", "III", "#III", "IV", "#IV", "V", "VI", "#VI", "VII", "#VII"];

    public static string Roman(int token, int frame = 0, bool minor = false)
    {
        int root = token / 3, q = token % 3;
        string name = (minor ? MinorDegrees : MajorDegrees)[((root - frame) % 12 + 12) % 12];
        string acc = name.TrimEnd('I', 'V');
        string num = name[acc.Length..];
        return q == 1 ? acc + num.ToLowerInvariant() : q == 2 ? acc + num.ToLowerInvariant() + "o" : acc + num;
    }

    private sealed record LoopOut(int Family, string CycleId, int Phase, string Rhythm, int[] Own, string Roman, string RomanMinor,
        double LoopBeats, int Passes, int Visits, double Coverage, double[] VisitStarts);

    private static List<LoopOut> Loops(G g)
    {
        var outp = new List<LoopOut>();
        int fam = 0;
        foreach (var kind in new[] { "V", "PC", "C", "Br" })
        {
            var visits = g.Secs.Where(s => s.Kind == kind).ToList();
            if (visits.Count == 0) continue;
            int family = fam++;
            var h = g.Harm[kind];
            if (!h.Loop) continue;
            // collapse, wrap merge, primitive period
            var toks = new List<int>();
            var durs = new List<double>();
            foreach (int t0 in h.Tokens)
            {
                int t = Features.ShiftChord(t0, g.Shift);
                if (toks.Count > 0 && toks[^1] == t) durs[^1] += h.ChordBeats;
                else { toks.Add(t); durs.Add(h.ChordBeats); }
            }
            if (toks.Count > 1 && toks[0] == toks[^1])
            {
                durs[0] += durs[^1];
                toks.RemoveAt(toks.Count - 1);
                durs.RemoveAt(durs.Count - 1);
            }
            int p = toks.Count;
            for (int q = 1; q <= toks.Count; q++)
                if (toks.Count % q == 0 && Enumerable.Range(0, toks.Count).All(i => toks[i] == toks[(i + q) % toks.Count])) { p = q; break; }
            if (p < 2 || p > 8) continue;
            var own = toks.Take(p).ToArray();
            var ownDur = durs.Take(p).ToArray();
            int k = Features.Booth(own);
            var cyc = Enumerable.Range(0, p).Select(i => own[(k + i) % p]).ToArray();
            var rhy = Enumerable.Range(0, p).Select(i => Features.DurClass(ownDur[(k + i) % p])).ToArray();
            double loopBeats = ownDur.Sum();
            double visitBeats = visits[0].Bars * g.Bpb;
            outp.Add(new LoopOut(family, string.Join(".", cyc), (p - k) % p, string.Join(".", rhy), own,
                string.Join("-", own.Select(t => Roman(t))), string.Join("-", own.Select(t => Roman(t, 9, true))),
                Math.Round(loopBeats, 4), (int)Math.Floor(visitBeats / loopBeats), visits.Count,
                visits.Sum(s => s.Bars * g.Bpb), visits.Select(s => Math.Round(s.Start, 4)).ToArray()));
        }
        return outp;
    }

    // ------------------------------------------------------------------------------ writing
    private static string Arr(IEnumerable<double> v) => "[" + string.Join(",", v.Select(x => Math.Round(x, 6).ToString("R", CultureInfo.InvariantCulture))) + "]";
    private static string Arr(IEnumerable<int> v) => "[" + string.Join(",", v.Select(x => x.ToString(CultureInfo.InvariantCulture))) + "]";

    private static void Write(string outPath, List<G> gs, List<Known> known, int seed)
    {
        string full = Path.GetFullPath(outPath);
        Directory.CreateDirectory(Path.GetDirectoryName(full)!);
        foreach (var f in new[] { full, full + "-wal", full + "-shm", full + "-journal" })
            if (File.Exists(f)) File.Delete(f);
        using var c = PipelineDb.Open(full, create: true);
        PipelineDb.Exec(c, Schema.Pipeline);
        using var tx = c.BeginTransaction();
        void Ins(string sql, params object?[] values)
        {
            using var cmd = c.CreateCommand();
            cmd.Transaction = tx;
            cmd.CommandText = sql;
            for (int i = 0; i < values.Length; i++) cmd.Parameters.AddWithValue("$" + (i + 1).ToString(CultureInfo.InvariantCulture), values[i] ?? DBNull.Value);
            cmd.ExecuteNonQuery();
        }
        Ins("INSERT INTO meta(key, value) VALUES ($1, $2)", "schema_version", Schema.PipelineSchemaVersion.ToString(CultureInfo.InvariantCulture));
        Ins("INSERT INTO meta(key, value) VALUES ($1, $2)", "normalization", "relative");
        Ins("INSERT INTO meta(key, value) VALUES ($1, $2)", "fixture_seed", seed.ToString(CultureInfo.InvariantCulture));
        var ranks = Enumerable.Range(1, gs.Count).ToArray();
        var r = new Rng(0xC0FFEEUL + (ulong)seed);
        for (int i = ranks.Length - 1; i > 0; i--)
        {
            int j = r.Below(i + 1);
            (ranks[i], ranks[j]) = (ranks[j], ranks[i]);
        }
        foreach (var g in gs)
        {
            var chg = Chg(g);
            var loops = Loops(g);
            int target = g.Minor ? 9 : 0;
            int shift = ((target - g.Tonic + 5) % 12 + 12) % 12 - 5;
            string key = Keys.Name(g.Tonic, g.Minor ? "minor" : "major");
            var mainLoop = loops.OrderByDescending(l => l.Coverage).ThenByDescending(l => l.Visits).ThenBy(l => l.Family).FirstOrDefault();
            var melPitches = g.Mel.Select(x => x.Pitch).ToArray();
            var top = chg.Tokens.Zip(chg.Durs).GroupBy(x => x.First).Select(x => (Tok: x.Key, Beats: x.Sum(y => y.Second)))
                .OrderByDescending(x => x.Beats).ThenBy(x => x.Tok).Take(6).ToList();
            string form = string.Join(" ", g.Secs.Select(s => s.Kind));
            string summary = JsonSerializer.Serialize(new Dictionary<string, object>
            {
                ["key"] = key, ["form"] = form, ["chord_changes"] = chg.Tokens.Length, ["loops"] = loops.Count,
                ["top_chords"] = top.Select(x => new object[] { Roman(x.Tok, g.Minor ? 9 : 0, g.Minor), Math.Round(x.Beats, 2) }).ToArray(),
                ["modulations"] = Array.Empty<object>(),
            });
            Ins("""
                INSERT INTO work(work_id, title, canonical_artist, original_artist, search_artists, work_date, work_date_precision,
                  work_year, effective_year, year_confidence, first_chart_week, rrf_score, canon_rank, in_pool, selected)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'high', $10, $11, $12, 1, 1)
                """, g.WorkId, g.Title, g.Artist, g.OrigArtist, JsonSerializer.Serialize(new[] { g.Artist }), g.Date, g.Prec, g.Year, g.Year,
                g.Chart, Math.Round(1.0 / (60 + ranks[g.I]), 6), ranks[g.I]);
            Ins("""
                INSERT INTO candidate(candidate_id, work_id, source, source_ref, md5, sanitized_path, valid, analyzed, chosen)
                VALUES ($1, $2, $3, $4, $5, $6, 1, 1, 1)
                """, g.I + 1, g.WorkId, g.Source, g.WorkId, Fnv.Text("md5-" + g.WorkId).ToString("x16", CultureInfo.InvariantCulture) + "0000000000000000",
                $"data/candidates/{g.WorkId}/{g.Source}__fixture.mid");
            double durS = Math.Round(g.EndBeat * 60 / g.Bpm, 3);
            Ins("""
                INSERT INTO song(work_id, candidate_id, midi_path, normalized_midi_path, patterns_path, analysis_ok, resonance_commit,
                  analyzed_at, n_bars, n_notes, duration_s, end_beat, style, form_grammar, tonic_pc, mode, key_confidence,
                  key_ambiguous_fifth, key_review, norm_shift, shift_parallel, native_bpm, beats_per_bar, first_downbeat,
                  melody_track, melody_channel, melody_method, melody_confidence, interval_entropy, n_melody_notes,
                  bass_track, bass_channel, main_loop, summary_json)
                VALUES ($1, $2, $3, $4, $5, 1, 'fixture', '2026-01-01T00:00:00Z', $6, $7, $8, $9, 'pop', $10, $11, $12, $13, $14, 0,
                  $15, $16, $17, $18, $19, 1, 2, 'name', $20, $21, $22, 2, 3, $23, $24)
                """, g.WorkId, g.I + 1, $"data/songs/{g.WorkId}/score.mid", $"data/normalized/{g.WorkId}.mid",
                $"data/songs/{g.WorkId}/analysis.json", (int)Math.Round((g.EndBeat - g.Fd) / g.Bpb), g.Mel.Count + g.Bass.Count + chg.Tokens.Length * 3,
                durS, g.EndBeat, form, g.Tonic, g.Minor ? "minor" : "major", g.KeyConf, g.Fifth ? 1 : 0, shift,
                ((0 - g.Tonic + 5) % 12 + 12) % 12 - 5, g.Bpm, g.Bpb, g.Fd, g.MelConf, PipelineDb.IntervalEntropy(melPitches), g.Mel.Count,
                mainLoop == null ? null : g.Minor ? $"{mainLoop.Roman} ({mainLoop.RomanMinor})" : mainLoop.Roman, summary);
            var cd = chg.Tokens.Select((t, i) => t * 8 + Features.DurClass(chg.Durs[i])).ToArray();
            var kf = Enumerable.Range(0, Math.Max(0, chg.Tokens.Length - 1)).Select(i =>
                ((chg.Tokens[i + 1] / 3 - chg.Tokens[i] / 3) % 12 + 12) % 12 * 9 + chg.Tokens[i] % 3 * 3 + chg.Tokens[i + 1] % 3).ToArray();
            int beats = (int)Math.Ceiling(g.EndBeat - 1e-6);
            var beatTok = new int[beats];
            Array.Fill(beatTok, -1);
            foreach (var (s, e, t) in g.Chords)
                for (int b = (int)Math.Floor(s); b < Math.Min(beats, (int)Math.Ceiling(e)); b++)
                    if (b + 0.5 >= s && b + 0.5 < e || beatTok[b] < 0) beatTok[b] = t;
            const string chordSql = "INSERT INTO chord_seq(work_id, kind, level, tokens, starts, durs, downbeat) VALUES ($1, $2, 'L1', $3, $4, $5, $6)";
            Ins(chordSql, g.WorkId, "chg", Arr(chg.Tokens), Arr(chg.Starts), Arr(chg.Durs), Arr(chg.Down));
            Ins(chordSql, g.WorkId, "cd", Arr(cd), Arr(chg.Starts), Arr(chg.Durs), Arr(chg.Down));
            Ins(chordSql, g.WorkId, "keyfree", Arr(kf), Arr(chg.Starts.Skip(1)), Arr(chg.Durs.Skip(1)), Arr(chg.Down.Skip(1)));
            Ins(chordSql, g.WorkId, "beat", Arr(beatTok), Arr(Enumerable.Range(0, beats).Select(b => (double)b)),
                Arr(Enumerable.Repeat(1.0, beats)), Arr(Enumerable.Range(0, beats).Select(b => Met(g, b) == 0 ? 1 : 0)));
            foreach (var l in loops)
                Ins("""
                    INSERT INTO loop(work_id, family, cycle_id, phase, rhythm_sig, cycle_tokens, roman, loop_beats, passes, visits,
                      coverage_beats, visit_starts) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    """, g.WorkId, l.Family, l.CycleId, l.Phase, l.Rhythm, Arr(l.Own), l.Roman, l.LoopBeats, l.Passes, l.Visits,
                    l.Coverage, Arr(l.VisitStarts));
            foreach (var (role, line) in new[] { ("melody", g.Mel), ("bass", g.Bass) })
            {
                if (line.Count == 0) continue;
                Ins("INSERT INTO melody_line(work_id, role, onsets, durs, pitches, met) VALUES ($1, $2, $3, $4, $5, $6)",
                    g.WorkId, role, Arr(line.Select(x => x.On)), Arr(line.Select(x => x.Dur)), Arr(line.Select(x => x.Pitch)),
                    Arr(line.Select(x => Met(g, x.On))));
            }
        }
        foreach (var k in known)
            Ins("INSERT OR IGNORE INTO known_influence(src_work_id, dst_work_id, kind, note) VALUES ($1, $2, $3, $4)", k.Src, k.Dst, k.Kind, k.Note);
        tx.Commit();
    }
}
