#nullable enable
using System.Collections.Generic;
using UnityEngine;

namespace MusicHistory.Viewer
{
    /// <summary>
    /// The time axis: a line under the graph with a tick and a year label at every decade, and a
    /// faint ring around the graph at each decade so time can be read from any viewpoint. Years
    /// map to coordinates through the frame's fitted time map (layout_run / graph_meta give the
    /// range; the fit guarantees the ticks sit exactly where songs of that year are). Line widths
    /// follow the camera distance so the axis stays visible from far away.
    /// </summary>
    public sealed class TimelineAxis : MonoBehaviour
    {
        public int FirstYear;
        public int LastYear;
        public readonly List<int> DecadeYears = new();
        public readonly List<WorldLabel> YearLabels = new();

        readonly List<(LineRenderer line, float baseWidth, float pixelWidth)> lines = new();
        GraphFrame frame = null!;
        Vector3 midpoint;

        static readonly Color AxisColor = new(.62f, .66f, .72f, 1f);
        static readonly Color TickColor = new(.78f, .81f, .86f, 1f);
        static readonly Color RingColor = new(.10f, .11f, .13f, 1f);

        public static TimelineAxis Create(GraphFrame frame, double minTime, double maxTime, LabelLayer labels, Transform? parent)
        {
            GameObject go = new("Timeline Axis");
            if (parent != null) go.transform.SetParent(parent, false);
            TimelineAxis axis = go.AddComponent<TimelineAxis>();
            axis.Build(frame, minTime, maxTime, labels);
            return axis;
        }

        void Build(GraphFrame graphFrame, double minTime, double maxTime, LabelLayer labels)
        {
            frame = graphFrame;
            FirstYear = (int)System.Math.Floor(minTime / 10.0) * 10;
            LastYear = (int)System.Math.Ceiling((maxTime + .001) / 10.0) * 10;
            float margin = Mathf.Max(2f, frame.LateralRadius * .08f);
            float below = frame.LateralCenter.x - frame.LateralRadius - margin;
            float depth = frame.LateralCenter.y;
            float ringRadius = frame.LateralRadius + margin * .5f;

            Vector3 axisStart = frame.Point(FirstYear, below, depth);
            Vector3 axisEnd = frame.Point(LastYear, below, depth);
            midpoint = (axisStart + axisEnd) * .5f;
            AddLine("Axis", new[] { axisStart, axisEnd }, AxisColor, .12f, 2.2f, false);

            float tickLength = margin * .45f;
            // A vertical time axis stands left of the graph: its year labels go left of the ticks.
            bool vertical = Mathf.Abs(Vector3.Dot(frame.TimeDir, Vector3.up)) > .9f;
            for (int year = FirstYear; year <= LastYear; year += 10)
            {
                DecadeYears.Add(year);
                Vector3 p = frame.Point(year, below, depth);
                AddLine($"Tick {year}", new[] { p - frame.Lateral1 * (tickLength * .5f), p + frame.Lateral1 * tickLength }, TickColor, .1f, 1.8f, false);
                if (year < LastYear || maxTime >= year)
                {
                    WorldLabel label = labels.Create($"<b>{year}</b>", null, p - frame.Lateral1 * (tickLength * .6f), 0f,
                        vertical ? LabelPlacement.Left : LabelPlacement.Below, 1.05f);
                    label.Priority = 1e9f;
                    YearLabels.Add(label);
                }
                AddRing(year, ringRadius);
                // Five-year minor ticks.
                if (year + 5 < LastYear)
                {
                    Vector3 q = frame.Point(year + 5, below, depth);
                    AddLine($"Tick {year + 5}", new[] { q, q + frame.Lateral1 * (tickLength * .5f) }, AxisColor, .06f, 1.2f, false);
                }
            }
        }

        void AddRing(int year, float radius)
        {
            const int segments = 72;
            Vector3[] points = new Vector3[segments];
            Vector3 center = frame.Point(year, frame.LateralCenter.x, frame.LateralCenter.y);
            for (int i = 0; i < segments; i++)
            {
                float a = i * Mathf.PI * 2f / segments;
                points[i] = center + (frame.Lateral1 * Mathf.Cos(a) + frame.Lateral2 * Mathf.Sin(a)) * radius;
            }
            AddLine($"Decade Ring {year}", points, RingColor, .06f, 1.0f, true, additive: true);
        }

        void AddLine(string lineName, Vector3[] points, Color color, float baseWidth, float pixelWidth, bool loop, bool additive = false)
        {
            GameObject go = new(lineName);
            go.transform.SetParent(transform, false);
            LineRenderer line = go.AddComponent<LineRenderer>();
            line.useWorldSpace = true;
            line.loop = loop;
            line.positionCount = points.Length;
            line.SetPositions(points);
            line.widthMultiplier = baseWidth;
            line.numCapVertices = loop ? 0 : 2;
            line.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            line.receiveShadows = false;
            line.lightProbeUsage = UnityEngine.Rendering.LightProbeUsage.Off;
            line.reflectionProbeUsage = UnityEngine.Rendering.ReflectionProbeUsage.Off;
            line.sharedMaterial = GraphMaterials.Line(color, 0f, additive);
            lines.Add((line, baseWidth, pixelWidth));
        }

        /// <summary>World position of a year on the axis line (for tests and camera framing).</summary>
        public Vector3 AxisPoint(double year)
        {
            float margin = Mathf.Max(2f, frame.LateralRadius * .08f);
            return frame.Point(year, frame.LateralCenter.x - frame.LateralRadius - margin, frame.LateralCenter.y);
        }

        void LateUpdate() => Refresh(Camera.main);

        /// <summary>Keeps lines at least <c>pixelWidth</c> pixels wide at the current camera distance.</summary>
        public void Refresh(Camera? cam)
        {
            if (cam == null) return;
            float distance = Vector3.Distance(cam.transform.position, midpoint);
            float worldPerPixel = cam.orthographic
                ? cam.orthographicSize * 2f / Mathf.Max(1, cam.pixelHeight)
                : 2f * distance * Mathf.Tan(cam.fieldOfView * .5f * Mathf.Deg2Rad) / Mathf.Max(1, cam.pixelHeight);
            foreach ((LineRenderer line, float baseWidth, float pixelWidth) in lines)
                line.widthMultiplier = Mathf.Max(baseWidth, worldPerPixel * pixelWidth);
        }
    }
}
