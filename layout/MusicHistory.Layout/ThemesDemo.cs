using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Layout;

/// <summary>
/// Writes a synthetic lyric-themes graph in the exact DESIGN.md §12 schema, so the viewer and the
/// themes layout can be built before (or independently of) the real classifier. Everything is a
/// deterministic function of (N, seed[, source graph]): songs "Song 0001".."Song N" by
/// "Artist 001".., years 1940–2025, score vectors of four kinds — <b>peaked</b> (one theme holds
/// 0.85–0.99; "I love you" and "I miss you" most often), <b>other</b> (peaked on anchor 10),
/// <b>mixed</b> (two or three themes share most of the score) and <b>flat</b> (close to uniform) —
/// about one song in ten title-only (text_source 'title', scores pulled toward uniform, as a
/// title says less than a lyric), and singer genders in plausible proportions (instrumentals are
/// always title-only). Positions are NULL until the themes layout runs.
///
/// Playback columns are placeholders (<c>../songs/&lt;work_id&gt;/score.mid</c>, which do not exist)
/// unless <c>--from &lt;music_graph.db&gt;</c> is given: then song k borrows year, midi_path (rebased
/// to the output folder), excerpt, key, tempo and meter of the source graph's node k, so
/// click-to-play can be tried with real MIDI files. Titles, artists and work ids stay synthetic,
/// so random theme scores are never attached to a real song's name. themes_meta carries
/// <c>synthetic = 1</c> and <c>backend = synthetic</c>. No lyric text of any kind is involved.
/// </summary>
internal static class ThemesDemo
{
    public sealed record Summary(int Songs, int Lyrics, int Titles, int Peaked, int Other, int Mixed, int Flat,
        IReadOnlyDictionary<string, int> Genders, int[] TopCounts, bool Borrowed);

    private sealed class Song
    {
        public string WorkId = "";
        public int Year;
        public string Kind = "";
        public double[] Score = new double[ThemesSchema.AnchorCount];
        public string Gender = "";
        public string TextSource = "";
        public string? MidiPath;
        public double? ExcerptStart, ExcerptEnd, Bpm, BeatsPerBar, FirstDownbeat;
        public int? Tonic;
        public string? Mode;
    }

    /// <summary>Which theme dominates a peaked song (anchors 1..9; 'Other' songs are their own kind).</summary>
    private static readonly double[] PeakedPopularity = [6, 2, 20, 10, 5, 2, 12, 5, 5, 0];
    /// <summary>Themes that share a mixed song.</summary>
    private static readonly double[] MixedPopularity = [6, 5, 14, 9, 6, 4, 9, 6, 6, 8];
    private static readonly (string Gender, double Weight)[] GenderWeights =
        [("male", 0.60), ("female", 0.27), ("mixed", 0.07), ("unknown", 0.02), ("nonbinary", 0.01), ("instrumental", 0.03)];

    public static Summary Write(string path, int n, int seed, string generatedAt, double radius = ThemesParams.DefaultRadius,
        string? fromGraph = null)
    {
        if (n < 1) throw new UsageException("--songs must be >= 1");
        if (!(double.IsFinite(radius) && radius > 0)) throw new UsageException("--radius must be > 0");
        string full = Path.GetFullPath(path);
        var borrowed = fromGraph != null ? ReadPlayback(Path.GetFullPath(fromGraph), n, Path.GetDirectoryName(full)!) : null;

        var rng = new Random(seed);
        const int k = ThemesSchema.AnchorCount;
        var songs = new List<Song>(n);
        for (int s = 0; s < n; s++)
        {
            var song = new Song { WorkId = "R" + Sha1Hex($"themes-demo|{seed}|{s}")[..12], Year = SampleYear(rng) };
            double u = rng.NextDouble();
            var score = song.Score;
            if (u < 0.55)
            {
                // Peaked: one theme holds 0.85..0.99, the rest is spread at random.
                song.Kind = u < 0.38 ? "peaked" : "other";
                int top = song.Kind == "other" ? k - 1 : Weighted(rng, PeakedPopularity);
                Spread(rng, score, 1.0, exclude: top);
                double t = 0.85 + 0.14 * rng.NextDouble();
                for (int a = 0; a < k; a++) score[a] *= 1 - t;
                score[top] = t;
            }
            else if (u < 0.85)
            {
                // Mixed: two or three themes share 0.6..0.9 of the score.
                song.Kind = "mixed";
                int parts = rng.NextDouble() < 0.6 ? 2 : 3;
                var chosen = new List<int>();
                while (chosen.Count < parts)
                {
                    int a = Weighted(rng, MixedPopularity);
                    if (!chosen.Contains(a)) chosen.Add(a);
                }
                double share = 0.6 + 0.3 * rng.NextDouble();
                Spread(rng, score, 1 - share, exclude: -1);
                var split = chosen.Select(_ => 0.5 + rng.NextDouble()).ToArray();
                double total = split.Sum();
                for (int c = 0; c < parts; c++) score[chosen[c]] += share * split[c] / total;
            }
            else
            {
                // Flat: nothing stands out.
                song.Kind = "flat";
                for (int a = 0; a < k; a++) score[a] = 1 + 0.6 * rng.NextDouble();
            }

            song.Gender = GenderWeights[Weighted(rng, [.. GenderWeights.Select(g => g.Weight)])].Gender;
            bool titleOnly = song.Gender == "instrumental" || rng.NextDouble() < 0.08;
            song.TextSource = titleOnly ? "title" : "lyrics";
            if (titleOnly)
            {
                double raw = score.Sum();
                for (int a = 0; a < k; a++) score[a] = 0.5 * score[a] / raw + 0.5 / k;
            }
            double sum = score.Sum();
            for (int a = 0; a < k; a++) score[a] = Math.Round(score[a] / sum, 9);
            // Exact sum 1 after rounding: the largest score takes the remainder.
            int big = Array.IndexOf(score, score.Max());
            score[big] = Math.Round(score[big] + (1 - score.Sum()), 9);

            // Playback placeholders (own draws, so --from does not change the scores).
            song.MidiPath = $"../songs/{song.WorkId}/score.mid";
            song.BeatsPerBar = rng.NextDouble() < 0.88 ? 4 : 3;
            song.FirstDownbeat = 0;
            song.ExcerptStart = rng.Next(0, 40) * song.BeatsPerBar;
            song.ExcerptEnd = song.ExcerptStart + (8 + rng.Next(0, 17)) * song.BeatsPerBar;
            song.Tonic = rng.Next(0, 12);
            song.Mode = rng.NextDouble() < 0.32 ? "minor" : "major";
            song.Bpm = Math.Round(70 + 110 * rng.NextDouble(), 1);
            if (borrowed != null)
            {
                var b = borrowed[s];
                song.Year = b.Year;
                song.MidiPath = b.MidiPath;
                song.ExcerptStart = b.ExcerptStart;
                song.ExcerptEnd = b.ExcerptEnd;
                song.Tonic = b.Tonic;
                song.Mode = b.Mode;
                song.Bpm = b.Bpm;
                song.BeatsPerBar = b.BeatsPerBar;
                song.FirstDownbeat = b.FirstDownbeat;
            }
            songs.Add(song);
        }

        // Node ids follow (year, work_id), as §12 requires.
        songs.Sort((x, y) => x.Year != y.Year ? x.Year.CompareTo(y.Year) : string.CompareOrdinal(x.WorkId, y.WorkId));

        Directory.CreateDirectory(Path.GetDirectoryName(full)!);
        string tmp = full + ".tmp";
        foreach (string f in new[] { tmp, tmp + "-journal" })
            if (File.Exists(f)) File.Delete(f);
        var genders = new SortedDictionary<string, int>(StringComparer.Ordinal);
        var topCounts = new int[k];
        using (var c = new SqliteConnection(new SqliteConnectionStringBuilder
               { DataSource = tmp, Mode = SqliteOpenMode.ReadWriteCreate, Pooling = false }.ToString()))
        {
            c.Open();
            Exec(c, "PRAGMA journal_mode=DELETE");
            Exec(c, "PRAGMA page_size=4096");
            Exec(c, ThemesSchema.Graph);
            Exec(c, ThemesSchema.LayoutRun);
            using var tx = c.BeginTransaction();
            string I(int x) => x.ToString(CultureInfo.InvariantCulture);
            var themes = new JsonArray([.. ThemesSchema.Themes.Select(t => (JsonNode)JsonValue.Create(t.Label))]);
            var meta = new SortedDictionary<string, string?>(StringComparer.Ordinal)
            {
                ["schema_version"] = I(ThemesSchema.SchemaVersion), ["backend"] = "synthetic", ["model"] = "themes-demo",
                ["generated_at"] = generatedAt, ["song_count"] = I(n),
                ["lyrics_count"] = I(songs.Count(s => s.TextSource == "lyrics")),
                ["title_count"] = I(songs.Count(s => s.TextSource == "title")),
                ["ring_radius"] = radius.ToString("R", CultureInfo.InvariantCulture), ["angle_unit"] = "degrees",
                ["anchor_plane"] = "xz", ["midi_base"] = "relative to this file's folder", ["synthetic"] = "1",
                ["demo_seed"] = I(seed), ["demo_generator"] = "MusicHistory.Layout themes-demo", ["singer_source"] = "synthetic",
                ["themes"] = themes.ToJsonString(new JsonSerializerOptions { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping }),
            };
            if (fromGraph != null) meta["demo_playback_from"] = Path.GetFileName(fromGraph);
            Insert(c, tx, "INSERT INTO themes_meta(key, value) VALUES ($1, $2)", meta.Select(kv => new object?[] { kv.Key, kv.Value }));
            Insert(c, tx, """
                INSERT INTO theme_anchor(anchor_id, label, short, angle, position_x, position_y, position_z)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                ThemesSchema.Themes.Select(t =>
                {
                    var (x, y, z) = ThemesSchema.AnchorPosition(t.Id, radius);
                    return new object?[] { t.Id, t.Label, t.Short, ThemesSchema.AngleDegrees(t.Id), x, y, z };
                }));
            Insert(c, tx, """
                INSERT INTO theme_song(node_id, work_id, title, artist, year, singer_gender, text_source, top_anchor, top_score,
                  position_x, position_y, position_z, midi_path, excerpt_start_beat, excerpt_end_beat, tonic_pc, mode,
                  native_bpm, beats_per_bar, first_downbeat)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NULL, NULL, NULL, $10, $11, $12, $13, $14, $15, $16, $17)
                """,
                songs.Select((s, i) =>
                {
                    int top = Array.IndexOf(s.Score, s.Score.Max());
                    topCounts[top]++;
                    genders[s.Gender] = genders.GetValueOrDefault(s.Gender) + 1;
                    return new object?[]
                    {
                        i + 1, s.WorkId, string.Create(CultureInfo.InvariantCulture, $"Song {i + 1:D4}"),
                        string.Create(CultureInfo.InvariantCulture, $"Artist {1 + Convert.ToInt32(Sha1Hex(s.WorkId)[..6], 16) % Math.Max(8, n / 6):D3}"),
                        s.Year, s.Gender, s.TextSource, top + 1, s.Score[top], s.MidiPath, s.ExcerptStart, s.ExcerptEnd, s.Tonic, s.Mode,
                        s.Bpm, s.BeatsPerBar, s.FirstDownbeat,
                    };
                }));
            Insert(c, tx, "INSERT INTO theme_score(node_id, anchor_id, score) VALUES ($1, $2, $3)",
                songs.SelectMany((s, i) => Enumerable.Range(0, k).Select(a => new object?[] { i + 1, a + 1, s.Score[a] })));
            tx.Commit();
        }
        SqliteConnection.ClearAllPools();
        File.Move(tmp, full, overwrite: true);
        return new Summary(n, songs.Count(s => s.TextSource == "lyrics"), songs.Count(s => s.TextSource == "title"),
            songs.Count(s => s.Kind == "peaked"), songs.Count(s => s.Kind == "other"), songs.Count(s => s.Kind == "mixed"),
            songs.Count(s => s.Kind == "flat"), genders, topCounts, borrowed != null);
    }

    private sealed record Playback(int Year, string MidiPath, double ExcerptStart, double ExcerptEnd, int Tonic, string Mode, double Bpm,
        double BeatsPerBar, double FirstDownbeat);

    /// <summary>The first <paramref name="n"/> songs of a §10 music graph (by node id), midi paths rebased to <paramref name="outDir"/>.</summary>
    private static List<Playback> ReadPlayback(string graph, int n, string outDir)
    {
        using var c = GraphInput.OpenReadOnly(graph);
        using var cmd = c.CreateCommand();
        cmd.CommandText = """
            SELECT year, midi_path, excerpt_start_beat, excerpt_end_beat, tonic_pc, mode, native_bpm, beats_per_bar, first_downbeat
            FROM song_node ORDER BY node_id LIMIT $n
            """;
        cmd.Parameters.AddWithValue("$n", n);
        var rows = new List<Playback>();
        string srcDir = Path.GetDirectoryName(graph)!;
        using var r = cmd.ExecuteReader();
        while (r.Read())
        {
            string midi = r.GetString(1);
            string abs = Path.GetFullPath(Path.Combine(srcDir, midi.Replace('/', Path.DirectorySeparatorChar)));
            string rel = Path.GetRelativePath(outDir, abs).Replace('\\', '/');
            rows.Add(new Playback((int)r.GetInt64(0), rel, r.GetDouble(2), r.GetDouble(3), (int)r.GetInt64(4), r.GetString(5), r.GetDouble(6),
                r.GetDouble(7), r.GetDouble(8)));
        }
        if (rows.Count < n)
            throw new UsageException($"--from {graph} has {rows.Count} songs, fewer than --songs {n}");
        return rows;
    }

    /// <summary>Random exponential shares of <paramref name="mass"/> over every theme but <paramref name="exclude"/>.</summary>
    private static void Spread(Random rng, double[] score, double mass, int exclude)
    {
        double total = 0;
        for (int a = 0; a < score.Length; a++)
        {
            score[a] = a == exclude ? 0 : -Math.Log(1 - rng.NextDouble());
            total += score[a];
        }
        for (int a = 0; a < score.Length; a++) score[a] *= mass / total;
    }

    /// <summary>Years 1940–2025: sparse before the mid-1950s, as all-time lists are.</summary>
    private static int SampleYear(Random rng)
    {
        while (true)
        {
            int y = 1940 + rng.Next(0, 86);
            double w = y < 1955 ? 0.35 + 0.65 * (y - 1940) / 15.0 : 1.0;
            if (rng.NextDouble() < w) return y;
        }
    }

    private static int Weighted(Random rng, double[] w)
    {
        double x = rng.NextDouble() * w.Sum();
        for (int i = 0; i < w.Length; i++)
        {
            x -= w[i];
            if (x < 0 && w[i] > 0) return i;
        }
        for (int i = w.Length - 1; i >= 0; i--)
            if (w[i] > 0) return i;
        return w.Length - 1;
    }

    private static string Sha1Hex(string s) => Convert.ToHexStringLower(SHA1.HashData(Encoding.UTF8.GetBytes(s)));

    private static void Exec(SqliteConnection c, string sql)
    {
        using var cmd = c.CreateCommand();
        cmd.CommandText = sql;
        cmd.ExecuteNonQuery();
    }

    private static void Insert(SqliteConnection c, SqliteTransaction tx, string sql, IEnumerable<object?[]> rows)
    {
        using var cmd = c.CreateCommand();
        cmd.Transaction = tx;
        cmd.CommandText = sql;
        SqliteParameter[]? ps = null;
        foreach (var row in rows)
        {
            if (ps == null)
            {
                ps = new SqliteParameter[row.Length];
                for (int i = 0; i < row.Length; i++) ps[i] = cmd.Parameters.Add("$" + (i + 1).ToString(CultureInfo.InvariantCulture), SqliteType.Text);
            }
            for (int i = 0; i < row.Length; i++)
            {
                object? v = row[i];
                ps[i].SqliteType = v switch { int or long => SqliteType.Integer, double => SqliteType.Real, _ => SqliteType.Text };
                ps[i].Value = v ?? DBNull.Value;
            }
            cmd.ExecuteNonQuery();
        }
    }
}
