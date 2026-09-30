using System;

namespace MusicHistory.Playback
{
    /// <summary>
    /// A song as the walkthrough plays it: one excerpt of a MIDI file plus the facts the
    /// key/BPM morph needs. Filled by the graph loader from song_node (docs/DESIGN.md §10).
    /// </summary>
    [Serializable]
    public sealed class SongClip
    {
        public int NodeId;
        public string Title;
        public string Artist;
        /// <summary>Absolute path to the sanitized MIDI in its native key and tempo.</summary>
        public string MidiPath;
        /// <summary>Absolute path to the MIDI transposed to C major / A minor at 120 BPM (may be null).</summary>
        public string NormalizedMidiPath;
        /// <summary>Excerpt bounds in the MIDI's own quarter-note beats, bar aligned.</summary>
        public double ExcerptStartBeat;
        public double ExcerptEndBeat;
        /// <summary>Native home tonic, pitch class with C = 0.</summary>
        public int TonicPc;
        public bool Minor;
        /// <summary>Beat-weighted median quarter-note BPM of the file.</summary>
        public double NativeBpm;
        /// <summary>Quarter-note beats per bar of the dominant meter.</summary>
        public double BeatsPerBar;
        public double FirstDownbeat;
        /// <summary>Semitones that take the native key to the target key (already applied in the normalized file).</summary>
        public int NormShift;
    }

    /// <summary>How a clip enters: it starts transposed and re-tempoed, then glides to native.</summary>
    public readonly struct MorphPlan
    {
        /// <summary>Transposition at the first beat, in semitones relative to native, in [-6, 5].</summary>
        public readonly double StartSemitones;
        /// <summary>Playback tempo divided by the file's own tempo at the first beat.</summary>
        public readonly double StartTempoRatio;
        /// <summary>Length of the glide in quarter-note beats (0 = no morph).</summary>
        public readonly double MorphBeats;

        public MorphPlan(double startSemitones, double startTempoRatio, double morphBeats)
        {
            StartSemitones = startSemitones;
            StartTempoRatio = startTempoRatio;
            MorphBeats = morphBeats;
        }

        public static readonly MorphPlan None = new MorphPlan(0, 1, 0);

        /// <summary>Smoothstep progress 0..1 at <paramref name="beatsIntoExcerpt"/>.</summary>
        public double Progress(double beatsIntoExcerpt)
        {
            if (MorphBeats <= 0) return 1;
            double u = Math.Max(0, Math.Min(1, beatsIntoExcerpt / MorphBeats));
            return u * u * (3 - 2 * u);
        }

        /// <summary>Transposition in semitones at a point of the excerpt.</summary>
        public double Semitones(double beatsIntoExcerpt) => StartSemitones * (1 - Progress(beatsIntoExcerpt));

        /// <summary>Tempo ratio at a point of the excerpt.</summary>
        public double TempoRatio(double beatsIntoExcerpt)
        {
            double s = Progress(beatsIntoExcerpt);
            return StartTempoRatio + (1 - StartTempoRatio) * s;
        }
    }

    public static class Morph
    {
        /// <summary>Wraps a semitone difference into [-6, 5].</summary>
        public static int Wrap(int semitones) => ((semitones % 12) + 12 + 6) % 12 - 6;

        /// <summary>
        /// Tempos further apart than this many octaves are treated as half/double time of
        /// each other (70 -> 140 is not a ramp); closer tempos are ramped literally
        /// (125 -> 87 starts at 125).
        /// </summary>
        public const double FoldOctaves = 0.8;

        /// <summary>
        /// The song starts in the key and BPM of the song played before it and glides to its
        /// own over <paramref name="morphBars"/> bars. The previous BPM is used as is unless it
        /// is more than <see cref="FoldOctaves"/> away from the native BPM; then it is halved or
        /// doubled (x1/2, x2) to whichever is closest, so the pulse carries over.
        /// </summary>
        public static MorphPlan Plan(SongClip previous, SongClip next, double morphBars)
        {
            if (previous == null || next == null || morphBars <= 0) return MorphPlan.None;
            int semis = Wrap(previous.TonicPc - next.TonicPc);
            double n = next.NativeBpm > 0 ? next.NativeBpm : 120;
            double p = previous.NativeBpm > 0 ? previous.NativeBpm : n;
            double best = p;
            if (Math.Abs(Math.Log(p / n, 2)) > FoldOctaves)
                foreach (double candidate in new[] { p / 2, p * 2, p / 4, p * 4 })
                    if (Math.Abs(Math.Log(candidate / n)) < Math.Abs(Math.Log(best / n))) best = candidate;
            double beatsPerBar = next.BeatsPerBar > 0 ? next.BeatsPerBar : 4;
            return new MorphPlan(semis, best / n, morphBars * beatsPerBar);
        }
    }

    /// <summary>
    /// Plays SongClips for the walkthrough. The real implementation is
    /// MusicHistory.Audio.SongPlayer (MeltySynth); a silent timer-based fallback keeps the
    /// walkthrough usable without a SoundFont.
    /// </summary>
    public interface ISongPlayer
    {
        /// <summary>Start <paramref name="clip"/>. <paramref name="previous"/> is the clip played just before (null = play natively).</summary>
        void Play(SongClip clip, SongClip previous);
        void Stop();
        bool Paused { get; set; }
        bool IsPlaying { get; }
        /// <summary>Position in the clip's own quarter-note beats.</summary>
        double CurrentBeat { get; }
        /// <summary>Transposition relative to native, in semitones, right now.</summary>
        double CurrentSemitones { get; }
        /// <summary>Effective quarter-note BPM right now.</summary>
        double CurrentBpm { get; }
        /// <summary>Glide length in bars (default 4).</summary>
        float MorphBars { get; set; }
        /// <summary>Play the normalized file (C major / A minor, 120 BPM) without morphing.</summary>
        bool ApplesToApples { get; set; }
        /// <summary>Raised on the main thread when a clip starts sounding.</summary>
        event Action<SongClip> Started;
        /// <summary>Raised on the main thread when the excerpt end is reached.</summary>
        event Action<SongClip> Finished;
    }
}
