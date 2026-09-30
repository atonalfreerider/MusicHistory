#nullable enable
using System;
using MusicHistory.Playback;

namespace MusicHistory.Audio
{
    /// <summary>
    /// The tempo side of a handoff, measured from what actually sounds (docs/DESIGN.md §11:
    /// each song starts in the BPM of the song played before it).
    ///
    /// The previous clip's tempo is what the listener heard over the last bar it played: its
    /// tempo map times its morph (or its fixed BPM), not the file's median. The next clip's
    /// start ratio is taken against its own mean tempo over the first bar of its excerpt, so
    /// that bar starts at the heard BPM even when the excerpt sits in a section faster or
    /// slower than the file's median (<see cref="Morph.Plan(SongClip, SongClip, double, double, double)"/>).
    /// </summary>
    public static class HandoffTempo
    {
        /// <summary>
        /// Mean BPM of the last bar <paramref name="job"/> played before <paramref name="reachedBeat"/>
        /// (clamped to the excerpt; a clip cut short counts up to where it was cut). 0 when it
        /// played nothing yet.
        /// </summary>
        public static double HeardBpm(PlaybackJob job, double reachedBeat)
        {
            if (job == null) throw new ArgumentNullException(nameof(job));
            double end = Math.Min(job.EndBeat, reachedBeat);
            double start = Math.Max(job.StartBeat, end - job.BeatsPerBar);
            if (!(end - start > 1e-6)) return 0;
            return job.MeanPlayedBpm(start, end);
        }

        /// <summary>The file's own mean BPM over the first bar of an excerpt starting at <paramref name="startBeat"/>.</summary>
        public static double EntryFileBpm(TempoMap tempo, double startBeat, double beatsPerBar)
        {
            if (tempo == null) throw new ArgumentNullException(nameof(tempo));
            double bar = beatsPerBar > 0 ? beatsPerBar : 4;
            return tempo.MeanBpm(startBeat, startBeat + bar);
        }

        /// <summary>
        /// The plan a clip plays with after <paramref name="previous"/>: the key from the previous
        /// excerpt's exit key, the tempo from <paramref name="heardBpm"/> (≤ 0: the previous
        /// clip's median BPM) against the next file's first-bar tempo in <paramref name="nextTempo"/>.
        /// </summary>
        public static MorphPlan Plan(SongClip? previous, SongClip next, double morphBars, double heardBpm, TempoMap nextTempo)
        {
            if (previous == null || next == null || morphBars <= 0) return MorphPlan.None;
            double heard = heardBpm > 0 ? heardBpm : previous.NativeBpm;
            return Morph.Plan(previous, next, morphBars, heard, EntryFileBpm(nextTempo, next.ExcerptStartBeat, next.BeatsPerBar));
        }
    }
}
