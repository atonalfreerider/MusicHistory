using System.Globalization;
using System.Text.Json.Nodes;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Layout;

/// <summary>
/// Writes a layout into the graph database: <c>nodes</c> positions (parameterized UPDATE by id;
/// GPU-FDG concatenated locale-formatted floats into its SQL), <c>node_layout_metadata</c> (replaced)
/// and one new <c>layout_run</c> row (history is kept; readers take the largest run_id). One
/// transaction, journal_mode=DELETE, SQLite 3.15 features only.
/// </summary>
internal static class GraphOutput
{
    public static SqliteConnection OpenReadWrite(string path)
    {
        if (!File.Exists(path)) throw new UsageException($"graph database not found: {path}");
        var c = new SqliteConnection(new SqliteConnectionStringBuilder
        {
            DataSource = path, Mode = SqliteOpenMode.ReadWrite, Pooling = false,
        }.ToString());
        c.Open();
        Exec(c, "PRAGMA journal_mode=DELETE");
        return c;
    }

    /// <summary>World position of node <paramref name="i"/>: time on the chosen axis, free axes elsewhere.</summary>
    public static (double X, double Y, double Z) Position(LayoutResult r, TimeAxis axis, int i) =>
        axis == TimeAxis.Y ? (r.U[i], r.TimeCoord[i], r.V[i]) : (r.U[i], r.V[i], r.TimeCoord[i]);

    public static double DisplayRadius(int descendants, double radiusScale) => radiusScale * Math.Sqrt(1.0 + descendants);

    /// <returns>The new layout_run.run_id.</returns>
    public static long Write(string path, LayoutGraph g, LayoutParams p, LayoutResult r, JsonObject stats, string createdAt)
    {
        int bad = r.NaNCount;
        if (bad > 0)
            throw new LayoutFailedException($"the layout produced {bad} non-finite position(s); nothing was written");
        using var c = OpenReadWrite(path);
        EnsureTable(c, "node_layout_metadata", GraphSchema.NodeLayoutMetadataColumns);
        EnsureTable(c, "layout_run", GraphSchema.LayoutRunColumns);
        using var tx = c.BeginTransaction();

        using (var cmd = c.CreateCommand())
        {
            cmd.Transaction = tx;
            cmd.CommandText = "UPDATE nodes SET position_x = $x, position_y = $y, position_z = $z WHERE id = $id";
            var px = cmd.Parameters.Add("$x", SqliteType.Real);
            var py = cmd.Parameters.Add("$y", SqliteType.Real);
            var pz = cmd.Parameters.Add("$z", SqliteType.Real);
            var pid = cmd.Parameters.Add("$id", SqliteType.Integer);
            cmd.Prepare();
            for (int i = 0; i < g.Count; i++)
            {
                var (x, y, z) = Position(r, p.Axis, i);
                px.Value = x;
                py.Value = y;
                pz.Value = z;
                pid.Value = i + 1;
                if (cmd.ExecuteNonQuery() != 1) throw new InvalidOperationException($"nodes has no row with id {i + 1}");
            }
        }

        Exec(c, "DELETE FROM node_layout_metadata", tx);
        using (var cmd = c.CreateCommand())
        {
            cmd.Transaction = tx;
            cmd.CommandText = """
                INSERT INTO node_layout_metadata(node_id, mass, is_root, tree_depth, display_radius, time_axis_value)
                VALUES ($id, $mass, $root, $depth, $radius, $time)
                """;
            var pid = cmd.Parameters.Add("$id", SqliteType.Integer);
            var pm = cmd.Parameters.Add("$mass", SqliteType.Real);
            var pr = cmd.Parameters.Add("$root", SqliteType.Integer);
            var pd = cmd.Parameters.Add("$depth", SqliteType.Integer);
            var prad = cmd.Parameters.Add("$radius", SqliteType.Real);
            var pt = cmd.Parameters.Add("$time", SqliteType.Real);
            cmd.Prepare();
            for (int i = 0; i < g.Count; i++)
            {
                pid.Value = i + 1;
                pm.Value = r.Mass[i];
                pr.Value = g.Parent[i] < 0 ? 1 : 0;
                pd.Value = g.Depth[i];
                prad.Value = DisplayRadius(g.Descendants[i], p.RadiusScale);
                pt.Value = r.TimeCoord[i];
                cmd.ExecuteNonQuery();
            }
        }

        long runId;
        using (var cmd = c.CreateCommand())
        {
            cmd.Transaction = tx;
            cmd.CommandText = """
                INSERT INTO layout_run(created_at, device, iterations, loop_ms, final_mean_move, time_axis, time_direction,
                  year_scale, min_time, params_json)
                VALUES ($created, $device, $iterations, $loop, $move, $axis, $direction, $scale, $min, $params)
                """;
            var json = p.ToJson();
            json["stats"] = stats;
            cmd.Parameters.AddWithValue("$created", createdAt);
            cmd.Parameters.AddWithValue("$device", r.Device);
            cmd.Parameters.AddWithValue("$iterations", r.Iterations);
            cmd.Parameters.AddWithValue("$loop", r.LoopMs);
            cmd.Parameters.AddWithValue("$move", r.FinalMeanMove);
            cmd.Parameters.AddWithValue("$axis", p.Axis == TimeAxis.Y ? "y" : "z");
            cmd.Parameters.AddWithValue("$direction", p.Direction == TimeDirection.Up ? "up" : "down");
            cmd.Parameters.AddWithValue("$scale", p.YearScale);
            cmd.Parameters.AddWithValue("$min", g.MinTime);
            cmd.Parameters.AddWithValue("$params", LayoutParams.Json(json));
            cmd.ExecuteNonQuery();
            cmd.CommandText = "SELECT last_insert_rowid()";
            cmd.Parameters.Clear();
            runId = (long)cmd.ExecuteScalar()!;
        }
        tx.Commit();
        return runId;
    }

    /// <summary>
    /// Creates a layout table when missing. A table with other columns (GPU-FDG's social-graph
    /// node_layout_metadata had inertia/follower_count/...) is dropped and recreated: this stage owns it.
    /// </summary>
    private static void EnsureTable(SqliteConnection c, string table, string[] columns)
    {
        var have = GraphInput.Columns(c, table);
        if (have.Count > 0 && have.SetEquals(columns)) return;
        if (have.Count > 0) Exec(c, $"DROP TABLE {table}");
        Exec(c, GraphSchema.LayoutTable(table));
    }

    private static void Exec(SqliteConnection c, string sql, SqliteTransaction? tx = null)
    {
        using var cmd = c.CreateCommand();
        cmd.Transaction = tx;
        cmd.CommandText = sql;
        cmd.ExecuteNonQuery();
    }

    public static string UtcNow() => DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", CultureInfo.InvariantCulture);
}
