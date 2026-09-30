using System.Globalization;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Layout.Tests;

/// <summary>Temp folders, small demo graphs and SQL helpers shared by the tests.</summary>
internal sealed class TempDir : IDisposable
{
    public string Path { get; } = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "musichistory-layout-tests",
        Guid.NewGuid().ToString("N"));

    public TempDir() => Directory.CreateDirectory(Path);

    public string File(string name) => System.IO.Path.Combine(Path, name);

    public void Dispose()
    {
        SqliteConnection.ClearAllPools();
        try
        {
            Directory.Delete(Path, recursive: true);
        }
        catch (IOException)
        {
            // Best effort: a file still mapped by the OS is left for the temp cleaner.
        }
    }
}

internal static class TestSupport
{
    public const string FixedTime = "2026-01-01T00:00:00Z";

    public static string Demo(TempDir dir, string name, int nodes, int seed = 42, double roots = 0.04)
    {
        string path = dir.File(name);
        DemoGraph.Write(path, nodes, seed, FixedTime, roots);
        return path;
    }

    public static void Exec(string path, string sql)
    {
        using var c = new SqliteConnection($"Data Source={path};Pooling=False");
        c.Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = sql;
        cmd.ExecuteNonQuery();
    }

    public static object? Scalar(string path, string sql)
    {
        using var c = new SqliteConnection($"Data Source={path};Mode=ReadOnly;Pooling=False");
        c.Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = sql;
        return cmd.ExecuteScalar();
    }

    public static List<object?[]> Rows(string path, string sql)
    {
        using var c = new SqliteConnection($"Data Source={path};Mode=ReadOnly;Pooling=False");
        c.Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = sql;
        using var r = cmd.ExecuteReader();
        var rows = new List<object?[]>();
        while (r.Read())
        {
            var row = new object?[r.FieldCount];
            for (int i = 0; i < r.FieldCount; i++) row[i] = r.IsDBNull(i) ? null : r.GetValue(i);
            rows.Add(row);
        }
        return rows;
    }

    /// <summary>Canonical dump of every table (for comparing two databases' contents).</summary>
    public static string Dump(string path, params string[] tables)
    {
        var sb = new System.Text.StringBuilder();
        foreach (string t in tables)
            foreach (var row in Rows(path, $"SELECT * FROM {t} ORDER BY 1"))
                sb.AppendLine(t + "|" + string.Join("|", row.Select(v => v switch
                {
                    null => "NULL",
                    double d => d.ToString("R", CultureInfo.InvariantCulture),
                    _ => Convert.ToString(v, CultureInfo.InvariantCulture),
                })));
        return sb.ToString();
    }

    public static (int Code, string Out, string Err) Cli(params string[] args)
    {
        var o = new StringWriter();
        var e = new StringWriter();
        int code = Program.Run(args, o, e);
        return (code, o.ToString(), e.ToString());
    }

    /// <summary>The repository root (the folder holding docs/DESIGN.md), found from the test binaries.</summary>
    public static string RepoRoot()
    {
        for (var d = new DirectoryInfo(AppContext.BaseDirectory); d != null; d = d.Parent)
            if (System.IO.File.Exists(System.IO.Path.Combine(d.FullName, "docs", "DESIGN.md"))) return d.FullName;
        throw new InvalidOperationException("docs/DESIGN.md not found above " + AppContext.BaseDirectory);
    }
}
