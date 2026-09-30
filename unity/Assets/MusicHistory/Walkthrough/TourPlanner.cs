#nullable enable
using System;
using System.Collections.Generic;
using MusicHistory.Playback;
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
        Chronological,
        /// <summary>
        /// Identity lineages (DESIGN.md §8b): every song of one identity family in time order —
        /// the selected edge's family, else the selected song's strongest, else the largest.
        /// </summary>
        Family
    }

    /// <summary>Where a family tour plays a song: the bars where the family's identity sounds in it.</summary>
    public readonly struct FamilyWindow
    {
        public const string OwnExcerpt = "own excerpt";
        public const string EdgeSpan = "edge span";
        public const string FirstVisit = "first visit";

        public readonly double Start;
        public readonly double End;
        /// <summary><see cref="OwnExcerpt"/>, <see cref="EdgeSpan"/> or <see cref="FirstVisit"/>.</summary>
        public readonly string Source;

        public FamilyWindow(double start, double end, string source)
        {
            Start = start;
            End = end;
            Source = source;
        }
    }

    /// <summary>Tour sequences as node ids (pure functions of the graph; no Unity state).</summary>
    public static class TourPlanner
    {
        /// <summary>Length of a family window that only the identity's first visit locates (the pipeline's minimum and median excerpt).</summary>
        public const double FirstVisitBars = 8;

        /// <summary>
        /// Beats within which two stored positions are the same: Mono.Data.Sqlite reads REAL
        /// columns as single precision (about 3e-5 beats at beat 300), so exact comparison fails.
        /// </summary>
        public const double BeatTolerance = 1e-3;

        public static List<int> Plan(SongGraphData data, TourMode mode, int? fromNode)
        {
            if (data.Songs.Count == 0) return new List<int>();
            if (mode == TourMode.Family)
                return FamilyFor(data, fromNode, null) is int family ? Family(data, family) : new List<int>();
            int start = fromNode is int f && f >= 1 && f <= data.Songs.Count ? f : DefaultStart(data, mode);
            return mode switch
            {
                TourMode.Lineage => Lineage(data, start),
                TourMode.Subtree => Subtree(data, data.Song(start).Descendants > 0 ? start : data.Song(start).TreeRoot),
                _ => Chronological(data, start)
            };
        }

        /// <summary>The M key: lineage → subtree → chronological → family (only with families) → lineage.</summary>
        public static TourMode NextMode(TourMode mode, bool hasFamilies) => mode switch
        {
            TourMode.Lineage => TourMode.Subtree,
            TourMode.Subtree => TourMode.Chronological,
            TourMode.Chronological => hasFamilies ? TourMode.Family : TourMode.Lineage,
            _ => TourMode.Lineage
        };

        /// <summary>
        /// The family a family tour plays: the selected edge's, else the selected song's strongest,
        /// else (nothing selected) the largest family. Null when the graph has no families or the
        /// selected song belongs to none.
        /// </summary>
        public static int? FamilyFor(SongGraphData data, int? fromNode, EdgeRecord? selectedEdge)
        {
            if (!data.HasFamilies) return null;
            if (selectedEdge?.FamilyId is int edgeFamily && data.Families.ContainsKey(edgeFamily)) return edgeFamily;
            if (fromNode is int node && node >= 1 && node <= data.Songs.Count) return StrongestFamily(data, node);
            return LargestFamily(data);
        }

        /// <summary>
        /// A song's strongest family: the highest song_family strength (the share of the song the
        /// identity covers) among families it shares with another song; ties go to the smaller
        /// (more specific) family, then the lower id. A song whose families are all its own falls
        /// back to them. Null when it belongs to none.
        /// </summary>
        public static int? StrongestFamily(SongGraphData data, int nodeId)
        {
            FamilyMember? best = null;
            bool bestShared = false;
            foreach (FamilyMember m in data.FamiliesOf(nodeId))
            {
                IdentityFamily f = data.Families[m.FamilyId];
                bool shared = f.Size >= 2;
                if (best == null || (shared && !bestShared) ||
                    (shared == bestShared && Better(m, f, best, data.Families[best.FamilyId])))
                {
                    best = m;
                    bestShared = shared;
                }
            }
            return best?.FamilyId;

            static bool Better(FamilyMember m, IdentityFamily f, FamilyMember b, IdentityFamily bf) =>
                m.Strength > b.Strength || (m.Strength == b.Strength && (f.Size < bf.Size || (f.Size == bf.Size && f.FamilyId < bf.FamilyId)));
        }

        /// <summary>The family with the most songs (ties: the lower id).</summary>
        public static int? LargestFamily(SongGraphData data)
        {
            IdentityFamily? best = null;
            foreach (IdentityFamily f in data.Families.Values)
                if (best == null || f.Members.Count > best.Members.Count || (f.Members.Count == best.Members.Count && f.FamilyId < best.FamilyId))
                    best = f;
            return best?.FamilyId;
        }

        /// <summary>Every member of <paramref name="familyId"/> in time order (node id = (time_value, work_id) order).</summary>
        public static List<int> Family(SongGraphData data, int familyId)
        {
            List<int> order = new();
            if (data.Family(familyId) is not IdentityFamily f) return order;
            foreach (FamilyMember m in f.Members) order.Add(m.NodeId);
            order.Sort();
            return order;
        }

        /// <summary>
        /// Where the family's identity sounds in the song, bar aligned: the song's own excerpt when
        /// it starts on the bar of the identity's first visit (the song's tree edge carries this
        /// family, so its entry/exit keys apply); else the span the influence stage exported for
        /// an edge of this family at this song; else <see cref="FirstVisitBars"/> bars from the bar
        /// of the first visit. The graph stores no song length, so that last window can run past
        /// the end of a short file (the tail is then silent).
        /// </summary>
        public static FamilyWindow Window(SongGraphData data, int familyId, int nodeId)
        {
            SongRecord s = data.Song(nodeId);
            FamilyWindow own = new(s.ExcerptStartBeat, s.ExcerptEndBeat, FamilyWindow.OwnExcerpt);
            FamilyMember? m = data.Membership(nodeId, familyId);
            if (m?.FirstBeat is not double first || double.IsNaN(first)) return own;
            double bpb = s.BeatsPerBar > 0 ? s.BeatsPerBar : 4;
            double bar = Math.Max(s.FirstDownbeat, s.FirstDownbeat + Math.Floor((first - s.FirstDownbeat + BeatTolerance) / bpb) * bpb);
            if (Math.Abs(bar - s.ExcerptStartBeat) < BeatTolerance) return own;
            foreach (EdgeRecord e in data.EdgesOfFamily(familyId))
            {
                if (e.Target == nodeId && e.DstStartBeat is double ds && e.DstEndBeat is double de && de > ds)
                    return Same(ds, de, own) ? own : new FamilyWindow(ds, de, FamilyWindow.EdgeSpan);
                if (e.Source == nodeId && e.SrcStartBeat is double ss && e.SrcEndBeat is double se && se > ss)
                    return Same(ss, se, own) ? own : new FamilyWindow(ss, se, FamilyWindow.EdgeSpan);
            }
            return new FamilyWindow(bar, bar + FirstVisitBars * bpb, FamilyWindow.FirstVisit);

            static bool Same(double start, double end, FamilyWindow w) => Math.Abs(start - w.Start) < BeatTolerance && Math.Abs(end - w.End) < BeatTolerance;
        }

        /// <summary>
        /// The clip a family tour plays for <paramref name="nodeId"/>: <see cref="Window"/>. Outside
        /// the song's own excerpt the graph has no key region for the window, so the handoff uses
        /// the home key (entry/exit NULL, as DESIGN.md §10 reads it).
        /// </summary>
        public static SongClip FamilyClip(SongGraphData data, int familyId, int nodeId)
        {
            SongClip clip = data.Song(nodeId).ToClip();
            FamilyWindow w = Window(data, familyId, nodeId);
            if (w.Source == FamilyWindow.OwnExcerpt) return clip;
            clip.ExcerptStartBeat = w.Start;
            clip.ExcerptEndBeat = w.End;
            clip.EntryTonicPc = null;
            clip.EntryMinor = null;
            clip.ExitTonicPc = null;
            clip.ExitMinor = null;
            return clip;
        }

        /// <summary>
        /// The edge a family step lights up: this family's edge from the song heard before, else
        /// the song's tree edge when it carries the family, else the family's strongest-scoring
        /// edge into the song. Null when the song joins the family without an edge of it.
        /// </summary>
        public static EdgeRecord? FamilyStepEdge(SongGraphData data, int familyId, int nodeId, int? previousNode)
        {
            EdgeRecord? best = null;
            foreach (EdgeRecord e in data.EdgesOfFamily(familyId))
            {
                if (e.Target != nodeId) continue;
                if (previousNode is int p && e.Source == p) return e;
                if (best == null || (e.IsTree && !best.IsTree) ||
                    (e.IsTree == best.IsTree && (e.ScoreBits > best.ScoreBits || (e.ScoreBits == best.ScoreBits && e.Id < best.Id))))
                    best = e;
            }
            return best;
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
