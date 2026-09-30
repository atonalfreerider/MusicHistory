using Microsoft.Data.Sqlite;

namespace MusicHistory.Influence.Tests;

/// <summary>The embedded DDL must equal the foundation's: db.py SCHEMA and DESIGN.md §10.</summary>
public class SchemaTests
{
    internal static string RepoRoot()
    {
        for (var d = new DirectoryInfo(AppContext.BaseDirectory); d != null; d = d.Parent)
            if (File.Exists(Path.Combine(d.FullName, "docs", "DESIGN.md")) && File.Exists(Path.Combine(d.FullName, "musichistory", "db.py")))
                return d.FullName;
        throw new InvalidOperationException("repository root not found above " + AppContext.BaseDirectory);
    }

    private static string Norm(string s) =>
        string.Join("\n", s.Replace("\r\n", "\n").Split('\n').Select(l => l.TrimEnd())).Trim('\n');

    [Fact]
    public void PipelineSchemaIsAVerbatimCopyOfDbPy()
    {
        string py = File.ReadAllText(Path.Combine(RepoRoot(), "musichistory", "db.py"));
        int start = py.IndexOf("SCHEMA = r\"\"\"", StringComparison.Ordinal) + "SCHEMA = r\"\"\"".Length;
        int end = py.IndexOf("\"\"\"", start, StringComparison.Ordinal);
        Assert.Equal(Norm(py[start..end]), Norm(Schema.Pipeline));
        Assert.Contains("SCHEMA_VERSION = " + Schema.PipelineSchemaVersion, py);
    }

    private static List<string> Columns(SqliteConnection c, string table)
    {
        using var cmd = c.CreateCommand();
        cmd.CommandText = $"PRAGMA table_info({table})";
        using var r = cmd.ExecuteReader();
        var cols = new List<string>();
        while (r.Read()) cols.Add($"{r.GetString(1)}|{r.GetString(2)}|{r.GetInt32(3)}|{r.GetInt32(5)}");
        return cols;
    }

    private static List<string> UniqueIndexes(SqliteConnection c, string table)
    {
        var outp = new List<string>();
        using var cmd = c.CreateCommand();
        cmd.CommandText = $"PRAGMA index_list({table})";
        var names = new List<(string, int)>();
        using (var r = cmd.ExecuteReader())
            while (r.Read()) names.Add((r.GetString(1), r.GetInt32(2)));
        foreach (var (name, unique) in names)
        {
            using var c2 = c.CreateCommand();
            c2.CommandText = $"PRAGMA index_info('{name}')";
            using var r2 = c2.ExecuteReader();
            var cols = new List<string>();
            while (r2.Read()) cols.Add(r2.GetString(2));
            outp.Add($"{unique}:{string.Join(",", cols)}");
        }
        outp.Sort(StringComparer.Ordinal);
        return outp;
    }

    [Fact]
    public void GraphSchemaMatchesDesign()
    {
        string design = File.ReadAllText(Path.Combine(RepoRoot(), "docs", "DESIGN.md"));
        int start = design.IndexOf("CREATE TABLE graph_meta", StringComparison.Ordinal);
        int end = design.IndexOf("```", start, StringComparison.Ordinal);
        using var d = new SqliteConnection("Data Source=:memory:");
        using var o = new SqliteConnection("Data Source=:memory:");
        d.Open();
        o.Open();
        using (var cmd = d.CreateCommand())
        {
            cmd.CommandText = design[start..end];
            cmd.ExecuteNonQuery();
        }
        using (var cmd = o.CreateCommand())
        {
            cmd.CommandText = Schema.Graph;
            cmd.ExecuteNonQuery();
        }
        foreach (var t in new[] { "graph_meta", "nodes", "song_node", "influence_edges" })
        {
            Assert.Equal(Columns(d, t), Columns(o, t));
            Assert.Equal(UniqueIndexes(d, t), UniqueIndexes(o, t));
        }
        // The CHECK constraint is part of the contract.
        Assert.Contains("CHECK(source_node < target_node)", Schema.Graph);
        Assert.DoesNotContain("STRICT", Schema.Graph);
        Assert.DoesNotContain("GENERATED", Schema.Graph, StringComparison.OrdinalIgnoreCase);
    }
}
