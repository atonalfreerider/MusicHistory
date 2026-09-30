#nullable enable
using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using UnityEngine.InputSystem;

namespace MusicHistory.Viewer
{
    /// <summary>
    /// Hover and click selection (fork of Unity-FDG's MouseBubbleHighlighter). The focus song
    /// glows, its influencers and influenced songs glow faintly, everything else dims; the focus'
    /// secondary edges appear (they are hidden otherwise) and its edges light up. A click selects
    /// a song (sticky focus, the walkthrough's target); a click on no bubble but within
    /// <see cref="EdgePickPixels"/> of a drawn edge selects the edge (its card, and the family a
    /// family tour plays); clicking empty space clears both.
    /// The walkthrough takes over with <see cref="ShowTourStep"/> and gives it back with
    /// <see cref="EndTour"/>.
    /// </summary>
    public sealed class HoverHighlighter : MonoBehaviour
    {
        public SongGraphLoader Loader = null!;
        [Tooltip("Extra labels shown for the most influential songs a focus influenced.")]
        public int InfluencedLabelLimit = 12;

        [Tooltip("A click this close (pixels) to a drawn edge, and on no bubble, selects the edge.")]
        public float EdgePickPixels = 7f;

        public SongNode? Hovered { get; private set; }
        public SongNode? Selected { get; private set; }
        public SongNode? Focus { get; private set; }
        /// <summary>A clicked edge (its card replaces the song info; family tours play its identity).</summary>
        public InfluenceEdge? SelectedEdge { get; private set; }
        /// <summary>The edge whose card is showing (null while a song is in focus).</summary>
        public InfluenceEdge? FocusEdge { get; private set; }
        /// <summary>True while the walkthrough owns highlighting.</summary>
        public bool Suspended { get; private set; }
        public event Action<SongNode?>? FocusChanged;
        public event Action<InfluenceEdge>? EdgeFocusChanged;

        readonly HashSet<SongNode> labelled = new();
        Vector2 pressPosition;
        bool pressed;
        bool applied;

        void Update()
        {
            if (Suspended || Loader == null || Loader.Nodes.Count == 0) return;
            Mouse? mouse = Mouse.current;
            if (mouse == null) return;
            Camera? cam = Camera.main;
            if (cam == null) return;

            if (CameraControl.UserIsNavigating())
            {
                Hovered = null;
                pressed = false;
                ApplyResting();
                return;
            }

            Vector2 pointer = mouse.position.ReadValue();
            Hovered = Pick(cam, pointer);

            if (mouse.leftButton.wasPressedThisFrame)
            {
                pressed = true;
                pressPosition = pointer;
            }
            if (pressed && mouse.leftButton.wasReleasedThisFrame)
            {
                pressed = false;
                if ((pointer - pressPosition).sqrMagnitude < 36f)
                {
                    InfluenceEdge? edge = Hovered == null ? PickEdge(cam, pointer, EdgePickPixels, out _) : null;
                    if (edge != null) SelectEdge(edge);
                    else Select(Hovered);
                }
            }

            if (Hovered != null) ApplyFocus(Hovered);
            else ApplyResting();
        }

        /// <summary>Without a hover: the selected edge's card, else the selected song (or nothing).</summary>
        void ApplyResting()
        {
            if (SelectedEdge != null) ApplyEdgeFocus(SelectedEdge);
            else ApplyFocus(Selected);
        }

        /// <summary>
        /// The drawn edge nearest <paramref name="screenPoint"/> on screen, within
        /// <paramref name="maxPixels"/> of its centre line (null when none is that close).
        /// </summary>
        public InfluenceEdge? PickEdge(Camera cam, Vector2 screenPoint, float maxPixels, out float pixels)
        {
            InfluenceEdge? best = null;
            pixels = float.MaxValue;
            foreach (InfluenceEdge e in Loader.Edges)
            {
                if (!e.Shown || !e.Renderer.enabled) continue;
                Vector3 a = cam.WorldToScreenPoint(e.Source.transform.position);
                Vector3 b = cam.WorldToScreenPoint(e.Target.transform.position);
                if (a.z <= cam.nearClipPlane || b.z <= cam.nearClipPlane) continue;
                float d = DistanceToSegment(screenPoint, a, b);
                if (d < pixels)
                {
                    pixels = d;
                    best = e;
                }
            }
            return pixels <= maxPixels ? best : null;
        }

        static float DistanceToSegment(Vector2 p, Vector2 a, Vector2 b)
        {
            Vector2 ab = b - a;
            float t = ab.sqrMagnitude > 1e-6f ? Mathf.Clamp01(Vector2.Dot(p - a, ab) / ab.sqrMagnitude) : 0f;
            return Vector2.Distance(p, a + ab * t);
        }

        public static SongNode? Pick(Camera cam, Vector2 screenPoint)
        {
            Ray ray = cam.ScreenPointToRay(screenPoint);
            return Physics.Raycast(ray, out RaycastHit hit, Mathf.Infinity, Physics.DefaultRaycastLayers,
                QueryTriggerInteraction.Ignore)
                ? hit.collider.GetComponentInParent<SongNode>()
                : null;
        }

        public void Select(SongNode? node)
        {
            Selected = node;
            SelectedEdge = null;
            if (!Suspended)
            {
                if (Hovered != null) ApplyFocus(Hovered);
                else ApplyResting();
            }
        }

        /// <summary>Selects an edge (clears the song selection); its card shows until something else is selected.</summary>
        public void SelectEdge(InfluenceEdge? edge)
        {
            if (edge == null)
            {
                Select(null);
                return;
            }
            Selected = null;
            SelectedEdge = edge;
            if (!Suspended)
            {
                if (Hovered != null) ApplyFocus(Hovered);
                else ApplyEdgeFocus(edge);
            }
        }

        /// <summary>
        /// An edge card: the edge lights up and its two songs glow; for identity lineages the other
        /// songs and edges of the edge's family stay undimmed, so the whole shared identity shows.
        /// </summary>
        public void ApplyEdgeFocus(InfluenceEdge edge, bool force = false)
        {
            if (!force && applied && FocusEdge == edge) return;
            applied = true;
            Focus = null;
            FocusEdge = edge;
            SongGraphData? data = Loader.Data;
            int? family = edge.Record.FamilyId;
            HashSet<int> members = new();
            if (data != null && data.Family(family) is IdentityFamily f)
                foreach (FamilyMember m in f.Members) members.Add(m.NodeId);
            foreach (SongNode n in Loader.Nodes)
                n.SetState(n == edge.Target ? BubbleState.Focus : n == edge.Source ? BubbleState.Related
                    : members.Contains(n.NodeId) ? BubbleState.Normal : BubbleState.Dimmed);
            foreach (InfluenceEdge e in Loader.Edges)
            {
                bool same = family != null && e.Record.FamilyId == family;
                e.SetState(e == edge ? EdgeState.Highlight : same ? EdgeState.Normal : EdgeState.Dimmed);
                e.SetShown(e.IsTree || e == edge || same || Loader.ShowAllSecondaryEdges);
            }
            SetExtraLabels(new[] { edge.Source, edge.Target });
            EdgeFocusChanged?.Invoke(edge);
        }

        /// <summary>Highlights <paramref name="focus"/> with its influencers and influenced songs (null = none).</summary>
        public void ApplyFocus(SongNode? focus, bool force = false)
        {
            if (!force && applied && focus == Focus && FocusEdge == null) return;
            applied = true;
            Focus = focus;
            FocusEdge = null;
            IReadOnlyList<SongNode> nodes = Loader.Nodes;
            IReadOnlyList<InfluenceEdge> edges = Loader.Edges;

            if (focus == null)
            {
                foreach (SongNode n in nodes) n.SetState(BubbleState.Normal);
                foreach (InfluenceEdge e in edges)
                {
                    e.SetState(EdgeState.Normal);
                    e.SetShown(e.IsTree || Loader.ShowAllSecondaryEdges);
                }
                SetExtraLabels(Array.Empty<SongNode>());
                FocusChanged?.Invoke(null);
                return;
            }

            HashSet<SongNode> related = new();
            foreach (SongNode n in focus.Influencers()) related.Add(n);
            foreach (SongNode n in focus.Influenced()) related.Add(n);
            foreach (SongNode n in nodes)
                n.SetState(n == focus ? BubbleState.Focus : related.Contains(n) ? BubbleState.Related : BubbleState.Dimmed);
            foreach (InfluenceEdge e in edges)
            {
                bool incident = e.Source == focus || e.Target == focus;
                e.SetState(incident ? EdgeState.Highlight : EdgeState.Dimmed);
                e.SetShown(e.IsTree || incident || Loader.ShowAllSecondaryEdges);
            }

            List<SongNode> extra = new() { focus };
            extra.AddRange(focus.Influencers());
            extra.AddRange(focus.Influenced()
                .OrderByDescending(n => n.Song.Descendants)
                .ThenBy(n => n.NodeId)
                .Take(InfluencedLabelLimit));
            SetExtraLabels(extra);
            FocusChanged?.Invoke(focus);
        }

        /// <summary>Walkthrough step: the child glows, its tree parent glows faintly, the tree edge lights up.</summary>
        public void ShowTourStep(SongNode child, SongNode? parent) => ShowTourStep(child, parent, child.TreeEdge, null);

        /// <summary>
        /// Walkthrough step: the child glows, <paramref name="parent"/> (the tree parent, or the
        /// previous song of a family tour) glows faintly and <paramref name="stepEdge"/> lights up.
        /// With <paramref name="familyId"/> (a family tour) the family's other songs and edges stay
        /// undimmed instead of fading.
        /// </summary>
        public void ShowTourStep(SongNode child, SongNode? parent, InfluenceEdge? stepEdge, int? familyId)
        {
            Suspended = true;
            Loader.Labels.HideDimmed = true;
            Hovered = null;
            Focus = child;
            FocusEdge = null;
            applied = true;
            HashSet<int> members = new();
            if (Loader.Data?.Family(familyId) is IdentityFamily f)
                foreach (FamilyMember m in f.Members) members.Add(m.NodeId);
            foreach (SongNode n in Loader.Nodes)
                n.SetState(n == child ? BubbleState.Focus : n == parent ? BubbleState.Related
                    : members.Contains(n.NodeId) ? BubbleState.Normal : BubbleState.Dimmed);
            foreach (InfluenceEdge e in Loader.Edges)
            {
                bool step = e == stepEdge;
                bool intoChild = e.Target == child;
                bool same = familyId != null && e.Record.FamilyId == familyId;
                e.SetState(step ? EdgeState.Highlight : same ? EdgeState.Normal : EdgeState.Dimmed);
                e.SetShown(e.IsTree || intoChild || step || same || Loader.ShowAllSecondaryEdges);
            }
            List<SongNode> extra = new() { child };
            if (parent != null) extra.Add(parent);
            SetExtraLabels(extra);
            FocusChanged?.Invoke(child);
        }

        public void EndTour()
        {
            Suspended = false;
            Loader.Labels.HideDimmed = false;
            Reapply();
        }

        /// <summary>Re-applies the hover, else the selected edge's card, else the selected song (after V or a tour).</summary>
        public void Reapply()
        {
            if (Hovered != null) ApplyFocus(Hovered, force: true);
            else if (SelectedEdge != null) ApplyEdgeFocus(SelectedEdge, force: true);
            else ApplyFocus(Selected, force: true);
        }

        void SetExtraLabels(IEnumerable<SongNode> wanted)
        {
            HashSet<SongNode> next = new(wanted);
            foreach (SongNode n in labelled)
                if (!next.Contains(n)) n.RequestLabel(Loader.Labels, false);
            foreach (SongNode n in next) n.RequestLabel(Loader.Labels, true);
            labelled.Clear();
            labelled.UnionWith(next);
            foreach (SongNode n in next) n.RefreshLabel(Loader.Labels);
            // Pinned labels grey out with their dimmed bubbles.
            foreach (SongNode n in Loader.PinnedLabelNodes) n.RefreshLabel(Loader.Labels);
        }

        void OnDisable()
        {
            if (Loader != null && Loader.Nodes.Count > 0 && !Suspended) ApplyFocus(null);
        }
    }
}
