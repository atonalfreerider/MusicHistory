#nullable enable
using System;
using UnityEngine;

namespace MusicHistory.Viewer
{
    /// <summary>
    /// A directed influence edge (fork of Unity-FDG's DirectedFollowEdge): a tapered shared mesh,
    /// wide at the influencer and narrow at the influenced song, running from one bubble surface
    /// to the other. <see cref="VisibleFraction"/> (0..1) grows it from the influencer, which the
    /// walkthrough animates. Colour comes from a shared per-(channel, state, kind) material; no
    /// edge owns a material.
    /// </summary>
    public sealed class InfluenceEdge : MonoBehaviour
    {
        const int Sides = 8;
        const float TipRatio = .22f;
        static Mesh? sharedMesh;

        [NonSerialized] public EdgeRecord Record = null!;
        public SongNode Source = null!;
        public SongNode Target = null!;
        public EdgeChannel Channel;
        public bool IsTree;
        public EdgeTier Tier = EdgeTier.Branch;
        /// <summary>Radius of the wide end, world units.</summary>
        public float Width = .15f;
        public float EndpointPadding = .05f;

        MeshRenderer meshRenderer = null!;
        EdgeState state = EdgeState.Normal;
        float visibleFraction = 1f;

        public EdgeState State => state;
        public MeshRenderer Renderer => meshRenderer;
        public bool Shown => gameObject.activeSelf;

        public float VisibleFraction
        {
            get => visibleFraction;
            set
            {
                float v = Mathf.Clamp01(value);
                if (Mathf.Approximately(v, visibleFraction)) return;
                visibleFraction = v;
                UpdateGeometry();
            }
        }

        public static InfluenceEdge Create(EdgeRecord record, SongNode source, SongNode target, float width,
            float padding, Transform parent, EdgeTier tier = EdgeTier.Branch)
        {
            GameObject go = new($"{(record.IsTree ? "tree" : "secondary")} {source.NodeId}->{target.NodeId}");
            go.transform.SetParent(parent, false);
            InfluenceEdge edge = go.AddComponent<InfluenceEdge>();
            edge.Record = record;
            edge.Source = source;
            edge.Target = target;
            edge.IsTree = record.IsTree;
            edge.Channel = SongPalette.Classify(record.Channels, record.PrimaryChannel);
            edge.Tier = tier;
            edge.Width = width;
            edge.EndpointPadding = padding;
            go.AddComponent<MeshFilter>().sharedMesh = SharedMesh();
            edge.meshRenderer = go.AddComponent<MeshRenderer>();
            edge.meshRenderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            edge.meshRenderer.receiveShadows = false;
            edge.meshRenderer.lightProbeUsage = UnityEngine.Rendering.LightProbeUsage.Off;
            edge.meshRenderer.reflectionProbeUsage = UnityEngine.Rendering.ReflectionProbeUsage.Off;
            edge.meshRenderer.sharedMaterial = GraphMaterials.Edge(edge.Channel, EdgeState.Normal, edge.IsTree, tier);
            edge.UpdateGeometry();
            return edge;
        }

        public void SetState(EdgeState newState)
        {
            if (newState == state) return;
            state = newState;
            meshRenderer.sharedMaterial = GraphMaterials.Edge(Channel, newState, IsTree, Tier);
        }

        public void SetShown(bool shown)
        {
            if (gameObject.activeSelf != shown) gameObject.SetActive(shown);
        }

        public void UpdateGeometry()
        {
            if (Source == null || Target == null) return;
            Vector3 from = Source.transform.position, to = Target.transform.position;
            Vector3 direction = to - from;
            float centerDistance = direction.magnitude;
            if (centerDistance <= 1e-5f)
            {
                meshRenderer.enabled = false;
                return;
            }
            direction /= centerDistance;
            float sourceInset = Source.Radius + EndpointPadding;
            float targetInset = Target.Radius + EndpointPadding;
            float insetScale = Mathf.Min(1f, centerDistance / Mathf.Max(1e-5f, sourceInset + targetInset));
            Vector3 start = from + direction * (sourceInset * insetScale);
            Vector3 targetSurface = to - direction * (targetInset * insetScale);
            Vector3 end = Vector3.Lerp(start, targetSurface, visibleFraction);
            float length = Vector3.Distance(start, end);
            meshRenderer.enabled = length > 1e-4f;
            // The wide end never exceeds the influencer's bubble.
            float width = Mathf.Min(Width, Source.Radius * .8f);

            Transform t = transform;
            t.SetPositionAndRotation(start, Quaternion.FromToRotation(Vector3.up, direction));
            t.localScale = new Vector3(width, Mathf.Max(length, 1e-4f), width);
        }

        /// <summary>Tapered prism: radius 1 at y = 0 (influencer), <see cref="TipRatio"/> at y = 1, capped.</summary>
        static Mesh SharedMesh()
        {
            if (sharedMesh != null) return sharedMesh;
            Vector3[] vertices = new Vector3[Sides * 2 + 2];
            int[] triangles = new int[Sides * 12];
            for (int side = 0; side < Sides; side++)
            {
                float angle = side * Mathf.PI * 2f / Sides;
                Vector3 radial = new(Mathf.Cos(angle), 0, Mathf.Sin(angle));
                vertices[side] = radial;
                vertices[side + Sides] = Vector3.up + radial * TipRatio;
            }
            int baseCenter = Sides * 2, tipCenter = Sides * 2 + 1;
            vertices[baseCenter] = Vector3.zero;
            vertices[tipCenter] = Vector3.up;
            int o = 0;
            for (int side = 0; side < Sides; side++)
            {
                int next = (side + 1) % Sides;
                triangles[o++] = side; triangles[o++] = side + Sides; triangles[o++] = next + Sides;
                triangles[o++] = side; triangles[o++] = next + Sides; triangles[o++] = next;
                triangles[o++] = baseCenter; triangles[o++] = side; triangles[o++] = next;
                triangles[o++] = tipCenter; triangles[o++] = next + Sides; triangles[o++] = side + Sides;
            }
            sharedMesh = new Mesh { name = "Tapered Influence Edge", hideFlags = HideFlags.DontSave };
            sharedMesh.vertices = vertices;
            sharedMesh.triangles = triangles;
            sharedMesh.RecalculateNormals();
            sharedMesh.RecalculateBounds();
            return sharedMesh;
        }

        public static void ReleaseSharedMesh()
        {
            if (sharedMesh == null) return;
            if (Application.isPlaying) Destroy(sharedMesh);
            else DestroyImmediate(sharedMesh);
            sharedMesh = null;
        }
    }
}
