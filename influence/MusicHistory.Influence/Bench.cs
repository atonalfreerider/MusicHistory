using System.Diagnostics;

namespace MusicHistory.Influence;

/// <summary>
/// <c>bench --db &lt;pipeline db&gt;</c>: single-thread throughput of the alignment kernels and of one
/// pair scoring, on the songs of a pipeline DB (usually a fixture). Used to size the full run.
/// </summary>
internal static class Bench
{
    public static int Run(RunOptions o, Params p, int pairs, TextWriter log)
    {
        using var conn = PipelineDb.Open(o.Db);
        var songs = PipelineDb.LoadSongs(conn, new LoadStats());
        var sc = new NgramScratch();
        foreach (var s in songs) s.F = Features.Build(s, 0, sc, full: true, p.NullDistinct);
        var corpus = Corpus.Build(songs, p);
        var scoring = new Scoring(p);
        var al = new LocalAligner();
        var hits = new Hit[p.Hits];
        var rng = new Rng(1);
        var idx = Enumerable.Range(0, pairs).Select(_ => (rng.Below(songs.Length), rng.Below(songs.Length))).ToArray();
        foreach (var (name, pick, isChord) in new (string, Func<Song, Seq?>, bool)[]
                 {
                     ("melody", s => s.F.Mel, false), ("bass", s => s.F.Bass, false), ("chord", s => s.F.Chord, true),
                 })
        {
            // Warm up (tiering), then measure.
            for (int rep = 0; rep < 2; rep++)
            {
                al.Cells = 0;
                var sw = Stopwatch.StartNew();
                foreach (var (a, b) in idx)
                {
                    var sa = pick(songs[a]);
                    var sb = pick(songs[b]);
                    if (sa == null || sb == null) continue;
                    var ap = isChord ? new AlignParams(scoring.ChordSub, 36, scoring.ChordOpen, scoring.ChordExt, 0, scoring.MinHit)
                        : new AlignParams(scoring.MelSub, 48, scoring.MelOpen, scoring.MelExt, scoring.Cons, scoring.MinHit);
                    al.Hits(sa, sb, ap, hits);
                }
                if (rep == 1) log.WriteLine($"{name}: {al.Cells / sw.Elapsed.TotalSeconds / 1e6:F1} M cells/s single thread ({al.Cells / 1e6:F0} M cells)");
            }
        }
        // AVX2 lockstep kernel vs scalar kernel on surrogates of one song (must be identical).
        {
            var w0 = new Worker(songs.Length, p);
            var ap = new AlignParams(scoring.MelSub, 48, scoring.MelOpen, scoring.MelExt, scoring.Cons, scoring.MinHit);
            int mismatches = 0, lanes = 0;
            long scalarCells = 0, simdCells = 0;
            double scalarS = 0, simdS = 0;
            var r2 = new Rng(5);
            var bests = new Best[8];
            for (int t = 0; t < Math.Max(4, pairs / 20); t++)
            {
                var sa = songs[r2.Below(songs.Length)];
                var sb = songs[r2.Below(songs.Length)];
                if (sa.Melody == null || sb.Melody == null) continue;
                var batch = w0.SurBatch;
                for (int l = 0; l < 8; l++) Surrogates.Notes(sb.Melody, sb.F.MelMarkov!, 0, ref r2, batch[l], true, w0.Scratch, false);
                var sw1 = Stopwatch.StartNew();
                long c0 = al.Cells;
                al.Best8(sa.F.Mel!, batch, 8, ap, bests);
                simdS += sw1.Elapsed.TotalSeconds;
                simdCells += al.Cells - c0;
                for (int l = 0; l < 8; l++)
                {
                    sw1.Restart();
                    c0 = al.Cells;
                    var bb = al.BestIn(sa.F.Mel!, batch[l], ap, 1, sa.F.Mel!.N, 1, batch[l].N);
                    scalarS += sw1.Elapsed.TotalSeconds;
                    scalarCells += al.Cells - c0;
                    lanes++;
                    if (bb.Score != bests[l].Score || bb.I0 != bests[l].I0 || bb.J0 != bests[l].J0 || bb.I != bests[l].I || bb.J != bests[l].J) mismatches++;
                }
            }
            log.WriteLine($"AVX2 {(LocalAligner.Simd ? "on" : "off")}: {simdCells / simdS / 1e6:F0} M cells/s vs scalar {scalarCells / scalarS / 1e6:F0} M cells/s; " +
                          $"{mismatches} of {lanes} lanes differ from the scalar kernel");
        }
        var scorer = new PairScorer(p, corpus, songs);
        var w = new Worker(songs.Length, p);
        var ordered = idx.Select(t => t.Item1 < t.Item2 ? t : (t.Item2, t.Item1)).Where(t => t.Item1 != t.Item2).Take(Math.Max(1, pairs / 10)).ToArray();
        var sw2 = Stopwatch.StartNew();
        long before = w.Aligner.Cells;
        foreach (var (a, b) in ordered) scorer.Score(songs[a], songs[b], w);
        log.WriteLine($"pair scoring: {sw2.Elapsed.TotalMilliseconds / ordered.Length:F1} ms per pair single thread, " +
                      $"{(w.Aligner.Cells - before) / (double)ordered.Length / 1e6:F2} M cells per pair, {w.Surrogates / (double)ordered.Length:F0} surrogates per pair " +
                      $"({w.PairsConfirmed} of {ordered.Length} confirmed)");
        return 0;
    }
}
