#nullable enable
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Rendering;

namespace MusicHistory.Themes
{
    /// <summary>How a song bubble is drawn right now.</summary>
    public enum ThemeBubbleState
    {
        Normal,
        /// <summary>Outside the active theme filter.</summary>
        Dimmed,
        /// <summary>Under the pointer.</summary>
        Hover,
        /// <summary>Its excerpt is playing.</summary>
        Playing
    }

    /// <summary>
    /// Shared materials of the themes viewer. A renderer never owns a material: a bubble's state
    /// change swaps sharedMaterial between cached variants keyed by (singer colour, lyrics/title,
    /// state), so ~1000 bubbles use at most 24 materials, all on the SongBubble shader (SRP Batcher
    /// compatible). Kept apart from the influence viewer's GraphMaterials cache so neither scene
    /// can destroy the other's materials.
    /// </summary>
    public static class ThemesMaterials
    {
        public const string BubbleShaderName = "MusicHistory/SongBubble";
        public const string GroundShaderName = "MusicHistory/ThemesGround";
        public const string TetherShaderName = "MusicHistory/ThemesTether";

        static readonly Dictionary<(SingerColorGroup group, bool lyrics, ThemeBubbleState state), Material> bubbles = new();
        static readonly List<Material> owned = new();
        static readonly HashSet<Material> shared = new();

        public static int BubbleMaterialCount => bubbles.Count;
        public static bool IsSharedBubble(Material? m) => m != null && shared.Contains(m);

        static Shader Find(string name)
        {
            Shader s = Shader.Find(name);
            if (s == null) s = Shader.Find("Universal Render Pipeline/Unlit");
            return s;
        }

        public static Material Bubble(SingerColorGroup group, bool fromLyrics, ThemeBubbleState state)
        {
            var key = (group, fromLyrics, state);
            if (bubbles.TryGetValue(key, out Material existing) && existing != null) return existing;
            Color fill = ThemesPalette.Fill(group, fromLyrics);
            Color ring = ThemesPalette.Ring(group, fromLyrics);
            Material m = new(Find(BubbleShaderName))
            {
                name = $"Theme Bubble {group} {(fromLyrics ? "lyrics" : "title")} {state}",
                hideFlags = HideFlags.DontSave
            };
            (float widthScale, float glow, float dim, Color glowColor) = state switch
            {
                ThemeBubbleState.Dimmed => (1f, 0f, .22f, ring),
                ThemeBubbleState.Hover => (1.25f, 2.6f, 1.15f, Color.Lerp(ring, Color.white, .4f)),
                ThemeBubbleState.Playing => (1.35f, 4.2f, 1.2f, ThemesPalette.Theme),
                _ => (1f, 0f, 1f, ring)
            };
            m.SetColor("_FillColor", fill);
            m.SetColor("_RingColor", state == ThemeBubbleState.Playing ? ThemesPalette.Theme : ring);
            m.SetColor("_BaseColor", fill);
            m.SetFloat("_RingWidth", Mathf.Min(.5f, ThemesPalette.RingWidth(fromLyrics) * widthScale));
            m.SetColor("_GlowColor", glowColor);
            m.SetFloat("_GlowIntensity", glow);
            m.SetFloat("_DimFactor", dim);
            bubbles[key] = m;
            shared.Add(m);
            owned.Add(m);
            return m;
        }

        /// <summary>A material for one flat ground disc (dial or theme pad); the caller sets its properties.</summary>
        public static Material Ground(string materialName, int renderQueue)
        {
            Material m = new(Find(GroundShaderName)) { name = materialName, hideFlags = HideFlags.DontSave, renderQueue = renderQueue };
            owned.Add(m);
            return m;
        }

        /// <summary>An additive line material drawn over the bubbles (hover tethers); <see cref="SetLineColor"/> changes its colour.</summary>
        public static Material Line(string materialName)
        {
            Material m = new(Find(TetherShaderName)) { name = materialName, hideFlags = HideFlags.DontSave };
            m.renderQueue = (int)RenderQueue.Transparent + 20;
            SetLineColor(m, Color.black);
            owned.Add(m);
            return m;
        }

        public static void SetLineColor(Material m, Color c)
        {
            m.SetColor("_Color", c);
        }

        /// <summary>Destroys every material this cache created (rebuild, scene exit, validation).</summary>
        public static void Clear()
        {
            foreach (Material m in owned)
            {
                if (m == null) continue;
                if (Application.isPlaying) Object.Destroy(m);
                else Object.DestroyImmediate(m);
            }
            owned.Clear();
            shared.Clear();
            bubbles.Clear();
        }
    }
}
