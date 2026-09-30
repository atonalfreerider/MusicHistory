#nullable enable
using System;
using System.Collections.Generic;
using TMPro;
using UnityEngine;

namespace MusicHistory.Viewer
{
    public enum LabelPlacement
    {
        /// <summary>Left-aligned, up and to the right of the anchor's bubble (clear of edges leaving along time).</summary>
        Right,
        /// <summary>Centred under the anchor.</summary>
        Below,
        /// <summary>Right-aligned, left of the anchor (year labels of a vertical time axis).</summary>
        Left
    }

    /// <summary>A world-space label managed by <see cref="LabelLayer"/>.</summary>
    public sealed class WorldLabel
    {
        public TextBox Box = null!;
        public Transform? Anchor;
        public Vector3 FixedPosition;
        public float AnchorRadius;
        public LabelPlacement Placement;
        public float Size = 1;
        public bool Visible;
        public bool Dimmed;
        /// <summary>Higher wins when labels collide on screen.</summary>
        public float Priority;
        /// <summary>False when hidden by decluttering (overlap or behind the camera).</summary>
        public bool Placed = true;
        internal MeshRenderer? Renderer;
        internal Vector2 LocalSize;
        public Rect ScreenRect;

        public Vector3 AnchorPosition => Anchor != null ? Anchor.position : FixedPosition;
    }

    /// <summary>
    /// Keeps world-space labels readable: every visible label faces the camera and is scaled
    /// with its distance so it keeps a constant size on screen, independent of bubble size
    /// (a small song's label is as legible as a big one's). Labels share one outlined material
    /// that always draws on top, so bubbles and edges never hide text. Colliding labels are
    /// decluttered greedily by priority (focus, then related songs, then the most influential),
    /// so text never lands on text. One loop in LateUpdate replaces a billboard per label.
    /// </summary>
    public sealed class LabelLayer : MonoBehaviour
    {
        [Tooltip("Label scale per unit of camera distance (constant on-screen size).")]
        public float ScreenSize = .03f;
        [Tooltip("Hide labels of dimmed songs entirely (the walkthrough sets this).")]
        public bool HideDimmed;
        [Tooltip("Pixels kept free around each label when decluttering.")]
        public float LabelPadding = 3f;

        /// <summary>
        /// Screen rects (pixels, origin bottom-left) that labels keep clear of, such as HUD panels.
        /// When set, a label under one of them or cut by the screen edge is hidden, except a focus
        /// label (priority at least <see cref="FocusPriority"/>). Null: no such rule.
        /// </summary>
        [NonSerialized] public Func<Camera, IReadOnlyList<Rect>>? KeepClear;
        public const float FocusPriority = 1e8f;

        public const string OverlayShaderName = "TextMeshPro/Distance Field Overlay";

        readonly List<WorldLabel> labels = new();
        Transform? root;
        Material? sharedMaterial;

        static readonly Color NormalColor = Color.white;
        static readonly Color DimColor = new(.42f, .44f, .47f, 1f);

        public IReadOnlyList<WorldLabel> Labels => labels;
        public Material? SharedLabelMaterial => sharedMaterial;

        public int VisibleCount
        {
            get
            {
                int count = 0;
                foreach (WorldLabel l in labels)
                    if (l.Visible) count++;
                return count;
            }
        }

        public WorldLabel Create(string richText, Transform? anchor, Vector3 fixedPosition, float anchorRadius,
            LabelPlacement placement, float size = 1f)
        {
            if (root == null) root = new GameObject("Labels").transform;
            TextBox box = TextBox.Create(richText, placement switch
            {
                LabelPlacement.Below => TextAlignmentOptions.Top,
                LabelPlacement.Left => TextAlignmentOptions.Right,
                _ => TextAlignmentOptions.Left
            });
            box.transform.SetParent(root, false);
            box.TextField.richText = true;
            box.TextField.fontSharedMaterial = SharedMaterial(box.TextField.fontSharedMaterial);
            WorldLabel label = new()
            {
                Box = box,
                Anchor = anchor,
                FixedPosition = fixedPosition,
                AnchorRadius = anchorRadius,
                Placement = placement,
                Size = size,
                Visible = true,
                Renderer = box.GetComponent<MeshRenderer>()
            };
            labels.Add(label);
            return label;
        }

        Material SharedMaterial(Material fontMaterial)
        {
            if (sharedMaterial != null) return sharedMaterial;
            // TMP's Overlay SDF shader tests depth Always, so bubbles and edges never hide text
            // (it is listed in GraphicsSettings' always-included shaders for player builds).
            sharedMaterial = new Material(fontMaterial)
            {
                name = "MusicHistory Label (outlined, on top)",
                hideFlags = HideFlags.DontSave
            };
            Shader overlay = Shader.Find(OverlayShaderName);
            if (overlay != null) sharedMaterial.shader = overlay;
            sharedMaterial.SetFloat("_OutlineWidth", .22f);
            sharedMaterial.SetColor("_OutlineColor", new Color(.03f, .035f, .045f, 1f));
            sharedMaterial.SetFloat("_FaceDilate", .12f);
            return sharedMaterial;
        }

        public void SetVisible(WorldLabel label, bool visible)
        {
            if (label.Visible == visible) return;
            label.Visible = visible;
            label.Box.gameObject.SetActive(visible);
        }

        public void SetDimmed(WorldLabel label, bool dimmed)
        {
            if (label.Dimmed == dimmed) return;
            label.Dimmed = dimmed;
            label.Box.Color = dimmed ? DimColor : NormalColor;
        }

        void LateUpdate() => Refresh(Camera.main);

        readonly List<WorldLabel> candidates = new();
        readonly List<Rect> placed = new();

        /// <summary>Positions, orients, scales and declutters every visible label for <paramref name="cam"/>.</summary>
        public void Refresh(Camera? cam)
        {
            if (cam == null) return;
            Transform ct = cam.transform;
            Quaternion rotation = ct.rotation;
            Vector3 right = ct.right, up = ct.up, forward = ct.forward, origin = ct.position;
            float tanHalf = Mathf.Tan(cam.fieldOfView * .5f * Mathf.Deg2Rad);
            float fovFactor = cam.orthographic ? 1f : tanHalf / Mathf.Tan(30f * Mathf.Deg2Rad);
            float pixelHeight = Mathf.Max(1, cam.pixelHeight);
            candidates.Clear();
            foreach (WorldLabel label in labels)
            {
                if (!label.Visible) continue;
                Vector3 anchor = label.AnchorPosition;
                float depth = Vector3.Dot(anchor - origin, forward);
                float distance = cam.orthographic ? cam.orthographicSize * 2f : Mathf.Max(.5f, depth);
                float scale = distance * ScreenSize * label.Size * fovFactor;
                if (label.LocalSize == Vector2.zero) label.LocalSize = label.Box.TextField.GetPreferredValues();
                float halfHeight = .5f * label.LocalSize.y * scale;
                Vector3 position = label.Placement switch
                {
                    LabelPlacement.Below => anchor - up * (label.AnchorRadius + .15f * scale),
                    LabelPlacement.Left => anchor - right * (label.AnchorRadius + .3f * scale),
                    _ => anchor + right * (label.AnchorRadius * .75f + .2f * scale) +
                         up * (label.AnchorRadius * .7f + halfHeight)
                };
                Transform t = label.Box.transform;
                t.SetPositionAndRotation(position, rotation);
                t.localScale = new Vector3(scale, scale, scale);

                bool hide = (!cam.orthographic && depth <= cam.nearClipPlane) || (HideDimmed && label.Dimmed);
                if (hide)
                {
                    SetPlaced(label, false);
                    continue;
                }
                float worldPerPixel = cam.orthographic
                    ? cam.orthographicSize * 2f / pixelHeight
                    : 2f * Mathf.Max(.01f, Vector3.Dot(position - origin, forward)) * tanHalf / pixelHeight;
                Vector2 size = label.LocalSize * (scale / worldPerPixel);
                Vector3 sp = cam.WorldToScreenPoint(position);
                label.ScreenRect = label.Placement switch
                {
                    LabelPlacement.Below => new Rect(sp.x - size.x * .5f, sp.y - size.y, size.x, size.y),
                    LabelPlacement.Left => new Rect(sp.x - size.x, sp.y - size.y * .5f, size.x, size.y),
                    _ => new Rect(sp.x, sp.y - size.y * .5f, size.x, size.y)
                };
                candidates.Add(label);
            }

            // Greedy declutter: highest priority first; a label stays only if it overlaps no kept one.
            candidates.Sort((a, b) => b.Priority.CompareTo(a.Priority));
            placed.Clear();
            IReadOnlyList<Rect>? keepClear = KeepClear?.Invoke(cam);
            Rect screen = new(0, 0, cam.pixelWidth, cam.pixelHeight);
            foreach (WorldLabel label in candidates)
            {
                Rect r = label.ScreenRect;
                Rect padded = new(r.xMin - LabelPadding, r.yMin - LabelPadding, r.width + 2 * LabelPadding, r.height + 2 * LabelPadding);
                bool clear = true;
                if (keepClear != null && label.Priority < FocusPriority)
                {
                    // Under a HUD panel or cut by the screen edge, a label cannot be read.
                    clear = r.xMin >= 0 && r.yMin >= 0 && r.xMax <= screen.width && r.yMax <= screen.height;
                    for (int i = 0; clear && i < keepClear.Count; i++)
                        if (r.Overlaps(keepClear[i])) clear = false;
                }
                foreach (Rect other in placed)
                {
                    if (!padded.Overlaps(other)) continue;
                    clear = false;
                    break;
                }
                SetPlaced(label, clear);
                if (clear) placed.Add(r);
            }
        }

        static void SetPlaced(WorldLabel label, bool shown)
        {
            label.Placed = shown;
            if (label.Renderer != null && label.Renderer.enabled != shown) label.Renderer.enabled = shown;
        }

        /// <summary>Labels currently drawn (visible and not decluttered away).</summary>
        public int PlacedCount
        {
            get
            {
                int count = 0;
                foreach (WorldLabel l in labels)
                    if (l.Visible && l.Placed) count++;
                return count;
            }
        }

        /// <summary>Forces TMP to build meshes now (edit-mode rendering).</summary>
        public void ForceMeshUpdate()
        {
            foreach (WorldLabel label in labels)
                if (label.Visible) label.Box.TextField.ForceMeshUpdate();
        }

        public void Clear()
        {
            foreach (WorldLabel label in labels)
            {
                if (label.Box == null) continue;
                if (Application.isPlaying) Destroy(label.Box.gameObject);
                else DestroyImmediate(label.Box.gameObject);
            }
            labels.Clear();
            if (root != null)
            {
                if (Application.isPlaying) Destroy(root.gameObject);
                else DestroyImmediate(root.gameObject);
            }
            root = null;
            if (sharedMaterial != null)
            {
                if (Application.isPlaying) Destroy(sharedMaterial);
                else DestroyImmediate(sharedMaterial);
            }
            sharedMaterial = null;
        }
    }
}
