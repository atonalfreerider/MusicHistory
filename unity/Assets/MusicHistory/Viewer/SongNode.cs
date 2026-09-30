#nullable enable
using System;
using System.Collections.Generic;
using UnityEngine;

namespace MusicHistory.Viewer
{
    /// <summary>
    /// One song (fork of Unity-FDG's NodeSphere). The root object sits at the song's position at
    /// scale 1 and carries a sphere collider for picking; a child quad is the bubble, drawn by the
    /// camera-facing SongBubble shader with a shared material (fill = key, ring = decade). Bubble
    /// area is proportional to descendants + 1. The label is a separate world label, so its size
    /// never depends on the bubble's size.
    /// </summary>
    public sealed class SongNode : MonoBehaviour
    {
        static Mesh? quadMesh;

        [NonSerialized] public SongRecord Song = null!;
        public float Radius;
        public Color KeyColor;
        public Color DecadeColor;
        public SongNode? TreeParent;
        public InfluenceEdge? TreeEdge;
        public readonly List<SongNode> TreeChildren = new();
        public readonly List<InfluenceEdge> Incoming = new();
        public readonly List<InfluenceEdge> Outgoing = new();
        /// <summary>Always labelled (one of the most influential songs).</summary>
        public bool LabelPinned;
        [NonSerialized] public WorldLabel? Label;

        MeshRenderer bubbleRenderer = null!;
        BubbleState state = BubbleState.Normal;
        bool labelRequested;

        public BubbleState State => state;
        public MeshRenderer BubbleRenderer => bubbleRenderer;
        public int NodeId => Song.NodeId;
        public string LabelText => $"<b><noparse>{Song.Title}</noparse></b>\n<size=78%><color=#c3c8d0><noparse>{Song.Artist}</noparse> · {Song.Year}</color></size>" +
                                   (Song.IsValidationExtra ? "\n<size=64%><color=#ffcf4a><i>validation control, outside the ranked list</i></color></size>" : "");

        public static SongNode Create(SongRecord song, Vector3 position, float radius, Transform parent)
        {
            GameObject go = new($"{song.NodeId:0000} {song.Title}");
            go.transform.SetParent(parent, false);
            go.transform.position = position;
            SongNode node = go.AddComponent<SongNode>();
            node.Song = song;
            node.Radius = radius;
            node.KeyColor = SongPalette.KeyColor(song.TonicPc, song.Minor);
            node.DecadeColor = SongPalette.DecadeColor(song.Year);

            SphereCollider picker = go.AddComponent<SphereCollider>();
            picker.radius = radius;

            GameObject bubble = new("Bubble");
            bubble.transform.SetParent(go.transform, false);
            bubble.transform.localScale = Vector3.one * (radius * 2f);
            bubble.AddComponent<MeshFilter>().sharedMesh = QuadMesh();
            node.bubbleRenderer = bubble.AddComponent<MeshRenderer>();
            node.bubbleRenderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            node.bubbleRenderer.receiveShadows = false;
            node.bubbleRenderer.lightProbeUsage = UnityEngine.Rendering.LightProbeUsage.Off;
            node.bubbleRenderer.reflectionProbeUsage = UnityEngine.Rendering.ReflectionProbeUsage.Off;
            node.bubbleRenderer.sharedMaterial = GraphMaterials.Bubble(node.KeyColor, node.DecadeColor, BubbleState.Normal);
            return node;
        }

        /// <summary>Unit quad in the XY plane; bounds are a cube so camera-facing discs are never culled early.</summary>
        static Mesh QuadMesh()
        {
            if (quadMesh != null) return quadMesh;
            quadMesh = new Mesh
            {
                name = "Song Bubble Quad",
                hideFlags = HideFlags.DontSave,
                vertices = new[]
                {
                    new Vector3(-.5f, -.5f, 0), new Vector3(.5f, -.5f, 0),
                    new Vector3(.5f, .5f, 0), new Vector3(-.5f, .5f, 0)
                },
                uv = new[] { new Vector2(0, 0), new Vector2(1, 0), new Vector2(1, 1), new Vector2(0, 1) },
                triangles = new[] { 0, 2, 1, 0, 3, 2 }
            };
            quadMesh.bounds = new Bounds(Vector3.zero, Vector3.one);
            return quadMesh;
        }

        public void SetState(BubbleState newState)
        {
            if (newState == state) return;
            state = newState;
            bubbleRenderer.sharedMaterial = GraphMaterials.Bubble(KeyColor, DecadeColor, newState);
        }

        /// <summary>Called when the live simulation moved this node.</summary>
        public void OnMoved()
        {
            foreach (InfluenceEdge e in Incoming) e.UpdateGeometry();
            foreach (InfluenceEdge e in Outgoing) e.UpdateGeometry();
        }

        /// <summary>Requests a label beyond the pinned set (hover, walkthrough); created on first use.</summary>
        public void RequestLabel(LabelLayer layer, bool requested)
        {
            labelRequested = requested;
            RefreshLabel(layer);
        }

        public void RefreshLabel(LabelLayer layer)
        {
            bool want = LabelPinned || labelRequested;
            if (want && Label == null)
                Label = layer.Create(LabelText, transform, transform.position, Radius, LabelPlacement.Right);
            if (Label != null)
            {
                layer.SetVisible(Label, want);
                layer.SetDimmed(Label, state == BubbleState.Dimmed);
                // Focus first, then related or requested songs, then the most influential.
                float influence = Song.Descendants + Song.RefCount * .01f;
                Label.Priority = state switch
                {
                    BubbleState.Focus => 1e8f,
                    BubbleState.Related => 1e7f + influence,
                    _ when labelRequested => 1e7f + influence,
                    BubbleState.Dimmed => influence,
                    _ => 1e6f + influence
                };
            }
        }

        public IEnumerable<SongNode> Influencers()
        {
            foreach (InfluenceEdge e in Incoming) yield return e.Source;
        }

        public IEnumerable<SongNode> Influenced()
        {
            foreach (InfluenceEdge e in Outgoing) yield return e.Target;
        }

        public static void ReleaseSharedMesh()
        {
            if (quadMesh == null) return;
            if (Application.isPlaying) Destroy(quadMesh);
            else DestroyImmediate(quadMesh);
            quadMesh = null;
        }
    }
}
