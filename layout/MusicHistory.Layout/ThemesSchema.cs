namespace MusicHistory.Layout;

/// <summary>
/// The lyric themes graph database of DESIGN.md §12 (<c>data/graph/themes_graph.db</c>). The
/// themes stage (Python) writes <see cref="Graph"/> with NULL song positions; the
/// <c>themes</c> subcommand writes the anchor and song positions and owns
/// <see cref="LayoutRun"/>. The <c>themes-demo</c> generator writes all of it. A unit test
/// compares this copy with the SQL block of DESIGN.md §12.
///
/// SQLite 3.15 compatible (Unity's native sqlite3.dll): no STRICT, generated columns, window
/// functions, UPSERT or RETURNING; journal_mode=DELETE.
/// </summary>
internal static class ThemesSchema
{
    public const int SchemaVersion = 1;

    public const string Graph = """
        CREATE TABLE themes_meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE theme_anchor(anchor_id INTEGER PRIMARY KEY, label TEXT NOT NULL, short TEXT NOT NULL,
          angle REAL NOT NULL, position_x REAL NOT NULL, position_y REAL NOT NULL, position_z REAL NOT NULL);
        CREATE TABLE theme_song(node_id INTEGER PRIMARY KEY,
          work_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL, artist TEXT NOT NULL, year INTEGER NOT NULL,
          singer_gender TEXT NOT NULL, text_source TEXT NOT NULL,
          top_anchor INTEGER NOT NULL, top_score REAL NOT NULL,
          position_x REAL, position_y REAL, position_z REAL,
          midi_path TEXT, excerpt_start_beat REAL, excerpt_end_beat REAL, tonic_pc INTEGER, mode TEXT,
          native_bpm REAL, beats_per_bar REAL, first_downbeat REAL);
        CREATE TABLE theme_score(node_id INTEGER NOT NULL, anchor_id INTEGER NOT NULL, score REAL NOT NULL,
          PRIMARY KEY(node_id, anchor_id));
        """;

    /// <summary>The table the <c>themes</c> layout owns (one row per run; readers take the largest run_id).</summary>
    public const string LayoutRun = """
        CREATE TABLE themes_layout_run(run_id INTEGER PRIMARY KEY, created_at TEXT, device TEXT,
          iterations INTEGER, sharpen REAL, repulsion REAL, final_mean_move REAL, params_json TEXT);
        """;

    public static readonly string[] LayoutRunColumns =
        ["run_id", "created_at", "device", "iterations", "sharpen", "repulsion", "final_mean_move", "params_json"];

    public const int AnchorCount = 10;

    /// <summary>The ten themes in anchor order (ids 1..10), with the short labels the themes stage uses.</summary>
    public static readonly (int Id, string Label, string Short)[] Themes =
    [
        (1, "I would be so good to you/him/her", "So good to you"),
        (2, "I'm sad you/she/he don't/doesn't love me", "Sad you don't love me"),
        (3, "I love you/him/her", "I love you"),
        (4, "I wish you/he/she loved me", "Wish you loved me"),
        (5, "I don't need/love you/him/her", "Don't need you"),
        (6, "I hate that I love you/him/her", "Hate that I love you"),
        (7, "I miss you/him/her", "I miss you"),
        (8, "Let's all love each other", "Love each other"),
        (9, "What is going on in the world?", "What's going on"),
        (10, "Other", "Other"),
    ];

    public static readonly string[] SingerGenders = ["male", "female", "mixed", "nonbinary", "unknown", "instrumental"];

    public static readonly string[] TextSources = ["lyrics", "title"];

    /// <summary>Anchor k sits at 36°·(k−1) on the ring (degrees, as theme_anchor.angle stores it).</summary>
    public static double AngleDegrees(int anchorId) => 360.0 / AnchorCount * (anchorId - 1);

    /// <summary>
    /// Anchor position on the ring of radius <paramref name="radius"/> in the x–z plane (y = 0):
    /// x = R·cos θ, z = R·sin θ, the convention of the themes stage's export. Rounded to 1e-9 so
    /// cos 90° is written as 0, not 6e-17.
    /// </summary>
    public static (double X, double Y, double Z) AnchorPosition(int anchorId, double radius)
    {
        double a = AngleDegrees(anchorId) * Math.PI / 180.0;
        return (Math.Round(radius * Math.Cos(a), 9) + 0.0, 0.0, Math.Round(radius * Math.Sin(a), 9) + 0.0);
    }

    /// <summary>CREATE statements of <see cref="Graph"/>, one per table.</summary>
    public static IEnumerable<string> GraphStatements() =>
        Graph.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
}
