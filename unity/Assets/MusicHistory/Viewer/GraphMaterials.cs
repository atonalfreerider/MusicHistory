#nullable enable
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Rendering;

namespace MusicHistory.Viewer
{
    public enum BubbleState
    {
        Normal,
        Dimmed,
        /// <summary>An influencer or influenced song of the focus.</summary>
        Related,
        /// <summary>The hovered, selected or playing song.</summary>
        Focus
    }

    public enum EdgeState
    {
        Normal,
        Dimmed,
        Highlight
    }

    /// <summary>How much of the tree hangs below a tree edge: trunks lead into big subtrees.</summary>
    public enum EdgeTier
    {
        Twig,
        Branch,
        Trunk,
        /// <summary>Identity lineages: a strong match (exact shared passage), drawn brightest.</summary>
        Strong
    }

    /// <summary>
    /// Shared materials for every bubble and edge. A renderer never owns a material: changing
    /// state swaps sharedMaterial between cached variants, so 1000 bubbles use at most
    /// (key colours x decade colours x states) materials and ~3000 edges use 30. Both shaders
    /// keep their properties in the UnityPerMaterial CBUFFER, so the SRP Batcher batches them.
    /// </summary>
    public static class GraphMaterials
    {
        public const string BubbleShaderName = "MusicHistory/SongBubble";
        public const string EdgeShaderName = "MusicHistory/GlowingEdge";

        static readonly Dictionary<(Color32 fill, Color32 ring, BubbleState state), Material> bubbles = new();
        static readonly Dictionary<(EdgeChannel channel, EdgeState state, bool tree, EdgeTier tier), Material> edges = new();
        static readonly Dictionary<(Color32 color, int glowMilli, bool additive), Material> lines = new();
        static readonly HashSet<Material> all = new();
        static Shader? bubbleShader;
        static Shader? edgeShader;

        public static int BubbleMaterialCount => bubbles.Count;
        public static int EdgeMaterialCount => edges.Count;
        public static bool IsShared(Material? material) => material != null && all.Contains(material);

        static Shader BubbleShader()
        {
            if (bubbleShader == null) bubbleShader = Shader.Find(BubbleShaderName);
            if (bubbleShader == null) bubbleShader = Shader.Find("Universal Render Pipeline/Unlit");
            return bubbleShader;
        }

        static Shader EdgeShader()
        {
            if (edgeShader == null) edgeShader = Shader.Find(EdgeShaderName);
            if (edgeShader == null) edgeShader = Shader.Find("Universal Render Pipeline/Unlit");
            return edgeShader;
        }

        public static Material Bubble(Color fill, Color ring, BubbleState state)
        {
            var key = ((Color32)fill, (Color32)ring, state);
            if (bubbles.TryGetValue(key, out Material existing) && existing != null) return existing;
            Material m = new(BubbleShader()) { name = $"Bubble {ColorUtility.ToHtmlStringRGB(fill)}/{ColorUtility.ToHtmlStringRGB(ring)} {state}" };
            m.hideFlags = HideFlags.DontSave;
            (float ringWidth, float glow, float dim) = state switch
            {
                BubbleState.Dimmed => (.14f, 0f, .3f),
                BubbleState.Related => (.22f, 1.6f, 1f),
                BubbleState.Focus => (.26f, 3.2f, 1.15f),
                _ => (.14f, 0f, 1f)
            };
            m.SetColor("_FillColor", fill);
            m.SetColor("_RingColor", ring);
            m.SetColor("_BaseColor", fill);
            m.SetFloat("_RingWidth", ringWidth);
            m.SetColor("_GlowColor", ring);
            m.SetFloat("_GlowIntensity", glow);
            m.SetFloat("_DimFactor", dim);
            bubbles[key] = m;
            all.Add(m);
            return m;
        }

        public static Material Edge(EdgeChannel channel, EdgeState state, bool tree, EdgeTier tier = EdgeTier.Branch)
        {
            // Only resting tree edges differ by tier.
            if (state != EdgeState.Normal || !tree) tier = EdgeTier.Branch;
            var key = (channel, state, tree, tier);
            if (edges.TryGetValue(key, out Material existing) && existing != null) return existing;
            Color c = SongPalette.ChannelColor(channel);
            Material m = new(EdgeShader()) { name = $"Edge {channel} {state} {(tree ? "tree " + tier : "secondary")}" };
            m.hideFlags = HideFlags.DontSave;
            // Resting and dimmed edges are additive light (bundles brighten instead of tangling);
            // highlighted edges are opaque and glow into the bloom.
            (float baseScale, float glow, bool additive) = (state, tree) switch
            {
                (EdgeState.Dimmed, _) => (.06f, 0f, true),
                (EdgeState.Highlight, true) => (.6f, .55f, false),
                (EdgeState.Highlight, false) => (.5f, .35f, false),
                (_, true) => (tier switch { EdgeTier.Twig => .17f, EdgeTier.Trunk => .5f, EdgeTier.Strong => .95f, _ => .3f }, 0f, true),
                _ => (.16f, 0f, true)
            };
            m.SetColor("_Color", new Color(c.r * baseScale, c.g * baseScale, c.b * baseScale, 1));
            m.SetColor("_BaseColor", new Color(c.r * baseScale, c.g * baseScale, c.b * baseScale, 1));
            m.SetColor("_GlowColor", c);
            m.SetFloat("_GlowIntensity", glow);
            SetBlend(m, additive);
            edges[key] = m;
            all.Add(m);
            return m;
        }

        static void SetBlend(Material m, bool additive)
        {
            m.SetFloat("_SrcBlend", (float)BlendMode.One);
            m.SetFloat("_DstBlend", (float)(additive ? BlendMode.One : BlendMode.Zero));
            m.SetFloat("_ZWrite", additive ? 0f : 1f);
            m.renderQueue = (int)(additive ? RenderQueue.Transparent : RenderQueue.Geometry);
        }

        /// <summary>Flat line material (timeline, decade rings); additive lines are faint light.</summary>
        public static Material Line(Color color, float glow = 0, bool additive = false)
        {
            var key = ((Color32)color, Mathf.RoundToInt(glow * 1000), additive);
            if (lines.TryGetValue(key, out Material existing) && existing != null) return existing;
            Material m = new(EdgeShader()) { name = $"Line {ColorUtility.ToHtmlStringRGBA(color)}" };
            m.hideFlags = HideFlags.DontSave;
            m.SetColor("_Color", color);
            m.SetColor("_BaseColor", color);
            m.SetColor("_GlowColor", color);
            m.SetFloat("_GlowIntensity", glow);
            SetBlend(m, additive);
            lines[key] = m;
            all.Add(m);
            return m;
        }

        /// <summary>Destroys every cached material (graph reload, validation).</summary>
        public static void Clear()
        {
            foreach (Material m in all)
            {
                if (m == null) continue;
                if (Application.isPlaying) Object.Destroy(m);
                else Object.DestroyImmediate(m);
            }
            all.Clear();
            bubbles.Clear();
            edges.Clear();
            lines.Clear();
        }
    }
}
