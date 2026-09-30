#nullable enable
using System;
using System.Collections.Generic;

namespace MusicHistory.Audio
{
    /// <summary>
    /// Piecewise-constant tempo of a MIDI file on the beat axis: segment i starts at
    /// <c>Beats[i]</c> and lasts until the next change. Beat 0 always has a tempo (120 BPM when
    /// the file sets none before its first change), so every beat maps to a tempo.
    /// </summary>
    public sealed class TempoMap
    {
        public const double DefaultMicrosecondsPerQuarter = 500000;

        readonly double[] beats;
        readonly double[] secondsPerBeat;
        readonly double[] secondsAtStart;

        public TempoMap(IEnumerable<KeyValuePair<double, double>> changes)
        {
            // Sort by beat; the last change at a given beat wins (files often repeat tempo events).
            var sorted = new List<KeyValuePair<double, double>>();
            foreach (var c in changes)
                if (c.Value > 0 && !double.IsNaN(c.Key) && !double.IsInfinity(c.Value))
                    sorted.Add(new KeyValuePair<double, double>(Math.Max(0, c.Key), c.Value));
            sorted.Sort((a, b) => a.Key.CompareTo(b.Key));
            var b0 = new List<double>();
            var spb = new List<double>();
            foreach (var c in sorted)
            {
                double s = c.Value / 1e6;
                if (b0.Count > 0 && Math.Abs(b0[b0.Count - 1] - c.Key) < 1e-9) { spb[spb.Count - 1] = s; continue; }
                if (b0.Count == 0 && c.Key > 0) { b0.Add(0); spb.Add(DefaultMicrosecondsPerQuarter / 1e6); }
                if (spb.Count > 0 && spb[spb.Count - 1] == s) continue;   // no change
                b0.Add(c.Key);
                spb.Add(s);
            }
            if (b0.Count == 0) { b0.Add(0); spb.Add(DefaultMicrosecondsPerQuarter / 1e6); }
            beats = b0.ToArray();
            secondsPerBeat = spb.ToArray();
            secondsAtStart = new double[beats.Length];
            for (int i = 1; i < beats.Length; i++)
                secondsAtStart[i] = secondsAtStart[i - 1] + (beats[i] - beats[i - 1]) * secondsPerBeat[i - 1];
        }

        public static TempoMap Constant(double bpm) =>
            new TempoMap(new[] { new KeyValuePair<double, double>(0, 60e6 / (bpm > 0 ? bpm : 120)) });

        public int Count => beats.Length;
        public double SegmentStart(int i) => beats[i];

        /// <summary>Index of the segment containing <paramref name="beat"/> (binary search).</summary>
        public int SegmentAt(double beat)
        {
            int lo = 0, hi = beats.Length - 1;
            if (beat >= beats[hi]) return hi;
            while (lo < hi)
            {
                int mid = (lo + hi + 1) >> 1;
                if (beats[mid] <= beat) lo = mid; else hi = mid - 1;
            }
            return lo;
        }

        public double SecondsPerBeatAt(double beat) => secondsPerBeat[SegmentAt(beat)];
        public double BpmAt(double beat) => 60.0 / SecondsPerBeatAt(beat);

        /// <summary>First tempo change strictly after <paramref name="beat"/> (+inf when none).</summary>
        public double NextChangeAfter(double beat)
        {
            int i = SegmentAt(beat) + 1;
            return i < beats.Length ? beats[i] : double.PositiveInfinity;
        }

        /// <summary>File time (no morph) at <paramref name="beat"/>.</summary>
        public double SecondsAt(double beat)
        {
            int i = SegmentAt(beat);
            return secondsAtStart[i] + (beat - beats[i]) * secondsPerBeat[i];
        }

        public double BeatAt(double seconds)
        {
            int lo = 0, hi = beats.Length - 1;
            while (lo < hi)
            {
                int mid = (lo + hi + 1) >> 1;
                if (secondsAtStart[mid] <= seconds) lo = mid; else hi = mid - 1;
            }
            return beats[lo] + (seconds - secondsAtStart[lo]) / secondsPerBeat[lo];
        }

        /// <summary>
        /// Mean BPM over [from, to): beats divided by the file time they take. This is the pulse a
        /// listener hears across the span (a bar), unlike the tempo of one segment at a point.
        /// </summary>
        public double MeanBpm(double from, double to)
        {
            if (!(to > from)) return BpmAt(from);
            double seconds = SecondsAt(to) - SecondsAt(from);
            return seconds > 0 ? (to - from) * 60.0 / seconds : BpmAt(from);
        }

        /// <summary>Beat-weighted median BPM over [from, to) (tempo segments below 20 or above 400 BPM ignored).</summary>
        public double MedianBpm(double from, double to)
        {
            var parts = new List<KeyValuePair<double, double>>();
            double total = 0;
            for (int i = 0; i < beats.Length; i++)
            {
                double a = Math.Max(from, beats[i]);
                double b = Math.Min(to, i + 1 < beats.Length ? beats[i + 1] : to);
                double bpm = 60.0 / secondsPerBeat[i];
                if (b <= a || bpm < 20 || bpm > 400) continue;
                parts.Add(new KeyValuePair<double, double>(bpm, b - a));
                total += b - a;
            }
            if (total <= 0) return BpmAt(from);
            parts.Sort((x, y) => x.Key.CompareTo(y.Key));
            double acc = 0;
            foreach (var p in parts)
            {
                acc += p.Value;
                if (acc >= total / 2) return p.Key;
            }
            return parts[parts.Count - 1].Key;
        }
    }
}
