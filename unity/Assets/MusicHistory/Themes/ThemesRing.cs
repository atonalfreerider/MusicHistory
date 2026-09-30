#nullable enable
using System.Collections.Generic;
using MusicHistory.Viewer;
using UnityEngine;

namespace MusicHistory.Themes
{
    /// <summary>One theme on the ring: a flat gold pad and a large label outside the ring.</summary>
    public sealed class ThemeAnchorNode : MonoBehaviour
    {
        public ThemeAnchorRecord Anchor = null!;
        public WorldLabel Label = null!;
        public MeshRenderer Pad = null!;
        public float PadRadius;
        /// <summary>How far this theme's songs reach outward past it (at least the pad radius): its label sits beyond.</summary>
        public float Reach;
        /// <summary>Unit vector from the ring centre to the anchor, in the ring plane.</summary>
        public Vector3 Outward;
        Material padMaterial = null!;
        AnchorLook look = (AnchorLook)(-1);

        public enum AnchorLook
        {
            Normal,
            /// <summary>One of the hovered song's top themes, or the filtered theme.</summary>
            Lit,
            /// <summary>Another theme is filtered.</summary>
            Faded
        }

        public AnchorLook Look => look;
        public Material PadMaterial => padMaterial;

        /// <summary>The filter key shown on the ring: 1..9, then 0 for the tenth theme.</summary>
        public static string KeyName(int anchorId) => anchorId == 10 ? "0" : anchorId.ToString();

        public string LabelText =>
            $"<b>{Wrap(Anchor.Label)}</b>\n<size=62%><color={ThemesPalette.ToHex(ThemesPalette.ThemeDim)}>[{KeyName(Anchor.AnchorId)}]  " +
            $"{Anchor.TopCount} {(Anchor.TopCount == 1 ? "song" : "songs")}</color></size>";

        /// <summary>The theme text, escaped, broken onto two lines at the space nearest its middle when it is long.</summary>
        public static string Wrap(string label, int maxLine = 26)
        {
            if (label.Length <= maxLine) return GraphHud.Esc(label);
            int mid = label.Length / 2, best = -1;
            for (int i = 0; i < label.Length; i++)
                if (label[i] == ' ' && (best < 0 || System.Math.Abs(i - mid) < System.Math.Abs(best - mid))) best = i;
            return best < 0 ? GraphHud.Esc(label) : GraphHud.Esc(label.Substring(0, best)) + "\n" + GraphHud.Esc(label.Substring(best + 1));
        }

        internal void Init(ThemeAnchorRecord anchor, float padRadius, Material material, Mesh disc)
        {
            Anchor = anchor;
            PadRadius = padRadius;
            Reach = padRadius;
            padMaterial = material;
            Vector3 flat = new(anchor.Position.x, 0f, anchor.Position.z);
            Outward = flat.sqrMagnitude > 1e-8f ? flat.normalized : Vector3.right;

            GameObject pad = new("Pad");
            pad.transform.SetParent(transform, false);
            pad.transform.localPosition = new Vector3(0f, -.03f, 0f);
            pad.transform.localScale = Vector3.one * (padRadius * 2f);
            pad.AddComponent<MeshFilter>().sharedMesh = disc;
            Pad = pad.AddComponent<MeshRenderer>();
            Pad.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            Pad.receiveShadows = false;
            Pad.lightProbeUsage = UnityEngine.Rendering.LightProbeUsage.Off;
            Pad.reflectionProbeUsage = UnityEngine.Rendering.ReflectionProbeUsage.Off;
            Pad.sharedMaterial = material;
            SetLook(AnchorLook.Normal);
        }

        public void SetLook(AnchorLook next)
        {
            if (next == look) return;
            look = next;
            Color t = ThemesPalette.Theme;
            (float fill, float ring, float px) = next switch
            {
                AnchorLook.Lit => (.26f, 1f, 2.6f),
                AnchorLook.Faded => (.035f, .22f, 1.2f),
                _ => (.12f, .6f, 1.7f)
            };
            padMaterial.SetColor("_FillColor", new Color(t.r, t.g, t.b, fill));
            padMaterial.SetFloat("_FillPower", 1.4f);
            padMaterial.SetColor("_RingColor", new Color(t.r, t.g, t.b, ring));
            padMaterial.SetFloat("_RingRadius", .965f);
            padMaterial.SetFloat("_RingPixels", px);
            padMaterial.SetFloat("_GuideCount", 0f);
            padMaterial.SetFloat("_SpokeCount", 0f);
        }
    }

    /// <summary>
    /// The ring of themes: a flat dial under the cloud (the ring itself, a guide circle at half the
    /// radius and a faint spoke to every theme), one gold pad per theme and the theme labels. The
    /// labels are re-placed every frame so each sits just outside the ring on screen, centred on
    /// its theme's outward direction, from any orbit angle.
    /// </summary>
    public sealed class ThemesRing : MonoBehaviour
    {
        public readonly List<ThemeAnchorNode> Anchors = new();
        public float RingRadius { get; private set; }
        public float PadRadius { get; private set; }
        public MeshRenderer Dial { get; private set; } = null!;
        /// <summary>Screen gap (pixels) between a pad's rim and its label.</summary>
        public float LabelGapPixels = 10f;
        public float LabelSize = 1.4f;

        LabelLayer labels = null!;
        static Mesh? discMesh;

        public const int DialQueue = 2990;
        public const int PadQueue = 2995;

        public static ThemesRing Create(ThemesGraphData data, float padRadius, LabelLayer labels, Transform parent)
        {
            GameObject go = new("Theme Ring");
            go.transform.SetParent(parent, false);
            ThemesRing ring = go.AddComponent<ThemesRing>();
            ring.Build(data, padRadius, labels);
            return ring;
        }

        void Build(ThemesGraphData data, float padRadius, LabelLayer layer)
        {
            labels = layer;
            RingRadius = data.RingRadius;
            PadRadius = padRadius;
            float dialRadius = RingRadius * 1.16f;

            GameObject dial = new("Dial");
            dial.transform.SetParent(transform, false);
            dial.transform.localPosition = new Vector3(0f, -.06f, 0f);
            dial.transform.localScale = Vector3.one * (dialRadius * 2f);
            dial.AddComponent<MeshFilter>().sharedMesh = DiscMesh();
            Dial = dial.AddComponent<MeshRenderer>();
            Dial.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            Dial.receiveShadows = false;
            Material dialMaterial = ThemesMaterials.Ground("Themes Dial", DialQueue);
            dialMaterial.SetColor("_FillColor", new Color(.30f, .36f, .48f, .16f));
            dialMaterial.SetFloat("_FillPower", 2.2f);
            dialMaterial.SetColor("_RingColor", new Color(.86f, .88f, .92f, .42f));
            dialMaterial.SetFloat("_RingRadius", RingRadius / dialRadius);
            dialMaterial.SetFloat("_RingPixels", 1.5f);
            dialMaterial.SetColor("_GuideColor", new Color(.8f, .84f, .9f, .10f));
            dialMaterial.SetFloat("_GuideCount", 1f);
            dialMaterial.SetColor("_SpokeColor", new Color(.8f, .84f, .9f, .13f));
            dialMaterial.SetFloat("_SpokeCount", data.Anchors.Count);
            dialMaterial.SetFloat("_SpokeAngle", data.Anchors.Count > 0 ? (float)data.Anchors[0].AngleDegrees : 0f);
            dialMaterial.SetFloat("_SpokePixels", 1.1f);
            dialMaterial.SetFloat("_SpokeInner", .035f);
            Dial.sharedMaterial = dialMaterial;

            foreach (ThemeAnchorRecord a in data.Anchors)
            {
                GameObject go = new($"Theme {a.AnchorId:00} {a.Short}");
                go.transform.SetParent(transform, false);
                go.transform.position = a.Position;
                ThemeAnchorNode node = go.AddComponent<ThemeAnchorNode>();
                node.Init(a, padRadius, ThemesMaterials.Ground($"Theme Pad {a.AnchorId}", PadQueue), DiscMesh());
                node.Label = layer.Create(node.LabelText, null, a.Position, 0f, LabelPlacement.Below, LabelSize);
                node.Label.Priority = 1e9f + a.AnchorId;
                Anchors.Add(node);
            }
        }

        /// <summary>Unit quad in the x-z plane with 0..1 UVs (the ground shader draws the disc in it).</summary>
        static Mesh DiscMesh()
        {
            if (discMesh != null) return discMesh;
            discMesh = new Mesh
            {
                name = "Themes Ground Quad",
                hideFlags = HideFlags.DontSave,
                vertices = new[]
                {
                    new Vector3(-.5f, 0, -.5f), new Vector3(.5f, 0, -.5f),
                    new Vector3(.5f, 0, .5f), new Vector3(-.5f, 0, .5f)
                },
                uv = new[] { new Vector2(0, 0), new Vector2(1, 0), new Vector2(1, 1), new Vector2(0, 1) },
                triangles = new[] { 0, 3, 2, 0, 2, 1 }
            };
            discMesh.RecalculateNormals();
            discMesh.bounds = new Bounds(Vector3.zero, new Vector3(1f, .05f, 1f));
            return discMesh;
        }

        /// <summary>
        /// Puts each theme label just outside its pad along the theme's outward direction as seen on
        /// screen, centred on that direction (the label layer then keeps its constant screen size).
        /// Uses the label's size from the previous refresh, so call before <see cref="LabelLayer.Refresh"/>.
        /// </summary>
        public void PlaceLabels(Camera? cam)
        {
            if (cam == null || Anchors.Count == 0) return;
            Transform ct = cam.transform;
            Vector3 origin = ct.position, forward = ct.forward, up = ct.up;
            Vector3 centerWorld = transform.position;
            Vector3 so = cam.WorldToScreenPoint(centerWorld);
            float tanHalf = Mathf.Tan(cam.fieldOfView * .5f * Mathf.Deg2Rad);
            float fovFactor = cam.orthographic ? 1f : tanHalf / Mathf.Tan(30f * Mathf.Deg2Rad);
            foreach (ThemeAnchorNode node in Anchors)
            {
                Vector3 a = node.transform.position;
                float depth = Vector3.Dot(a - origin, forward);
                if (!cam.orthographic && depth <= cam.nearClipPlane) continue;
                Vector3 sa = cam.WorldToScreenPoint(a);
                Vector2 dir = new(sa.x - so.x, sa.y - so.y);
                if (dir.sqrMagnitude < 1e-4f)
                {
                    Vector3 edge0 = cam.WorldToScreenPoint(a + node.Outward);
                    dir = new Vector2(edge0.x - sa.x, edge0.y - sa.y);
                }
                dir = dir.sqrMagnitude > 1e-8f ? dir.normalized : Vector2.down;
                Vector3 se = cam.WorldToScreenPoint(a + node.Outward * node.Reach);
                float gap = new Vector2(se.x - sa.x, se.y - sa.y).magnitude;
                Rect r = node.Label.ScreenRect;
                float w = r.width, h = r.height;
                float half = Mathf.Min(Mathf.Abs(dir.x) > 1e-4f ? w * .5f / Mathf.Abs(dir.x) : float.MaxValue,
                                       Mathf.Abs(dir.y) > 1e-4f ? h * .5f / Mathf.Abs(dir.y) : float.MaxValue);
                if (half == float.MaxValue) half = 0f;
                Vector2 c = new Vector2(sa.x, sa.y) + dir * (gap + half + LabelGapPixels);
                // Below placement hangs the text under its point: lift the point by half the label height,
                // plus the layer's own small offset (0.15 x label scale).
                float distance = cam.orthographic ? cam.orthographicSize * 2f : Mathf.Max(.5f, depth);
                float scale = distance * labels.ScreenSize * node.Label.Size * fovFactor;
                Vector3 world = cam.ScreenToWorldPoint(new Vector3(c.x, c.y + h * .5f, depth));
                node.Label.FixedPosition = world + up * (.15f * scale);
            }
        }

        /// <summary>
        /// Measures how far songs pile up outward past each theme (a dense cluster spreads beyond its
        /// pad), so the theme's label is placed beyond them rather than on top of them.
        /// </summary>
        public void SetReach(IReadOnlyList<Vector3> positions, float songRadius)
        {
            foreach (ThemeAnchorNode node in Anchors)
            {
                float reach = node.PadRadius;
                Vector3 a = node.transform.position;
                foreach (Vector3 p in positions)
                {
                    Vector3 rel = new(p.x - a.x, 0f, p.z - a.z);
                    float along = Vector3.Dot(rel, node.Outward);
                    if (along <= 0f) continue;
                    float lateral = (rel - node.Outward * along).magnitude;
                    if (lateral < node.PadRadius * 1.5f) reach = Mathf.Max(reach, along + songRadius);
                }
                node.Reach = Mathf.Min(reach, RingRadius * .5f);
            }
        }

        public static void ReleaseSharedMesh()
        {
            if (discMesh == null) return;
            if (Application.isPlaying) Destroy(discMesh);
            else DestroyImmediate(discMesh);
            discMesh = null;
        }
    }
}
