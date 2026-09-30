#nullable enable
using System;
using UnityEngine;

namespace MusicHistory.Playback
{
    /// <summary>
    /// A silent <see cref="ISongPlayer"/>: advances <see cref="CurrentBeat"/> through the excerpt
    /// with the same key/BPM morph as the real player (<see cref="Morph.Plan"/>, smoothstep over
    /// <see cref="MorphBars"/> bars) and raises <see cref="Finished"/> at the excerpt end. The
    /// walkthrough uses it when no synth is installed or a song's MIDI file is missing, so tours,
    /// timing and the HUD behave exactly as with sound.
    /// </summary>
    public sealed class SilentSongPlayer : MonoBehaviour, ISongPlayer, IMorphReadout
    {
        [Tooltip("BPM of the normalized files (graph_meta target_bpm).")]
        public double TargetBpm = 120;

        SongClip? clip;
        MorphPlan plan = MorphPlan.None;
        bool playing;
        bool startedRaised;

        public SongClip? Clip => clip;
        public MorphPlan Plan => plan;
        /// <summary>The silent clock has no tempo map: its plan is the median-based <see cref="Morph.Plan(SongClip, SongClip, double)"/>.</summary>
        public MorphPlan CurrentPlan => plan;

        public double PlanStartBpm
        {
            get
            {
                if (clip == null) return 0;
                if (ApplesToApples) return TargetBpm;
                return (clip.NativeBpm > 0 ? clip.NativeBpm : 120) * plan.StartTempoRatio;
            }
        }
        public bool Paused { get; set; }
        public bool IsPlaying => playing;
        public double CurrentBeat { get; private set; }
        public float MorphBars { get; set; } = 4f;
        public bool ApplesToApples { get; set; }

        public double CurrentSemitones
        {
            get
            {
                if (clip == null) return 0;
                return ApplesToApples ? clip.NormShift : plan.Semitones(CurrentBeat - clip.ExcerptStartBeat);
            }
        }

        public double CurrentBpm
        {
            get
            {
                if (clip == null) return 0;
                if (ApplesToApples) return TargetBpm;
                double native = clip.NativeBpm > 0 ? clip.NativeBpm : 120;
                return native * plan.TempoRatio(CurrentBeat - clip.ExcerptStartBeat);
            }
        }

        public event Action<SongClip>? Started;
        public event Action<SongClip>? Finished;

        public void Play(SongClip next, SongClip? previous)
        {
            clip = next ?? throw new ArgumentNullException(nameof(next));
            plan = ApplesToApples ? MorphPlan.None : Morph.Plan(previous!, next, MorphBars);
            CurrentBeat = next.ExcerptStartBeat;
            Paused = false;
            playing = next.ExcerptEndBeat > next.ExcerptStartBeat;
            startedRaised = false;
            if (!playing) Finished?.Invoke(next);
        }

        public void Stop()
        {
            playing = false;
            Paused = false;
            plan = MorphPlan.None;
        }

        void Update() => Advance(Time.deltaTime);

        /// <summary>Moves the clock forward by <paramref name="seconds"/> of wall time (public for tests).</summary>
        public void Advance(double seconds)
        {
            if (!playing || Paused || clip == null) return;
            if (!startedRaised)
            {
                startedRaised = true;
                Started?.Invoke(clip);
            }
            // Integrate beats = ∫ bpm/60 dt in small substeps so the tempo glide is followed closely.
            double remaining = Math.Max(0, seconds);
            while (remaining > 0 && playing)
            {
                double dt = Math.Min(remaining, 0.02);
                remaining -= dt;
                CurrentBeat += dt * CurrentBpm / 60.0;
                if (CurrentBeat >= clip.ExcerptEndBeat)
                {
                    CurrentBeat = clip.ExcerptEndBeat;
                    playing = false;
                    Finished?.Invoke(clip);
                }
            }
        }

        /// <summary>
        /// Wall-clock length of the excerpt under <paramref name="plan"/>: t = ∫ 60 / (N·r(b)) db,
        /// integrated numerically (tests compare the simulated clock to this).
        /// </summary>
        public static double ExpectedSeconds(SongClip clip, MorphPlan plan, bool applesToApples = false, double targetBpm = 120)
        {
            double length = clip.ExcerptEndBeat - clip.ExcerptStartBeat;
            if (length <= 0) return 0;
            if (applesToApples) return length * 60.0 / targetBpm;
            double native = clip.NativeBpm > 0 ? clip.NativeBpm : 120;
            const int steps = 20000;
            double h = length / steps, total = 0;
            for (int i = 0; i < steps; i++)
            {
                double b = (i + .5) * h;
                total += 60.0 / (native * plan.TempoRatio(b)) * h;
            }
            return total;
        }
    }
}
