#nullable enable
using System;
using System.Collections.Generic;
using UnityEngine;

namespace MusicHistory.Viewer
{
    /// <summary>Where time runs on screen.</summary>
    public enum TimeAxisView
    {
        /// <summary>Time runs left to right (+x); the graph fills a landscape screen.</summary>
        LeftToRight,
        /// <summary>Time runs bottom to top (+y).</summary>
        BottomToTop,
        /// <summary>Use the layout's own axis unchanged.</summary>
        AsLaidOut
    }

    /// <summary>
    /// Display-space geometry of the graph: the time axis, two lateral axes, and the linear map
    /// from a song's time_value to its coordinate on the time axis (fitted from the nodes, so the
    /// timeline matches whatever layout produced the positions).
    /// </summary>
    public sealed class GraphFrame
    {
        public Vector3 TimeDir = Vector3.right;
        /// <summary>Lateral axis shown vertically in the default view.</summary>
        public Vector3 Lateral1 = Vector3.up;
        /// <summary>Lateral axis pointing into the screen in the default view.</summary>
        public Vector3 Lateral2 = Vector3.forward;
        public double Slope = 1;
        public double Intercept;
        /// <summary>Largest distance (years) between a node's time coordinate and its date: 0 = time exactly pinned.</summary>
        public double MaxResidualYears;
        public Vector2 LateralCenter;
        public float LateralRadius = 1;
        public Bounds Bounds;
        /// <summary>Default camera direction: across the time axis, slightly from above.</summary>
        public Vector3 ViewForward = new Vector3(0, -.2f, 1).normalized;
        public string Description = "";

        public float TimeCoord(double timeValue) => (float)(Intercept + Slope * timeValue);

        public double TimeAt(float coordinate) => Math.Abs(Slope) < 1e-9 ? 0 : (coordinate - Intercept) / Slope;

        public Vector3 Point(double timeValue, float lateral1, float lateral2) =>
            TimeDir * TimeCoord(timeValue) + Lateral1 * lateral1 + Lateral2 * lateral2;
    }

    /// <summary>
    /// Turns layout positions into display positions and builds the frame. When the layout stage
    /// has not run (positions NULL), a deterministic fallback places each song at its date on the
    /// time axis and spreads trees laterally around their parents.
    /// </summary>
    public static class GraphLayoutMapping
    {
        public static Vector3[] DisplayPositions(SongGraphData data, TimeAxisView view, out GraphFrame frame)
        {
            int n = data.Songs.Count;
            Vector3 layoutTime = AxisVector(data.Layout.TimeAxis) * DirectionSign(data.Layout.TimeDirection);
            Vector3 displayTime = view switch
            {
                TimeAxisView.LeftToRight => Vector3.right,
                TimeAxisView.BottomToTop => Vector3.up,
                _ => layoutTime
            };
            frame = new GraphFrame { TimeDir = displayTime };
            OrthonormalLaterals(frame);

            Vector3[] positions = new Vector3[n];
            if (data.HasPositions)
            {
                Quaternion rotation = Quaternion.FromToRotation(layoutTime, displayTime);
                for (int i = 0; i < n; i++) positions[i] = rotation * data.Songs[i].LayoutPosition!.Value;
                frame.Description = data.Layout.Present
                    ? $"layout_run: time {data.Layout.TimeAxis}/{data.Layout.TimeDirection}, {data.Layout.YearScale:0.##} units/year" +
                      $"{(string.IsNullOrEmpty(data.Layout.Device) ? "" : ", " + data.Layout.Device)}"
                    : "layout positions (no layout_run row)";
            }
            else
            {
                FallbackLayout(data, frame, positions);
                frame.Description = "fallback layout (positions are NULL: layout has not run)";
            }

            FitTime(data, positions, frame);
            MeasureLaterals(positions, frame);
            return positions;
        }

        static Vector3 AxisVector(string axis) => axis switch
        {
            "x" => Vector3.right,
            "z" => Vector3.forward,
            _ => Vector3.up
        };

        static float DirectionSign(string direction) => direction switch
        {
            "down" or "negative" or "-" or "backward" or "left" => -1f,
            _ => 1f
        };

        static void OrthonormalLaterals(GraphFrame frame)
        {
            Vector3 t = frame.TimeDir.normalized;
            // Prefer world up as the on-screen lateral; if time itself is vertical, use x.
            Vector3 l1 = Mathf.Abs(Vector3.Dot(t, Vector3.up)) > .9f ? Vector3.right : Vector3.up;
            l1 = (l1 - Vector3.Dot(l1, t) * t).normalized;
            Vector3 l2 = Vector3.Cross(t, l1).normalized;
            if (Vector3.Dot(l2, Vector3.forward) < 0) l2 = -l2;
            frame.TimeDir = t;
            frame.Lateral1 = l1;
            frame.Lateral2 = l2;
            // Look across time: along +z unless time runs along z.
            Vector3 across = Mathf.Abs(Vector3.Dot(t, Vector3.forward)) > .9f ? Vector3.right : Vector3.forward;
            frame.ViewForward = (across - .2f * Vector3.up).normalized;
        }

        /// <summary>Least-squares map time_value -> coordinate along TimeDir, and how well nodes sit on it.</summary>
        static void FitTime(SongGraphData data, Vector3[] positions, GraphFrame frame)
        {
            int n = positions.Length;
            if (n == 0) return;
            double sx = 0, sy = 0, sxx = 0, sxy = 0;
            for (int i = 0; i < n; i++)
            {
                double x = data.Songs[i].TimeValue, y = Vector3.Dot(positions[i], frame.TimeDir);
                sx += x; sy += y; sxx += x * x; sxy += x * y;
            }
            double denom = n * sxx - sx * sx;
            if (n >= 2 && Math.Abs(denom) > 1e-9)
            {
                frame.Slope = (n * sxy - sx * sy) / denom;
                frame.Intercept = (sy - frame.Slope * sx) / n;
            }
            else
            {
                frame.Slope = data.Layout.YearScale;
                frame.Intercept = (n > 0 ? Vector3.Dot(positions[0], frame.TimeDir) : 0) - frame.Slope * (n > 0 ? data.Songs[0].TimeValue : 0);
            }
            if (Math.Abs(frame.Slope) < 1e-9) frame.Slope = data.Layout.YearScale;
            double worst = 0;
            for (int i = 0; i < n; i++)
            {
                double coord = Vector3.Dot(positions[i], frame.TimeDir);
                worst = Math.Max(worst, Math.Abs(frame.TimeAt((float)coord) - data.Songs[i].TimeValue));
            }
            frame.MaxResidualYears = worst;
        }

        static void MeasureLaterals(Vector3[] positions, GraphFrame frame)
        {
            if (positions.Length == 0)
            {
                frame.Bounds = new Bounds(Vector3.zero, Vector3.one);
                return;
            }
            Vector2 sum = Vector2.zero;
            Bounds bounds = new(positions[0], Vector3.zero);
            foreach (Vector3 p in positions)
            {
                sum += new Vector2(Vector3.Dot(p, frame.Lateral1), Vector3.Dot(p, frame.Lateral2));
                bounds.Encapsulate(p);
            }
            frame.LateralCenter = sum / positions.Length;
            float radius = 0;
            foreach (Vector3 p in positions)
            {
                Vector2 lateral = new(Vector3.Dot(p, frame.Lateral1), Vector3.Dot(p, frame.Lateral2));
                radius = Mathf.Max(radius, (lateral - frame.LateralCenter).magnitude);
            }
            frame.LateralRadius = Mathf.Max(radius, 1f);
            frame.Bounds = bounds;
        }

        const float GoldenAngle = 2.39996323f;

        /// <summary>
        /// Deterministic stand-in for the GPU layout: time pinned exactly, roots on a ring,
        /// children at golden-angle offsets from their parent, then a short lateral relaxation
        /// between songs that are close in time.
        /// </summary>
        static void FallbackLayout(SongGraphData data, GraphFrame frame, Vector3[] positions)
        {
            int n = data.Songs.Count;
            double minTime = data.Layout.MinTime ?? data.MinTime;
            double yearScale = data.Layout.YearScale > 0 ? data.Layout.YearScale : 2;
            float[] t = new float[n];
            Vector2[] lateral = new Vector2[n];
            float[] radius = new float[n];
            int[] childOrdinal = new int[n + 1];
            List<int> roots = new();
            for (int i = 0; i < n; i++)
            {
                SongRecord s = data.Songs[i];
                t[i] = (float)((s.TimeValue - minTime) * yearScale);
                radius[i] = .2f * Mathf.Sqrt(s.Descendants + 1);
                if (s.TreeParent == null) roots.Add(i);
            }
            roots.Sort((a, b) => data.Songs[b].Descendants.CompareTo(data.Songs[a].Descendants));
            float ring = 3f + 2.5f * Mathf.Sqrt(roots.Count);
            for (int k = 0; k < roots.Count; k++)
            {
                float angle = k * GoldenAngle;
                float r = roots.Count == 1 ? 0 : ring * Mathf.Sqrt((k + .5f) / roots.Count);
                lateral[roots[k]] = new Vector2(Mathf.Cos(angle), Mathf.Sin(angle)) * r;
            }
            for (int i = 0; i < n; i++)
            {
                SongRecord s = data.Songs[i];
                if (s.TreeParent is not int parentId || parentId < 1 || parentId > i) continue;
                int parent = parentId - 1;
                int ordinal = childOrdinal[parentId]++;
                float angle = parentId * 1.3f + ordinal * GoldenAngle;
                float distance = 1.2f + radius[parent] + radius[i];
                lateral[i] = lateral[parent] + new Vector2(Mathf.Cos(angle), Mathf.Sin(angle)) * distance;
            }

            // Relax laterally: keep songs close in time apart, keep children near their parents.
            const float window = 6f;
            Vector2[] push = new Vector2[n];
            for (int iteration = 0; iteration < 60; iteration++)
            {
                Array.Clear(push, 0, n);
                for (int i = 0; i < n; i++)
                {
                    for (int j = i + 1; j < n && t[j] - t[i] < window; j++)
                    {
                        Vector2 d = lateral[i] - lateral[j];
                        float dt = t[j] - t[i];
                        float want = radius[i] + radius[j] + .6f;
                        float lat = d.magnitude;
                        float dist3 = Mathf.Sqrt(lat * lat + dt * dt);
                        if (dist3 >= want) continue;
                        Vector2 dir = lat > 1e-4f ? d / lat : new Vector2(Mathf.Cos(i + j), Mathf.Sin(i + j));
                        float overlap = (want - dist3) * .5f;
                        push[i] += dir * overlap;
                        push[j] -= dir * overlap;
                    }
                    if (data.Songs[i].TreeParent is int p && p >= 1 && p <= n)
                    {
                        Vector2 toParent = lateral[p - 1] - lateral[i];
                        float rest = 1.2f + radius[p - 1] + radius[i];
                        float m = toParent.magnitude;
                        if (m > rest) push[i] += toParent / m * (m - rest) * .1f;
                    }
                }
                for (int i = 0; i < n; i++) lateral[i] += push[i];
            }

            for (int i = 0; i < n; i++)
                positions[i] = frame.TimeDir * t[i] + frame.Lateral1 * lateral[i].x + frame.Lateral2 * lateral[i].y;
        }
    }
}
