#nullable enable
using System.Collections.Generic;
using MusicHistory.Viewer;

namespace MusicHistory.Walkthrough
{
    public enum TourMode
    {
        /// <summary>Root → … → the selected song, along tree parents.</summary>
        Lineage,
        /// <summary>Depth-first from the selected song (or its root when it has no children), children by date.</summary>
        Subtree,
        /// <summary>Every song by date, from the selected song onward.</summary>
        Chronological
    }

    /// <summary>Tour sequences as node ids (pure functions of the graph; no Unity state).</summary>
    public static class TourPlanner
    {
        public static List<int> Plan(SongGraphData data, TourMode mode, int? fromNode)
        {
            if (data.Songs.Count == 0) return new List<int>();
            int start = fromNode is int f && f >= 1 && f <= data.Songs.Count ? f : DefaultStart(data, mode);
            return mode switch
            {
                TourMode.Lineage => Lineage(data, start),
                TourMode.Subtree => Subtree(data, data.Song(start).Descendants > 0 ? start : data.Song(start).TreeRoot),
                _ => Chronological(data, start)
            };
        }

        /// <summary>Without a selection: the deepest song for lineage, the largest tree otherwise, the first song for time order.</summary>
        public static int DefaultStart(SongGraphData data, TourMode mode)
        {
            int best = 1;
            switch (mode)
            {
                case TourMode.Lineage:
                    foreach (SongRecord s in data.Songs)
                    {
                        SongRecord b = data.Song(best);
                        if (s.TreeDepth > b.TreeDepth || (s.TreeDepth == b.TreeDepth && s.Descendants > b.Descendants)) best = s.NodeId;
                    }
                    return best;
                case TourMode.Subtree:
                    foreach (SongRecord s in data.Songs)
                        if (s.IsRoot && s.Descendants > data.Song(best).Descendants) best = s.NodeId;
                    return data.Song(best).IsRoot ? best : data.Song(best).TreeRoot;
                default:
                    return 1;
            }
        }

        public static List<int> Lineage(SongGraphData data, int target)
        {
            List<int> chain = new();
            HashSet<int> seen = new();
            int? id = target;
            while (id is int n && n >= 1 && n <= data.Songs.Count && seen.Add(n))
            {
                chain.Add(n);
                id = data.Song(n).TreeParent;
            }
            chain.Reverse();
            return chain;
        }

        public static List<int> Subtree(SongGraphData data, int start)
        {
            Dictionary<int, List<int>> children = ChildrenByDate(data);
            List<int> order = new();
            Stack<int> stack = new();
            stack.Push(start);
            HashSet<int> seen = new();
            while (stack.Count > 0)
            {
                int id = stack.Pop();
                if (!seen.Add(id)) continue;
                order.Add(id);
                if (!children.TryGetValue(id, out List<int>? kids)) continue;
                for (int i = kids.Count - 1; i >= 0; i--) stack.Push(kids[i]);
            }
            return order;
        }

        public static List<int> Chronological(SongGraphData data, int start)
        {
            // Node ids are ordered by (time_value, work_id), so id order is date order.
            List<int> order = new();
            for (int id = start; id <= data.Songs.Count; id++) order.Add(id);
            return order;
        }

        static Dictionary<int, List<int>> ChildrenByDate(SongGraphData data)
        {
            Dictionary<int, List<int>> children = new();
            foreach (SongRecord s in data.Songs)
            {
                if (s.TreeParent is not int parent) continue;
                if (!children.TryGetValue(parent, out List<int>? list)) children[parent] = list = new List<int>();
                list.Add(s.NodeId); // ascending id = ascending date
            }
            return children;
        }
    }
}
