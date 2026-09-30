#nullable enable
using System;
using MusicHistory.Viewer;
using UnityEngine;

namespace MusicHistory.Themes
{
    /// <summary>
    /// One song in the themes cloud: a camera-facing sphere impostor (the SongBubble shader) with
    /// a shared material chosen by singer colour, lyrics/title source and state. No collider:
    /// the viewer picks bubbles in screen space, so even the 4-pixel minimum dots are pickable.
    /// </summary>
    public sealed class ThemeSongNode : MonoBehaviour
    {
        static Mesh? quadMesh;

        [NonSerialized] public ThemeSongRecord Song = null!;
        public float Radius;
        public SingerColorGroup ColorGroup;
        /// <summary>Always labelled (the clearest example of its theme).</summary>
        public bool LabelPinned;
        [NonSerialized] public WorldLabel? Label;

        MeshRenderer bubbleRenderer = null!;
        ThemeBubbleState state = ThemeBubbleState.Normal;
        bool labelRequested;

        public ThemeBubbleState State => state;
        public MeshRenderer BubbleRenderer => bubbleRenderer;
        public int NodeId => Song.NodeId;

        /// <summary>World label: title, then artist and year. Strings from the DB are escaped.</summary>
        public string LabelText => $"<b>{GraphHud.Esc(Song.Title)}</b>\n<size=78%><color=#c3c8d0>{GraphHud.Esc(Song.Artist)} · {Song.Year}</color></size>";

        public static ThemeSongNode Create(ThemeSongRecord song, Vector3 position, float radius, Transform parent)
        {
            GameObject go = new($"{song.NodeId:0000} {song.Title}");
            go.transform.SetParent(parent, false);
            go.transform.position = position;
            ThemeSongNode node = go.AddComponent<ThemeSongNode>();
            node.Song = song;
            node.Radius = radius;
            node.ColorGroup = ThemesPalette.Group(song.Gender);

            GameObject bubble = new("Bubble");
            bubble.transform.SetParent(go.transform, false);
            bubble.transform.localScale = Vector3.one * (radius * 2f);
            bubble.AddComponent<MeshFilter>().sharedMesh = QuadMesh();
            node.bubbleRenderer = bubble.AddComponent<MeshRenderer>();
            node.bubbleRenderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            node.bubbleRenderer.receiveShadows = false;
            node.bubbleRenderer.lightProbeUsage = UnityEngine.Rendering.LightProbeUsage.Off;
            node.bubbleRenderer.reflectionProbeUsage = UnityEngine.Rendering.ReflectionProbeUsage.Off;
            node.bubbleRenderer.sharedMaterial = ThemesMaterials.Bubble(node.ColorGroup, song.FromLyrics, ThemeBubbleState.Normal);
            return node;
        }

        /// <summary>Unit quad in the XY plane (the shader billboards it); cube bounds so it is never culled early.</summary>
        static Mesh QuadMesh()
        {
            if (quadMesh != null) return quadMesh;
            quadMesh = new Mesh
            {
                name = "Theme Bubble Quad",
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

        public void SetState(ThemeBubbleState newState)
        {
            if (newState == state) return;
            state = newState;
            bubbleRenderer.sharedMaterial = ThemesMaterials.Bubble(ColorGroup, Song.FromLyrics, newState);
        }

        /// <summary>Requests a label beyond the pinned set (hover, playing); created on first use.</summary>
        public void RequestLabel(LabelLayer layer, bool requested, bool allLabels)
        {
            labelRequested = requested;
            RefreshLabel(layer, allLabels);
        }

        public void RefreshLabel(LabelLayer layer, bool allLabels)
        {
            bool want = LabelPinned || labelRequested || allLabels;
            if (want && Label == null)
                Label = layer.Create(LabelText, transform, transform.position, Radius, LabelPlacement.Right);
            if (Label == null) return;
            layer.SetVisible(Label, want);
            layer.SetDimmed(Label, state == ThemeBubbleState.Dimmed);
            float clarity = (float)Song.TopScore;
            Label.Priority = state switch
            {
                ThemeBubbleState.Hover => 1e8f,
                ThemeBubbleState.Playing => 1e8f - 1f,
                ThemeBubbleState.Dimmed => clarity,
                _ when labelRequested => 1e7f + clarity,
                _ when LabelPinned => 1e6f + clarity,
                _ => 1e3f + clarity
            };
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
