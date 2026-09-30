#nullable enable
using System;
using MusicHistory.Playback;

namespace MusicHistory.Audio
{
    /// <summary>
    /// Everything a deck needs to play one clip, prepared off the audio thread and handed over
    /// by reference: the excerpt events, the file's tempo map and the key/BPM morph.
    ///
    /// Time is a function of beats: the playback rate at beat b is
    /// <c>ratio(b) / secondsPerBeat_file(b)</c> beats per second, with ratio from
    /// <see cref="MorphPlan.TempoRatio"/>. <see cref="Advance"/> integrates it (midpoint rule,
    /// exact across tempo-map changes), so onsets follow the tempo integral and never drift.
    /// </summary>
    public sealed class PlaybackJob
    {
        public readonly long Id;
        public readonly ExcerptEvents Excerpt;
        public readonly TempoMap Tempo;
        public readonly MorphPlan Plan;
        public readonly double BeatsPerBar;
        /// <summary>Extra constant transposition (apples-to-apples fallback when no normalized file exists).</summary>
        public readonly double ConstantSemitones;
        /// <summary>When &gt; 0, replaces the file's tempo map with this BPM.</summary>
        public readonly double FixedBpm;
        /// <summary>Start on the handoff grid (bar lines) of a clip that just ended naturally.</summary>
        public readonly bool AlignToHandoff;
        /// <summary>Owner's tag (the SongClip); never touched by the engine.</summary>
        public readonly object? Tag;

        public PlaybackJob(long id, ExcerptEvents excerpt, TempoMap tempo, MorphPlan plan, double beatsPerBar,
                           bool alignToHandoff, double constantSemitones = 0, double fixedBpm = 0, object? tag = null)
        {
            Id = id;
            Excerpt = excerpt;
            Tempo = tempo;
            Plan = plan;
            BeatsPerBar = beatsPerBar > 0 ? beatsPerBar : 4;
            AlignToHandoff = alignToHandoff;
            ConstantSemitones = constantSemitones;
            FixedBpm = fixedBpm;
            Tag = tag;
        }

        public double StartBeat => Excerpt.StartBeat;
        public double EndBeat => Excerpt.EndBeat;

        double Ratio(double beat) => Plan.TempoRatio(beat - Excerpt.StartBeat);
        double SecondsPerBeat(double beat) => FixedBpm > 0 ? 60.0 / FixedBpm : Tempo.SecondsPerBeatAt(beat);
        double NextChange(double beat) => FixedBpm > 0 ? double.PositiveInfinity : Tempo.NextChangeAfter(beat);

        /// <summary>File BPM (or the fixed BPM) at <paramref name="beat"/>, before the morph.</summary>
        public double FileBpmAt(double beat) => 60.0 / SecondsPerBeat(beat);

        /// <summary>Effective quarter-note BPM being played at <paramref name="beat"/>.</summary>
        public double BpmAt(double beat) => FileBpmAt(beat) * Ratio(beat);

        /// <summary>Beats per second at <paramref name="beat"/>.</summary>
        public double RateAt(double beat) => Ratio(beat) / SecondsPerBeat(beat);

        /// <summary>Transposition relative to the file at <paramref name="beat"/>.</summary>
        public double SemitonesAt(double beat) => ConstantSemitones + Plan.Semitones(beat - Excerpt.StartBeat);

        /// <summary>
        /// The file's own mean BPM over the excerpt's first bar (the fixed BPM when set): what
        /// <see cref="MorphPlan.StartTempoRatio"/> is relative to under
        /// <see cref="Morph.Plan(SongClip, SongClip, double, double, double)"/>.
        /// </summary>
        public double EntryFileBpm => FixedBpm > 0 ? FixedBpm : Tempo.MeanBpm(StartBeat, StartBeat + BeatsPerBar);

        /// <summary>Tempo the clip starts at: its entry tempo times the plan's start ratio.</summary>
        public double StartBpm => EntryFileBpm * Plan.StartTempoRatio;

        /// <summary>
        /// Wall time (musical seconds) to play from <paramref name="from"/> to <paramref name="to"/>:
        /// the integral of secondsPerBeat(b) / ratio(b), exact per tempo segment outside the morph
        /// and by Simpson's rule inside it. Allocation-free (the engine calls it on the audio thread).
        /// </summary>
        public double SecondsBetween(double from, double to)
        {
            if (!(to > from)) return 0;
            double total = 0, b = from;
            for (int guard = 0; b < to && guard < 1_000_000; guard++)
            {
                double segEnd = Math.Min(to, NextChange(b));
                if (!(segEnd > b)) break;
                total += SecondsPerBeat(b) * InverseRatioIntegral(b, segEnd);
                b = segEnd;
            }
            return total;
        }

        /// <summary>Mean BPM actually played over [from, to) (tempo map x morph, or the fixed BPM).</summary>
        public double MeanPlayedBpm(double from, double to)
        {
            if (!(to > from)) return BpmAt(from);
            double seconds = SecondsBetween(from, to);
            return seconds > 0 ? (to - from) * 60.0 / seconds : BpmAt(from);
        }

        /// <summary>∫ 1 / ratio(b) db over [a, c].</summary>
        double InverseRatioIntegral(double a, double c)
        {
            double morphEnd = Excerpt.StartBeat + Plan.MorphBeats;
            if (Plan.MorphBeats <= 0 || Plan.StartTempoRatio == 1 || a >= morphEnd) return c - a;
            double total = 0;
            if (c > morphEnd)
            {
                total += c - morphEnd;
                c = morphEnd;
            }
            const int n = 32;
            double h = (c - a) / n, s = 0;
            for (int i = 0; i <= n; i++)
            {
                double f = 1.0 / Ratio(a + i * h);
                s += f * (i == 0 || i == n ? 1 : (i & 1) == 1 ? 4 : 2);
            }
            return total + s * h / 3;
        }

        /// <summary>The beat reached after playing <paramref name="seconds"/> from <paramref name="beat"/>.</summary>
        public double Advance(double beat, double seconds)
        {
            double remaining = seconds;
            for (int guard = 0; remaining > 0 && guard < 1_000_000; guard++)
            {
                double segEnd = NextChange(beat);
                double spb = SecondsPerBeat(beat);
                double r1 = Ratio(beat) / spb;
                double mid = Math.Min(beat + 0.5 * r1 * remaining, segEnd);
                double next = beat + Ratio(mid) / spb * remaining;
                if (next < segEnd) return next;
                // The step crosses a tempo change: spend the time needed to reach it, then continue.
                double rc = Ratio(0.5 * (beat + segEnd)) / spb;
                double toChange = (segEnd - beat) / rc;
                if (toChange >= remaining) return segEnd;
                remaining -= toChange;
                beat = segEnd;
            }
            return beat;
        }
    }
}
