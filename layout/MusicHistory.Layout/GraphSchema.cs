namespace MusicHistory.Layout;

/// <summary>
/// The graph database of DESIGN.md §10. The influence stage writes <see cref="Graph"/>; this
/// stage writes only <see cref="LayoutTables"/> (plus the positions in <c>nodes</c>). The demo
/// generator writes all of it. A unit test compares this copy with the SQL block in
/// docs/DESIGN.md so the two cannot drift apart silently.
///
/// Unity reads the file with its native sqlite3.dll 3.15.0, so nothing here may use a later
/// feature: no STRICT tables, generated columns, window functions, UPSERT or RETURNING, and
/// the journal stays in DELETE mode (WAL would make the file unreadable read-only).
/// </summary>
internal static class GraphSchema
{
    public const int SchemaVersion = 1;

    public const string Graph = """
        CREATE TABLE graph_meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE nodes(id INTEGER PRIMARY KEY,
          position_x REAL, position_y REAL, position_z REAL);
        CREATE TABLE song_node(
          node_id INTEGER PRIMARY KEY,
          work_id TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL, artist TEXT NOT NULL,
          year INTEGER NOT NULL, release_date TEXT, date_precision INTEGER,
          time_value REAL NOT NULL, canon_rank INTEGER,
          tonic_pc INTEGER NOT NULL, mode TEXT NOT NULL, key_name TEXT NOT NULL,
          norm_shift INTEGER NOT NULL,
          native_bpm REAL NOT NULL, beats_per_bar REAL NOT NULL, first_downbeat REAL NOT NULL,
          midi_path TEXT NOT NULL,
          normalized_midi_path TEXT,
          midi_source TEXT,
          excerpt_start_beat REAL NOT NULL, excerpt_end_beat REAL NOT NULL,
          tree_parent_node INTEGER, tree_root_node INTEGER NOT NULL, tree_depth INTEGER NOT NULL,
          ref_count INTEGER NOT NULL, ref_norm REAL, katz REAL, descendants INTEGER NOT NULL,
          in_degree INTEGER NOT NULL, out_degree INTEGER NOT NULL,
          key_confidence REAL, melody_confidence REAL,
          main_loop TEXT,
          summary TEXT
        );
        CREATE TABLE influence_edges(
          id INTEGER PRIMARY KEY,
          source_node INTEGER NOT NULL, target_node INTEGER NOT NULL,
          kind TEXT NOT NULL,
          channels TEXT NOT NULL,
          primary_channel TEXT NOT NULL,
          score_bits REAL NOT NULL, z REAL NOT NULL, q REAL,
          similarity REAL NOT NULL,
          weight REAL NOT NULL,
          src_start_beat REAL, src_end_beat REAL, dst_start_beat REAL, dst_end_beat REAL,
          evidence TEXT,
          UNIQUE(source_node, target_node), CHECK(source_node < target_node));
        """;

    /// <summary>The two tables this stage owns (DESIGN.md §10, "written by layout").</summary>
    public const string LayoutTables = """
        CREATE TABLE node_layout_metadata(node_id INTEGER PRIMARY KEY, mass REAL, is_root INTEGER,
          tree_depth INTEGER, display_radius REAL, time_axis_value REAL);
        CREATE TABLE layout_run(run_id INTEGER PRIMARY KEY, created_at TEXT, device TEXT,
          iterations INTEGER, loop_ms REAL, final_mean_move REAL, time_axis TEXT, time_direction TEXT,
          year_scale REAL, min_time REAL, params_json TEXT);
        """;

    public static readonly string[] NodeLayoutMetadataColumns =
        ["node_id", "mass", "is_root", "tree_depth", "display_radius", "time_axis_value"];

    public static readonly string[] LayoutRunColumns =
    [
        "run_id", "created_at", "device", "iterations", "loop_ms", "final_mean_move", "time_axis", "time_direction",
        "year_scale", "min_time", "params_json",
    ];

    /// <summary>CREATE statement of one layout table (split out of <see cref="LayoutTables"/>).</summary>
    public static string LayoutTable(string name)
    {
        foreach (string stmt in LayoutTables.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
            if (stmt.StartsWith($"CREATE TABLE {name}(", StringComparison.Ordinal))
                return stmt;
        throw new ArgumentException($"no layout table '{name}'", nameof(name));
    }
}
