#nullable enable
using System;
using System.Collections.Concurrent;
using System.Threading;
using MeltySynth;

namespace MusicHistory.Audio
{
    public enum HandoffGrid
    {
        /// <summary>The next clip starts as soon as it is ready.</summary>
        Immediate,
        /// <summary>...on the next beat of the finished clip's continued pulse.</summary>
        Beat,
        /// <summary>...on the next bar line of the finished clip's continued pulse.</summary>
        Bar,
    }

    public sealed class EngineSettings
    {
        /// <summary>How long a clip that ended naturally keeps ringing (release, reverb, pedal).</summary>
        public double TailSeconds = 2.0;
        /// <summary>The last part of that tail fades to silence.</summary>
        public double TailFadeSeconds = 0.5;
        /// <summary>Tail of a clip cut short by Stop or by a new Play.</summary>
        public double InterruptSeconds = 0.35;
        /// <summary>Fade used when a deck still ringing must be reused at once.</summary>
        public double RecycleSeconds = 0.012;
        /// <summary>
        /// Pause fades the playing deck to silence over this long, then cuts its voices (so notes
        /// held by the sustain pedal stop too); resume re-strikes the held keys and fades back in.
        /// </summary>
        public double PauseFadeSeconds = 0.01;
        public HandoffGrid Handoff = HandoffGrid.Bar;
        /// <summary>Align to the grid only when the next clip arrives within this many bars of the end.</summary>
        public double HandoffWindowBars = 4;
        /// <summary>
        /// A clip that arrives at most this late after a grid line joins at once, already that far
        /// into its first beat (the pulse stays continuous; only its first onsets are late).
        /// Covers the main-thread round trip (Finished -> director -> Play), typically 20-60 ms.
        /// </summary>
        public double LateJoinSeconds = 0.12;
        public float MasterGain = 1f;
    }

    /// <summary>Test/diagnostic hooks, called on the audio thread (keep them allocation-free).</summary>
    public interface IEngineObserver
    {
        void OnStarted(int deck, PlaybackJob job, long sample);
        void OnDispatch(int deck, PlaybackJob job, long sample, in MidiMessage message);
        void OnFinished(int deck, PlaybackJob job, double exactEndSample);
    }

    /// <summary>What the main thread may read about playback (a consistent-enough snapshot).</summary>
    public readonly struct EngineState
    {
        public readonly long ActiveJobId, StartedJobId, FinishedJobId;
        public readonly double Beat, Semitones, Bpm;
        public readonly long SampleClock;

        public EngineState(long active, long started, long finished, double beat, double semitones, double bpm, long clock)
        {
            ActiveJobId = active;
            StartedJobId = started;
            FinishedJobId = finished;
            Beat = beat;
            Semitones = semitones;
            Bpm = bpm;
            SampleClock = clock;
        }
    }

    /// <summary>
    /// Two-deck, beat-domain MIDI sequencer rendered in 64-frame blocks.
    ///
    /// Each deck owns a synth. A clip plays on one deck; when it ends (note-offs at the
    /// excerpt end) that deck keeps ringing for <see cref="EngineSettings.TailSeconds"/> while
    /// the next clip starts on the other deck, on the next bar line of the finished clip's
    /// continued pulse, so the handoff keeps the groove. Per block: the beat advance comes from
    /// the tempo integral (<see cref="PlaybackJob.Advance"/>), events are dispatched in the
    /// block whose start is nearest their time (error ≤ half a block, 0.67 ms at 48 kHz), and
    /// the morph's transposition is set before rendering.
    ///
    /// Threading: <see cref="Post"/>, <see cref="PostStop"/>, <see cref="Paused"/> and
    /// <see cref="ReadState"/> may be used from any thread; the Render methods belong to the
    /// audio thread and do not allocate.
    /// </summary>
    public sealed class DeckEngine
    {
        public const int BlockSize = 64;

        enum DeckState { Idle, Scheduled, Playing, Tail }

        sealed class Deck
        {
            public readonly int Index;
            public readonly IDeckSynth Synth;
            public readonly float[] L = new float[BlockSize], R = new float[BlockSize];
            public readonly byte[] Sounding = new byte[16 * 128];
            public DeckState State;
            public PlaybackJob? Job;
            public long StartSample;
            public double Beat;
            public int NextEvent;
            /// <summary>Seconds of the clip already elapsed when it starts (late join).</summary>
            public double JoinSeconds;
            // Gain envelope: FadeFrom until FadeStart, then linear to 0 at FadeEnd.
            public double FadeFrom = 1;
            public long FadeStart = long.MaxValue, FadeEnd = long.MaxValue;
            // Pause envelope (multiplies the gain): PauseFrom until PauseStart, linear to PauseTo at PauseEnd.
            public double PauseFrom = 1, PauseTo = 1;
            public long PauseStart, PauseEnd;
            /// <summary>Voices are cut once the pause fade reaches silence.</summary>
            public bool CutWhenSilent;
            /// <summary>The voices were cut while paused: resume re-strikes the held keys.</summary>
            public bool Cut;

            public Deck(int index, IDeckSynth synth)
            {
                Index = index;
                Synth = synth;
            }

            public double GainAt(long sample)
            {
                if (sample <= FadeStart) return FadeFrom;
                if (sample >= FadeEnd) return 0;
                return FadeFrom * (1 - (double)(sample - FadeStart) / (FadeEnd - FadeStart));
            }

            public void FadeOut(long now, long length)
            {
                double g = GainAt(now);
                if (FadeEnd <= now + length && FadeStart <= now) return;   // already fading faster
                FadeFrom = g;
                FadeStart = now;
                FadeEnd = now + Math.Max(BlockSize, length);
            }

            public void ClearFade()
            {
                FadeFrom = 1;
                FadeStart = FadeEnd = long.MaxValue;
                ClearPause();
            }

            public double PauseGainAt(long sample)
            {
                if (sample >= PauseEnd) return PauseTo;
                if (sample <= PauseStart) return PauseFrom;
                return PauseFrom + (PauseTo - PauseFrom) * (sample - PauseStart) / (PauseEnd - PauseStart);
            }

            public void RampPause(long now, double to, long length)
            {
                PauseFrom = PauseGainAt(now);
                PauseTo = to;
                PauseStart = now;
                PauseEnd = now + Math.Max(1, length);
            }

            public void ClearPause()
            {
                PauseFrom = PauseTo = 1;
                PauseStart = PauseEnd = 0;
                CutWhenSilent = Cut = false;
            }
        }

        sealed class Command
        {
            public readonly PlaybackJob? Job;
            public Command(PlaybackJob? job) { Job = job; }
        }

        readonly Deck[] decks;
        readonly ConcurrentQueue<Command> commands = new ConcurrentQueue<Command>();
        readonly float[] mixL = new float[BlockSize], mixR = new float[BlockSize];
        readonly int rate;
        readonly double blockSeconds;
        double stepSeconds;   // musical seconds per block (block length x time scale)
        public readonly EngineSettings Settings;
        int served = BlockSize;
        long clock;
        volatile bool paused;
        bool wasPaused;
        double timeScale = 1;
        long latestRequest;

        Deck? active;
        bool gridValid;
        double gridOrigin, gridUnit, gridUntil;
        Deck? gridDeck;

        long pubActive, pubStarted, pubFinished, pubBeat, pubSemis, pubBpm, pubClock;

        public IEngineObserver? Observer;

        public DeckEngine(IDeckSynth a, IDeckSynth b, EngineSettings? settings = null)
        {
            if (a.SampleRate != b.SampleRate) throw new ArgumentException("both decks need the same sample rate");
            rate = a.SampleRate;
            blockSeconds = (double)BlockSize / rate;
            stepSeconds = blockSeconds;
            decks = new[] { new Deck(0, a), new Deck(1, b) };
            Settings = settings ?? new EngineSettings();
        }

        /// <summary>Two MeltySynth decks sharing one SoundFont (MeltySynth never mutates it).</summary>
        public static DeckEngine CreateMelty(SoundFont soundFont, int sampleRate, EngineSettings? settings = null, bool reverbAndChorus = true) =>
            new DeckEngine(new MeltyDeckSynth(soundFont, sampleRate, reverbAndChorus), new MeltyDeckSynth(soundFont, sampleRate, reverbAndChorus), settings);

        public static DeckEngine CreateSine(int sampleRate, EngineSettings? settings = null) =>
            new DeckEngine(new SineDeckSynth(sampleRate), new SineDeckSynth(sampleRate), settings);

        public int SampleRate => rate;
        public IDeckSynth DeckSynth(int index) => decks[index].Synth;

        /// <summary>Jobs with a smaller id than this are stale and dropped (set before posting).</summary>
        public long LatestRequestId
        {
            get => Interlocked.Read(ref latestRequest);
            set => Interlocked.Exchange(ref latestRequest, value);
        }

        public bool Paused
        {
            get => paused;
            set => paused = value;
        }

        /// <summary>
        /// Multiplies the musical clock (the walkthrough's time scale): 2 plays twice as fast at
        /// the same pitch, 0 pauses. Reported BPM stays the musical one.
        /// </summary>
        public double TimeScale
        {
            get => Volatile.Read(ref timeScale);
            set => Volatile.Write(ref timeScale, double.IsNaN(value) ? 1 : Math.Max(0, Math.Min(100, value)));
        }

        public void Post(PlaybackJob job) => commands.Enqueue(new Command(job ?? throw new ArgumentNullException(nameof(job))));
        public void PostStop() => commands.Enqueue(new Command(null));

        public EngineState ReadState() => new EngineState(
            Interlocked.Read(ref pubActive), Interlocked.Read(ref pubStarted), Interlocked.Read(ref pubFinished),
            BitConverter.Int64BitsToDouble(Interlocked.Read(ref pubBeat)),
            BitConverter.Int64BitsToDouble(Interlocked.Read(ref pubSemis)),
            BitConverter.Int64BitsToDouble(Interlocked.Read(ref pubBpm)),
            Interlocked.Read(ref pubClock));

        // ------------------------------------------------------------------ audio thread

        /// <summary>Fills an interleaved buffer (as OnAudioFilterRead gets it), overwriting it.</summary>
        public void Render(float[] data, int channels)
        {
            if (channels <= 0) return;
            int frames = data.Length / channels;
            int i = 0;
            while (i < frames)
            {
                if (served == BlockSize) RenderBlock();
                int n = Math.Min(BlockSize - served, frames - i);
                for (int k = 0; k < n; k++)
                {
                    float l = mixL[served + k], r = mixR[served + k];
                    int o = (i + k) * channels;
                    if (channels == 1) data[o] = 0.5f * (l + r);
                    else
                    {
                        data[o] = l;
                        data[o + 1] = r;
                        for (int c = 2; c < channels; c++) data[o + c] = 0;
                    }
                }
                served += n;
                i += n;
            }
        }

        /// <summary>Renders <paramref name="frames"/> stereo frames into separate buffers.</summary>
        public void RenderStereo(float[] left, float[] right, int offset, int frames)
        {
            int i = 0;
            while (i < frames)
            {
                if (served == BlockSize) RenderBlock();
                int n = Math.Min(BlockSize - served, frames - i);
                Array.Copy(mixL, served, left, offset + i, n);
                Array.Copy(mixR, served, right, offset + i, n);
                served += n;
                i += n;
            }
        }

        void RenderBlock()
        {
            double scale = Volatile.Read(ref timeScale);
            stepSeconds = blockSeconds * scale;
            while (commands.TryDequeue(out Command? cmd))
            {
                if (cmd.Job == null) StopActive();
                else Accept(cmd.Job);
            }
            bool p = paused || scale <= 0;
            if (p != wasPaused)
            {
                PauseChanged(p);
                wasPaused = p;
            }
            Array.Clear(mixL, 0, BlockSize);
            Array.Clear(mixR, 0, BlockSize);
            for (int d = 0; d < decks.Length; d++) Process(decks[d], p);
            float gain = Settings.MasterGain;
            for (int k = 0; k < BlockSize; k++)
            {
                mixL[k] = Clamp(mixL[k] * gain);
                mixR[k] = Clamp(mixR[k] * gain);
            }
            clock += BlockSize;
            served = 0;
            Publish();
        }

        static float Clamp(float x) => x > 1f ? 1f : x < -1f ? -1f : x;

        void Process(Deck d, bool isPaused)
        {
            if (d.State == DeckState.Idle) return;
            if (d.State == DeckState.Scheduled)
            {
                bool fading = clock < d.FadeEnd && d.FadeEnd != long.MaxValue;
                if (!fading)
                {
                    if (isPaused) d.StartSample = Math.Max(d.StartSample, clock + BlockSize);
                    else if (clock >= d.StartSample) Start(d);
                }
                if (d.State == DeckState.Scheduled)
                {
                    if (fading) Mix(d);
                    return;
                }
            }
            if (d.State == DeckState.Playing && !isPaused) Step(d);
            Mix(d);
            if (d.CutWhenSilent && d.PauseTo == 0 && clock + BlockSize >= d.PauseEnd)
            {
                // The pause fade has reached silence: stop every voice, pedal-held ones included.
                d.Synth.SilenceAll();
                d.CutWhenSilent = false;
                d.Cut = true;
            }
            if (d.State == DeckState.Tail && clock + BlockSize >= d.FadeEnd)
            {
                d.Synth.Reset();
                d.State = DeckState.Idle;
                d.Job = null;
                d.ClearFade();
                if (gridDeck == d) gridDeck = null;
            }
        }

        void Mix(Deck d)
        {
            d.Synth.Render(d.L, d.R, 0, BlockSize);
            double g0 = d.GainAt(clock) * d.PauseGainAt(clock), g1 = d.GainAt(clock + BlockSize) * d.PauseGainAt(clock + BlockSize);
            if (g0 == 1 && g1 == 1)
            {
                for (int k = 0; k < BlockSize; k++)
                {
                    mixL[k] += d.L[k];
                    mixR[k] += d.R[k];
                }
                return;
            }
            double step = (g1 - g0) / BlockSize;
            for (int k = 0; k < BlockSize; k++)
            {
                float g = (float)(g0 + step * k);
                mixL[k] += d.L[k] * g;
                mixR[k] += d.R[k] * g;
            }
        }

        void Start(Deck d)
        {
            PlaybackJob job = d.Job!;
            d.Synth.Reset();
            Array.Clear(d.Sounding, 0, d.Sounding.Length);
            d.ClearFade();
            MidiMessage[] chase = job.Excerpt.Chase;
            for (int i = 0; i < chase.Length; i++) d.Synth.Send(chase[i].Status, chase[i].Data1, chase[i].Data2);
            d.Beat = d.JoinSeconds > 0 ? job.Advance(job.StartBeat, d.JoinSeconds) : job.StartBeat;
            d.NextEvent = 0;
            d.State = DeckState.Playing;
            d.Synth.SetTranspose(job.SemitonesAt(job.StartBeat));
            Interlocked.Exchange(ref pubStarted, job.Id);
            Observer?.OnStarted(d.Index, job, clock);
        }

        void Step(Deck d)
        {
            PlaybackJob job = d.Job!;
            double b0 = d.Beat;
            double bHalf = job.Advance(b0, 0.5 * stepSeconds);
            double bEnd = job.Advance(b0, stepSeconds);
            double end = job.EndBeat;
            bool ending = end < bHalf;
            MidiMessage[] events = job.Excerpt.Events;
            while (d.NextEvent < events.Length && (ending || events[d.NextEvent].Beat < bHalf))
                Dispatch(d, job, events[d.NextEvent++]);
            d.Synth.SetTranspose(job.SemitonesAt(Math.Min(bHalf, end)));
            if (!ending)
            {
                d.Beat = bEnd;
                return;
            }
            double frac = bEnd > b0 ? (end - b0) / (bEnd - b0) : 0;
            FinishNaturally(d, job, clock + frac * BlockSize);
        }

        void Dispatch(Deck d, PlaybackJob job, in MidiMessage m)
        {
            d.Synth.Send(m.Status, m.Data1, m.Data2);
            int command = m.Command;
            if (command == MidiCommand.NoteOn || command == MidiCommand.NoteOff)
                d.Sounding[(m.Channel << 7) | m.Data1] = m.IsNoteOn ? m.Data2 : (byte)0;
            Observer?.OnDispatch(d.Index, job, clock, m);
        }

        void FinishNaturally(Deck d, PlaybackJob job, double endSample)
        {
            d.State = DeckState.Tail;
            d.Beat = job.EndBeat;
            long tail = (long)(Settings.TailSeconds * rate);
            long fade = (long)(Math.Min(Settings.TailFadeSeconds, Settings.TailSeconds) * rate);
            d.FadeFrom = 1;
            d.FadeStart = clock + tail - fade;
            d.FadeEnd = clock + tail;
            // The pulse the listener just heard: the mean rate over the last bar played, not the
            // instantaneous tempo at the end beat (a tempo change there has not sounded yet).
            double lastBarStart = Math.Max(job.StartBeat, job.EndBeat - job.BeatsPerBar);
            double lastBarSeconds = job.SecondsBetween(lastBarStart, job.EndBeat);
            double beatsPerSecond = lastBarSeconds > 0 ? (job.EndBeat - lastBarStart) / lastBarSeconds : job.RateAt(job.EndBeat);
            double beatSamples = rate / (beatsPerSecond * Math.Max(1e-6, stepSeconds / blockSeconds));
            double barSamples = beatSamples * job.BeatsPerBar;
            gridUnit = Settings.Handoff == HandoffGrid.Beat ? beatSamples : barSamples;
            gridOrigin = endSample;
            gridUntil = endSample + Settings.HandoffWindowBars * barSamples;
            gridValid = Settings.Handoff != HandoffGrid.Immediate;
            gridDeck = d;
            if (active == d) active = null;
            SetPublished(job.EndBeat, job.SemitonesAt(job.EndBeat), job.BpmAt(job.EndBeat));
            Interlocked.Exchange(ref pubFinished, job.Id);
            Observer?.OnFinished(d.Index, job, endSample);
        }

        void Interrupt(Deck d, long length)
        {
            if (d.State == DeckState.Playing)
            {
                d.Synth.ReleaseAll();
                d.State = DeckState.Tail;
                d.FadeOut(clock, length);
            }
            else if (d.State == DeckState.Scheduled)
            {
                // Never started: nothing of this job sounds; an older tail may still be fading.
                bool fading = d.FadeEnd != long.MaxValue && clock < d.FadeEnd;
                d.State = fading ? DeckState.Tail : DeckState.Idle;
                if (!fading) d.ClearFade();
            }
            Array.Clear(d.Sounding, 0, d.Sounding.Length);
        }

        void StopActive()
        {
            // A clip that already ended naturally keeps its handoff grid: the next aligned job
            // still lands on the finished clip's bar line (the grid expires after
            // HandoffWindowBars anyway). Only an interrupted clip invalidates it.
            if (active != null)
            {
                Interrupt(active, (long)(Settings.InterruptSeconds * rate));
                gridValid = false;
            }
            active = null;
            Interlocked.Exchange(ref pubActive, 0);
        }

        void Accept(PlaybackJob job)
        {
            if (job.Id < Interlocked.Read(ref latestRequest)) return;   // superseded while it was being prepared
            long now = clock;
            long start = now;
            double join = 0;
            bool onGrid = false;
            if (job.AlignToHandoff && gridValid && active == null && gridUnit > 0)
            {
                double elapsed = now - gridOrigin;
                if (elapsed > 0 && elapsed <= Settings.LateJoinSeconds * rate)
                {
                    join = elapsed / rate * (stepSeconds / blockSeconds);   // musical seconds
                    onGrid = true;
                }
                else
                {
                    double k = Math.Ceiling(elapsed / gridUnit - 1e-9);
                    double s = gridOrigin + Math.Max(0, k) * gridUnit;
                    if (s <= gridUntil)
                    {
                        start = Math.Max(now, (long)Math.Round(s / BlockSize) * BlockSize);
                        onGrid = true;
                    }
                }
            }
            gridValid = false;
            if (active != null) Interrupt(active, (long)(Settings.InterruptSeconds * rate));
            Deck? previous = active;
            active = null;

            // Prefer an idle deck; otherwise reuse the deck whose tail is closest to silence.
            Deck? target = null;
            foreach (Deck d in decks)
                if (d.State == DeckState.Idle && d != previous) { target = d; break; }
            if (target == null)
                foreach (Deck d in decks)
                    if (d.State == DeckState.Idle) { target = d; break; }
            if (target == null)
            {
                foreach (Deck d in decks)
                    if (d != previous && d != gridDeck && (target == null || d.FadeEnd < target.FadeEnd)) target = d;
                if (target == null)
                    foreach (Deck d in decks)
                        if (d != previous && (target == null || d.FadeEnd < target.FadeEnd)) target = d;
                target ??= decks[0].FadeEnd <= decks[1].FadeEnd ? decks[0] : decks[1];
                target.FadeOut(now, (long)(Settings.RecycleSeconds * rate));
                long ready = (target.FadeEnd + BlockSize - 1) / BlockSize * BlockSize;
                // A deck must finish fading before it restarts; on the grid, join that much later.
                if (ready > start && onGrid) join += (double)(ready - start) / rate * (stepSeconds / blockSeconds);
                start = Math.Max(start, ready);
            }
            target.Job = job;
            target.State = DeckState.Scheduled;
            target.StartSample = start;
            target.JoinSeconds = join;
            active = target;
            Interlocked.Exchange(ref pubActive, job.Id);
            SetPublished(job.StartBeat, job.SemitonesAt(job.StartBeat), job.BpmAt(job.StartBeat));
        }

        void PauseChanged(bool nowPaused)
        {
            long fade = (long)(Settings.PauseFadeSeconds * rate);
            foreach (Deck d in decks)
            {
                if (d.State != DeckState.Playing) continue;
                if (nowPaused)
                {
                    // Fade to silence, then cut the voices (Process). A note-off would leave
                    // pedal-held voices ringing for the whole pause.
                    d.RampPause(clock, 0, fade);
                    d.CutWhenSilent = true;
                    continue;
                }
                d.CutWhenSilent = false;
                if (d.Cut)
                {
                    // Resume: strike again what was held when we paused (the old voices are gone,
                    // so nothing doubles); the controller state, pedal included, was never touched.
                    for (int i = 0; i < d.Sounding.Length; i++)
                        if (d.Sounding[i] > 0) d.Synth.Send(MidiCommand.NoteOn | (i >> 7), i & 0x7F, d.Sounding[i]);
                    d.Cut = false;
                }
                d.RampPause(clock, 1, fade);
            }
        }

        void Publish()
        {
            Interlocked.Exchange(ref pubClock, clock);
            Deck? d = active;
            if (d == null || d.Job == null || d.State != DeckState.Playing) return;
            PlaybackJob job = d.Job;
            SetPublished(d.Beat, job.SemitonesAt(d.Beat), job.BpmAt(d.Beat));
        }

        void SetPublished(double beat, double semitones, double bpm)
        {
            Interlocked.Exchange(ref pubBeat, BitConverter.DoubleToInt64Bits(beat));
            Interlocked.Exchange(ref pubSemis, BitConverter.DoubleToInt64Bits(semitones));
            Interlocked.Exchange(ref pubBpm, BitConverter.DoubleToInt64Bits(bpm));
        }
    }
}
