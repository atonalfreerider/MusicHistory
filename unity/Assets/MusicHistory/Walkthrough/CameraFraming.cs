#nullable enable
using System.Collections.Generic;
using UnityEngine;

namespace MusicHistory.Walkthrough
{
    /// <summary>Camera poses that frame a set of spheres from a given direction.</summary>
    public static class CameraFraming
    {
        /// <summary>The whole screen as a normalized viewport rect.</summary>
        public static readonly Rect FullScreen = new(0, 0, 1, 1);

        /// <summary>
        /// Position and rotation that look along <paramref name="forward"/> and fit every sphere
        /// (centre, radius) inside <paramref name="viewport"/> (normalized screen rect, so HUD
        /// panels can be kept clear), with <paramref name="margin"/> (1 = tight). The distance is
        /// solved per sphere with perspective, so near and far parts of a deep graph both fit.
        /// </summary>
        public static (Vector3 position, Quaternion rotation) Frame(Camera cam, IReadOnlyList<(Vector3 center, float radius)> items,
            Vector3 forward, float margin, float minDistance, Rect? viewport = null)
        {
            Quaternion rotation = Quaternion.LookRotation(forward.normalized, Vector3.up);
            if (items.Count == 0) return (cam.transform.position, rotation);
            Vector3 right = rotation * Vector3.right, up = rotation * Vector3.up, fwd = rotation * Vector3.forward;
            Rect view = viewport ?? FullScreen;

            Vector3 min = new(float.MaxValue, float.MaxValue, float.MaxValue);
            Vector3 max = new(float.MinValue, float.MinValue, float.MinValue);
            foreach ((Vector3 c, float r) in items)
            {
                Vector3 p = new(Vector3.Dot(c, right), Vector3.Dot(c, up), Vector3.Dot(c, fwd));
                min = Vector3.Min(min, p - Vector3.one * r);
                max = Vector3.Max(max, p + Vector3.one * r);
            }
            Vector3 mid = (min + max) * .5f;
            Vector3 center = right * mid.x + up * mid.y + fwd * mid.z;

            float tanV = Mathf.Tan(cam.fieldOfView * .5f * Mathf.Deg2Rad);
            float aspect = cam.aspect > 0 ? cam.aspect : 16f / 9f;
            float tanH = tanV * aspect;
            // Viewport rect in NDC: centre (cx, cy) and half extents (hx, hy), shrunk by the margin.
            float cx = view.center.x * 2f - 1f, cy = view.center.y * 2f - 1f;
            float hx = Mathf.Max(.05f, view.width / Mathf.Max(1f, margin)), hy = Mathf.Max(.05f, view.height / Mathf.Max(1f, margin));

            float distance = minDistance;
            foreach ((Vector3 c, float r) in items)
            {
                Vector3 rel = c - center;
                float x = Vector3.Dot(rel, right), y = Vector3.Dot(rel, up), z = Vector3.Dot(rel, fwd);
                // A point is inside when |x - cx·tanH·z| + r <= hx·tanH·(z + d); same for y.
                float dx = (Mathf.Abs(x - cx * tanH * z) + r) / (hx * tanH) - z;
                float dy = (Mathf.Abs(y - cy * tanV * z) + r) / (hy * tanV) - z;
                distance = Mathf.Max(distance, Mathf.Max(dx, dy));
            }
            // Shift the camera so the content centre lands on the viewport centre.
            Vector3 position = center - fwd * distance - right * (cx * tanH * distance) - up * (cy * tanV * distance);
            return (position, rotation);
        }

        public static float SmoothStep01(float t)
        {
            t = Mathf.Clamp01(t);
            return t * t * (3f - 2f * t);
        }
    }
}
