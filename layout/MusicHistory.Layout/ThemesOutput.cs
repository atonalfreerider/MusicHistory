using System.Globalization;
using System.Text.Json.Nodes;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Layout;

/// <summary>
/// Writes a themes layout into the §12 database in one transaction (journal_mode=DELETE,
/// parameterized SQL, SQLite 3.15 features only): the ten anchors' angle and positions on the
/// ring, <c>theme_song</c> positions (UPDATE by node_id), <c>themes_meta.ring_radius</c> (so the
/// meta always matches the anchors), and one new <c>themes_layout_run</c> row (history kept;
/// readers take the largest run_id).
/// </summary>
internal static class ThemesOutput
{
    /// <returns>The new themes_layout_run.run_id.</returns>
    public static long Write(string path, ThemesGraph g, ThemesParams p, ThemesResult r, JsonObject stats, string createdAt)
    {
        int bad = r.NaNCount;
        if (bad > 0)
            throw new LayoutFailedException($"the themes layout produced {bad} non-finite position(s); nothing was written");
        using var c = GraphOutput.OpenReadWrite(path);
        EnsureRunTable(c);
        using var tx = c.BeginTransaction();

        using (var cmd = c.CreateCommand())
        {
            cmd.Transaction = tx;
            cmd.CommandText = "UPDATE theme_anchor SET angle = $angle, position_x = $x, position_y = $y, position_z = $z WHERE anchor_id = $id";
            var pa = cmd.Parameters.Add("$angle", SqliteType.Real);
            var px = cmd.Parameters.Add("$x", SqliteType.Real);
            var py = cmd.Parameters.Add("$y", SqliteType.Real);
            var pz = cmd.Parameters.Add("$z", SqliteType.Real);
            var pid = cmd.Parameters.Add("$id", SqliteType.Integer);
            cmd.Prepare();
            for (int id = 1; id <= ThemesSchema.AnchorCount; id++)
            {
                var (x, y, z) = ThemesSchema.AnchorPosition(id, r.Radius);
                pa.Value = ThemesSchema.AngleDegrees(id);
                px.Value = x;
                py.Value = y;
                pz.Value = z;
                pid.Value = id;
                if (cmd.ExecuteNonQuery() != 1) throw new InvalidOperationException($"theme_anchor has no row with anchor_id {id}");
            }
        }

        using (var cmd = c.CreateCommand())
        {
            cmd.Transaction = tx;
            cmd.CommandText = "UPDATE theme_song SET position_x = $x, position_y = $y, position_z = $z WHERE node_id = $id";
            var px = cmd.Parameters.Add("$x", SqliteType.Real);
            var py = cmd.Parameters.Add("$y", SqliteType.Real);
            var pz = cmd.Parameters.Add("$z", SqliteType.Real);
            var pid = cmd.Parameters.Add("$id", SqliteType.Integer);
            cmd.Prepare();
            for (int i = 0; i < g.Count; i++)
            {
                px.Value = (double)r.X[i];
                py.Value = (double)r.Y[i] + 0.0;   // never -0
                pz.Value = (double)r.Z[i];
                pid.Value = i + 1;
                if (cmd.ExecuteNonQuery() != 1) throw new InvalidOperationException($"theme_song has no row with node_id {i + 1}");
            }
        }

        if (GraphInput.Columns(c, "themes_meta").Count > 0)
        {
            using var cmd = c.CreateCommand();
            cmd.Transaction = tx;
            cmd.CommandText = "INSERT OR REPLACE INTO themes_meta(key, value) VALUES ('ring_radius', $r)";
            cmd.Parameters.AddWithValue("$r", r.Radius.ToString("R", CultureInfo.InvariantCulture));
            cmd.ExecuteNonQuery();
        }

        long runId;
        using (var cmd = c.CreateCommand())
        {
            cmd.Transaction = tx;
            cmd.CommandText = """
                INSERT INTO themes_layout_run(created_at, device, iterations, sharpen, repulsion, final_mean_move, params_json)
                VALUES ($created, $device, $iterations, $sharpen, $repulsion, $move, $params)
                """;
            var json = p.ToJson(r.Radius);
            json["stats"] = stats;
            cmd.Parameters.AddWithValue("$created", createdAt);
            cmd.Parameters.AddWithValue("$device", r.Device);
            cmd.Parameters.AddWithValue("$iterations", r.Iterations);
            cmd.Parameters.AddWithValue("$sharpen", p.Sharpen);
            // The float option as its shortest decimal (0.35, not 0.3499999940395355).
            cmd.Parameters.AddWithValue("$repulsion", double.Parse(p.Repulsion.ToString(CultureInfo.InvariantCulture), CultureInfo.InvariantCulture));
            cmd.Parameters.AddWithValue("$move", r.FinalMeanMove);
            cmd.Parameters.AddWithValue("$params", LayoutParams.Json(json));
            cmd.ExecuteNonQuery();
            cmd.CommandText = "SELECT last_insert_rowid()";
            cmd.Parameters.Clear();
            runId = (long)cmd.ExecuteScalar()!;
        }
        tx.Commit();
        return runId;
    }

    /// <summary>Creates themes_layout_run when missing; one with other columns is dropped and recreated (this command owns it).</summary>
    private static void EnsureRunTable(SqliteConnection c)
    {
        var have = GraphInput.Columns(c, "themes_layout_run");
        if (have.Count > 0 && have.SetEquals(ThemesSchema.LayoutRunColumns)) return;
        using var cmd = c.CreateCommand();
        if (have.Count > 0)
        {
            cmd.CommandText = "DROP TABLE themes_layout_run";
            cmd.ExecuteNonQuery();
        }
        cmd.CommandText = ThemesSchema.LayoutRun;
        cmd.ExecuteNonQuery();
    }
}
