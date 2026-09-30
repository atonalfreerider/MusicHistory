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
    /// a song (sticky focus, the walkthrough's target); clicking empty space clears it.
    /// The walkthrough takes over with <see cref="ShowTourStep"/> and gives it back with
    /// <see cref="EndTour"/>.
    /// </summary>
    public sealed class HoverHighlighter : MonoBehaviour
    {
        public SongGraphLoader Loader = null!;
        [Tooltip("Extra labels shown for the most influential songs a focus influenced.")]
        public int InfluencedLabelLimit = 12;

        public SongNode? Hovered { get; private set; }
        public SongNode? Selected { get; private set; }
        public SongNode? Focus { get; private set; }
        /// <summary>True while the walkthrough owns highlighting.</summary>
        public bool Suspended { get; private set; }
        public event Action<SongNode?>? FocusChanged;

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
                ApplyFocus(Selected);
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
                if ((pointer - pressPosition).sqrMagnitude < 36f) Select(Hovered);
            }

            ApplyFocus(Hovered != null ? Hovered : Selected);
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
            if (!Suspended) ApplyFocus(Hovered != null ? Hovered : Selected);
        }

        /// <summary>Highlights <paramref name="focus"/> with its influencers and influenced songs (null = none).</summary>
        public void ApplyFocus(SongNode? focus, bool force = false)
        {
            if (!force && applied && focus == Focus) return;
            applied = true;
            Focus = focus;
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
        public void ShowTourStep(SongNode child, SongNode? parent)
        {
            Suspended = true;
            Loader.Labels.HideDimmed = true;
            Hovered = null;
            Focus = child;
            applied = true;
            foreach (SongNode n in Loader.Nodes)
                n.SetState(n == child ? BubbleState.Focus : n == parent ? BubbleState.Related : BubbleState.Dimmed);
            foreach (InfluenceEdge e in Loader.Edges)
            {
                bool treeStep = e == child.TreeEdge;
                bool intoChild = e.Target == child;
                e.SetState(treeStep ? EdgeState.Highlight : EdgeState.Dimmed);
                e.SetShown(e.IsTree || intoChild || Loader.ShowAllSecondaryEdges);
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
            ApplyFocus(Hovered != null ? Hovered : Selected, force: true);
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
