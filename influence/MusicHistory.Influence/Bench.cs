using System.Diagnostics;

namespace MusicHistory.Influence;

/// <summary>
/// <c>bench --db &lt;pipeline db&gt;</c>: single-thread cost of the V8 engine on a pipeline DB (usually a
/// fixture or a scratch copy): index build, then all earlier songs of N later songs spread over the corpus.
/// Used to size the full run.
/// </summary>
internal static class Bench
{
    public static int Run(RunOptions o, Params p, int laterSongs, TextWriter log)
    {
        using var conn = PipelineDb.Open(o.Db);
        var songs = PipelineDb.LoadSongs(conn, new LoadStats());
        var sw = Stopwatch.StartNew();
        var engine = V8Engine.Build(songs, p);
        log.WriteLine($"index: {engine.Df.Distinct:N0} keys, {engine.Df.PostingsCount:N0} postings, {engine.TotalLanes} lanes in {sw.Elapsed.TotalSeconds:F2}s ({p.Threads} threads)");
        var w = engine.NewWorker();
        int m = Math.Max(1, Math.Min(laterSongs, songs.Length - 1));
        long pairs = 0, windows = 0;
        // Warm up (tiering), then measure.
        engine.ScoreB(songs.Length - 1, w);
        sw.Restart();
        for (int k = 0; k < m; k++)
        {
            int b = songs.Length - 1 - (int)((long)k * (songs.Length - 1) / m);
            engine.ScoreB(b, w);
            pairs += b;
            windows += engine.Windows(songs[b]).Count;
        }
        double s = sw.Elapsed.TotalSeconds;
        log.WriteLine($"V8: {m} later songs, {windows} windows, {pairs} pairs in {s:F2}s single thread: {s / m * 1e3:F1} ms per later song, " +
                      $"{s / Math.Max(1, pairs) * 1e6:F1} us per pair; all {(long)songs.Length * (songs.Length - 1) / 2:N0} pairs ~ " +
                      $"{s / Math.Max(1, pairs) * songs.Length * (songs.Length - 1) / 2:F0}s single thread");
        return 0;
    }
}
