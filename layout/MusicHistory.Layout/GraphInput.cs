using System.Globalization;
using Microsoft.Data.Sqlite;

namespace MusicHistory.Layout;

/// <summary>One influence edge as the layout sees it (0-based node indices).</summary>
internal readonly record struct InputEdge(int Id, int Source, int Target, bool IsTree, double Similarity, double Weight);

/// <summary>The part of the §10 graph the layout needs, indexed by node id − 1.</summary>
internal sealed class LayoutGraph
{
    public required int Count { get; init; }
    public required double[] Time { get; init; }
    public required int[] Parent { get; init; }        // index, −1 for a root
    public required int[] Root { get; init; }
    public required int[] Depth { get; init; }
    public required int[] Descendants { get; init; }
    public required int[] RefCount { get; init; }
    public required InputEdge[] Edges { get; init; }
    public required int[] OutDegree { get; init; }     // over all influence edges
    public double MinTime { get; init; }
    public double MaxTime { get; init; }
    public int Roots => Parent.Count(p => p < 0);
    public int TreeEdges => Edges.Count(e => e.IsTree);
    public List<string> Warnings { get; } = [];
}

/// <summary>
/// Reads nodes, song_node and influence_edges from a DESIGN.md §10 graph database and refuses
/// graphs the temporal layout cannot represent. Checked, with every problem listed (first 25):
/// <list type="bullet">
/// <item>nodes.id is 1..N contiguous and every node has exactly one song_node row;</item>
/// <item>ids are ordered by time_value, which is finite;</item>
/// <item>influence edges point from an earlier to a strictly later song (source &lt; target,
///   time_value[source] &lt; time_value[target]), kind is 'tree' or 'secondary', similarity is in
///   [0, 1] and weight is finite and ≥ 0, no duplicate pairs;</item>
/// <item>every non-root has exactly one tree edge in, from tree_parent_node; roots have none;</item>
/// <item>tree_root_node, tree_depth and descendants agree with the parent links (warnings
///   under <c>--lenient</c>, where the recomputed values are used).</item>
/// </list>
/// </summary>
internal static class GraphInput
{
    private const int MaxListed = 25;

    public static SqliteConnection OpenReadOnly(string path)
    {
        if (!File.Exists(path)) throw new UsageException($"graph database not found: {path}");
        var c = new SqliteConnection(new SqliteConnectionStringBuilder
        {
            DataSource = path, Mode = SqliteOpenMode.ReadOnly, Pooling = false,
        }.ToString());
        c.Open();
        return c;
    }

    public static LayoutGraph Load(string path, bool lenient = false)
    {
        using var c = OpenReadOnly(path);
        return Load(c, lenient);
    }

    public static LayoutGraph Load(SqliteConnection c, bool lenient = false)
    {
        var problems = new Problems();
        RequireColumns(c, problems, "nodes", "id");
        RequireColumns(c, problems, "song_node", "node_id", "time_value", "tree_parent_node", "tree_root_node", "tree_depth",
            "descendants", "ref_count");
        RequireColumns(c, problems, "influence_edges", "id", "source_node", "target_node", "kind", "similarity", "weight");
        problems.ThrowIfAny("the graph database is missing tables or columns of DESIGN.md section 10");

        // nodes: 1..N contiguous.
        var ids = new List<long>();
        using (var cmd = Cmd(c, "SELECT id FROM nodes ORDER BY id"))
        using (var r = cmd.ExecuteReader())
            while (r.Read()) ids.Add(r.GetInt64(0));
        int n = ids.Count;
        if (n == 0) throw new InvalidGraphException("the graph has no nodes", ["nodes is empty"]);
        for (int i = 0; i < n; i++)
            if (ids[i] != i + 1)
            {
                problems.Add($"nodes.id must be 1..{n} contiguous: position {i + 1} holds id {ids[i]}");
                break;
            }
        problems.ThrowIfAny("node ids are not contiguous");

        var time = new double[n];
        var parent = new int[n];
        var root = new int[n];
        var depth = new int[n];
        var desc = new int[n];
        var refCount = new int[n];
        var seen = new bool[n];
        using (var cmd = Cmd(c, """
                   SELECT node_id, time_value, tree_parent_node, tree_root_node, tree_depth, descendants, ref_count
                   FROM song_node ORDER BY node_id
                   """))
        using (var r = cmd.ExecuteReader())
        {
            while (r.Read())
            {
                long id = r.GetInt64(0);
                if (id < 1 || id > n)
                {
                    problems.Add($"song_node.node_id {id} has no row in nodes");
                    continue;
                }
                int i = (int)id - 1;
                seen[i] = true;
                time[i] = Real(r, 1, problems, $"song_node {id}: time_value");
                parent[i] = r.IsDBNull(2) ? -1 : Index(r.GetInt64(2), n, problems, $"song_node {id}: tree_parent_node");
                root[i] = Index(Integer(r, 3, problems, $"song_node {id}: tree_root_node"), n, problems, $"song_node {id}: tree_root_node");
                depth[i] = (int)Integer(r, 4, problems, $"song_node {id}: tree_depth");
                desc[i] = (int)Integer(r, 5, problems, $"song_node {id}: descendants");
                refCount[i] = (int)Integer(r, 6, problems, $"song_node {id}: ref_count");
                if (refCount[i] < 0) problems.Add($"song_node {id}: ref_count {refCount[i]} is negative");
            }
        }
        int missing = seen.Count(s => !s);
        if (missing > 0)
            problems.Add($"{missing} node(s) have no song_node row (first: node {Array.IndexOf(seen, false) + 1})");
        problems.ThrowIfAny("song_node does not match nodes");

        // Time order: ids are ordered by time_value, and a tree parent is strictly earlier.
        for (int i = 1; i < n; i++)
            if (time[i] < time[i - 1])
                problems.Add($"node ids must be ordered by time_value: node {i + 1} ({Fmt(time[i])}) comes after node {i} ({Fmt(time[i - 1])})");
        for (int i = 0; i < n; i++)
        {
            int p = parent[i];
            if (p == i) problems.Add($"node {i + 1} is its own tree parent");
            else if (p >= 0 && !(time[p] < time[i]))
                problems.Add($"node {i + 1}: tree parent {p + 1} is not earlier ({Fmt(time[p])} >= {Fmt(time[i])})");
        }
        problems.ThrowIfAny("the tree does not run forward in time");

        // Edges.
        var edges = new List<InputEdge>();
        var pairs = new HashSet<(int, int)>();
        var treeIn = new int[n];
        var treeFrom = new int[n];
        var outDeg = new int[n];
        Array.Fill(treeFrom, -1);
        using (var cmd = Cmd(c, "SELECT id, source_node, target_node, kind, similarity, weight FROM influence_edges ORDER BY id"))
        using (var r = cmd.ExecuteReader())
        {
            while (r.Read())
            {
                long id = r.GetInt64(0);
                string what = $"edge {id}";
                int s = Index(Integer(r, 1, problems, what + ": source_node"), n, problems, what + ": source_node");
                int t = Index(Integer(r, 2, problems, what + ": target_node"), n, problems, what + ": target_node");
                string kind = r.IsDBNull(3) ? "" : r.GetValue(3)?.ToString() ?? "";
                double sim = Real(r, 4, null, what + ": similarity");   // range-checked below
                double w = Real(r, 5, null, what + ": weight");
                if (s < 0 || t < 0) continue;
                what = $"edge {id} ({s + 1} -> {t + 1})";
                if (s >= t) problems.Add($"{what}: source_node must be < target_node (the source is the earlier song)");
                else if (!(time[s] < time[t])) problems.Add($"{what}: source is not earlier ({Fmt(time[s])} >= {Fmt(time[t])})");
                if (kind != "tree" && kind != "secondary") problems.Add($"{what}: kind '{kind}' is not 'tree' or 'secondary'");
                if (!(sim >= 0 && sim <= 1)) problems.Add($"{what}: similarity {Fmt(sim)} is not in [0, 1]");
                if (!(double.IsFinite(w) && w >= 0)) problems.Add($"{what}: weight {Fmt(w)} is not a finite number >= 0");
                if (!pairs.Add((s, t))) problems.Add($"{what}: duplicate edge");
                bool isTree = kind == "tree";
                if (isTree)
                {
                    treeIn[t]++;
                    treeFrom[t] = s;
                }
                outDeg[s]++;
                edges.Add(new InputEdge((int)id, s, t, isTree, sim, w));
            }
        }
        for (int i = 0; i < n; i++)
        {
            if (parent[i] < 0 && treeIn[i] > 0)
                problems.Add($"node {i + 1} is a root (no tree_parent_node) but has {treeIn[i]} tree edge(s) in");
            else if (parent[i] >= 0 && treeIn[i] != 1)
                problems.Add($"node {i + 1} must have exactly one tree edge in (from {parent[i] + 1}), found {treeIn[i]}");
            else if (parent[i] >= 0 && treeFrom[i] != parent[i])
                problems.Add($"node {i + 1}: its tree edge comes from {treeFrom[i] + 1}, not from tree_parent_node {parent[i] + 1}");
        }
        problems.ThrowIfAny("influence_edges break the DESIGN.md section 10 invariants");

        // Derived tree columns. Parents precede children (earlier time => smaller id), so one
        // forward pass gives root/depth and one backward pass gives subtree sizes.
        var cRoot = new int[n];
        var cDepth = new int[n];
        var cDesc = new int[n];
        for (int i = 0; i < n; i++)
        {
            int p = parent[i];
            cRoot[i] = p < 0 ? i : cRoot[p];
            cDepth[i] = p < 0 ? 0 : cDepth[p] + 1;
        }
        for (int i = n - 1; i >= 0; i--)
            if (parent[i] >= 0) cDesc[parent[i]] += 1 + cDesc[i];
        var derived = new Problems();
        for (int i = 0; i < n; i++)
        {
            if (root[i] != cRoot[i]) derived.Add($"node {i + 1}: tree_root_node {root[i] + 1}, the parent links give {cRoot[i] + 1}");
            if (depth[i] != cDepth[i]) derived.Add($"node {i + 1}: tree_depth {depth[i]}, the parent links give {cDepth[i]}");
            if (desc[i] != cDesc[i]) derived.Add($"node {i + 1}: descendants {desc[i]}, the parent links give {cDesc[i]}");
        }
        var graph = new LayoutGraph
        {
            Count = n, Time = time, Parent = parent, Root = cRoot, Depth = cDepth, Descendants = cDesc, RefCount = refCount,
            Edges = [.. edges], OutDegree = outDeg, MinTime = time.Min(), MaxTime = time.Max(),
        };
        if (derived.Count > 0)
        {
            if (!lenient) derived.ThrowIfAny("tree_root_node / tree_depth / descendants disagree with tree_parent_node (use --lenient to recompute them)");
            graph.Warnings.Add($"{derived.Count} derived tree value(s) disagree with the parent links and were recomputed; first: {derived.First}");
        }
        return graph;
    }

    private static void RequireColumns(SqliteConnection c, Problems problems, string table, params string[] columns)
    {
        var have = Columns(c, table);
        if (have.Count == 0)
        {
            problems.Add($"table {table} is missing");
            return;
        }
        foreach (string col in columns)
            if (!have.Contains(col)) problems.Add($"table {table} has no column {col}");
    }

    public static HashSet<string> Columns(SqliteConnection c, string table)
    {
        // PRAGMA table_info(...) rather than the pragma_table_info() function (SQLite 3.16+);
        // table names here are constants of this program, never user input.
        if (!table.All(ch => char.IsAsciiLetterOrDigit(ch) || ch == '_')) throw new ArgumentException($"bad table name '{table}'");
        var cols = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using var cmd = Cmd(c, $"PRAGMA table_info({table})");
        using var r = cmd.ExecuteReader();
        while (r.Read()) cols.Add(r.GetString(1));
        return cols;
    }

    private static SqliteCommand Cmd(SqliteConnection c, string sql)
    {
        var cmd = c.CreateCommand();
        cmd.CommandText = sql;
        return cmd;
    }

    private static double Real(SqliteDataReader r, int col, Problems? problems, string what)
    {
        object v = r.GetValue(col);
        double x = v switch
        {
            double d => d,
            long l => l,
            _ => double.NaN,
        };
        if (!double.IsFinite(x)) problems?.Add($"{what} is {Describe(v)}, not a finite number");
        return x;
    }

    private static long Integer(SqliteDataReader r, int col, Problems problems, string what)
    {
        object v = r.GetValue(col);
        if (v is long l) return l;
        if (v is double d && d == Math.Floor(d) && Math.Abs(d) < int.MaxValue) return (long)d;
        problems.Add($"{what} is {Describe(v)}, not an integer");
        return 0;
    }

    private static int Index(long id, int n, Problems problems, string what)
    {
        if (id >= 1 && id <= n) return (int)id - 1;
        problems.Add($"{what} {id} is not a node id (1..{n})");
        return -1;
    }

    private static string Describe(object v) => v switch
    {
        DBNull => "NULL",
        string s => $"text '{(s.Length > 20 ? s[..20] + "..." : s)}'",
        _ => Convert.ToString(v, CultureInfo.InvariantCulture) ?? "?",
    };

    private static string Fmt(double x) => x.ToString("0.#####", CultureInfo.InvariantCulture);

    /// <summary>Collects problems and throws them together, so one run shows everything that is wrong.</summary>
    private sealed class Problems
    {
        private readonly List<string> _list = [];
        public int Count { get; private set; }
        public string First => _list.Count > 0 ? _list[0] : "";

        public void Add(string problem)
        {
            Count++;
            if (_list.Count < MaxListed) _list.Add(problem);
        }

        public void ThrowIfAny(string summary)
        {
            if (Count == 0) return;
            string more = Count > _list.Count ? $"\n  ... and {Count - _list.Count} more" : "";
            throw new InvalidGraphException($"{summary} ({Count} problem{(Count == 1 ? "" : "s")}):\n  " +
                                            string.Join("\n  ", _list) + more, _list);
        }
    }
}
