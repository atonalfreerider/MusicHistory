#nullable enable
using System;
using System.Diagnostics;
using System.Threading;

namespace MusicHistory.Audio
{
    /// <summary>
    /// Decides who renders the <see cref="DeckEngine"/>. Normally the audio callback does. When no
    /// callback has arrived for <see cref="StallSeconds"/> (batchmode, audio disabled, no
    /// output device), the main thread renders the elapsed time into a scratch buffer instead,
    /// so clip timing, CurrentBeat and Finished keep working, silently. A compare-and-swap flag
    /// guarantees the two never render at once; the audio thread never waits.
    /// </summary>
    public sealed class EngineDriver
    {
        DeckEngine? engine;
        long lastCallback;          // Now() at the last audio callback
        bool anyCallback;
        long firstTick;             // Now() at the first main-thread tick
        bool ticked;
        int busy;
        readonly float[] scratchL = new float[1024], scratchR = new float[1024];
        Exception? error;

        /// <summary>Clock source (Stopwatch ticks); replaceable for tests.</summary>
        public Func<long> Now = Stopwatch.GetTimestamp;
        public double TicksPerSecond = Stopwatch.Frequency;
        public double StallSeconds = 0.3;
        /// <summary>Longest stretch the main thread catches up in one tick (a hitch is not replayed in full).</summary>
        public double MaxCatchUpSeconds = 0.25;

        public DeckEngine? Engine
        {
            get => Volatile.Read(ref engine);
            set => Volatile.Write(ref engine, value);
        }

        /// <summary>True while the main thread keeps the clock (no audio callbacks).</summary>
        public bool DrivingFromMainThread { get; private set; }

        /// <summary>An exception thrown while rendering (the engine is dropped); read and cleared by the owner.</summary>
        public Exception? TakeError() => Interlocked.Exchange(ref error, null);

        /// <summary>The audio callback: renders into the interleaved buffer, or silence.</summary>
        public void AudioCallback(float[] data, int channels)
        {
            Interlocked.Exchange(ref lastCallback, Now());
            Volatile.Write(ref anyCallback, true);
            DeckEngine? e = Engine;
            if (e == null || Interlocked.CompareExchange(ref busy, 1, 0) != 0)
            {
                Array.Clear(data, 0, data.Length);
                return;
            }
            try
            {
                e.Render(data, channels);
            }
            catch (Exception ex)
            {
                Engine = null;
                Interlocked.Exchange(ref error, ex);
                Array.Clear(data, 0, data.Length);
            }
            finally
            {
                Volatile.Write(ref busy, 0);
            }
        }

        /// <summary>
        /// Called once per frame on the main thread with the frame's real (unscaled) duration.
        /// Returns the number of frames it rendered (0 while the audio callback is alive).
        /// </summary>
        public int MainThreadTick(double seconds)
        {
            DeckEngine? e = Engine;
            if (e == null) return 0;
            long now = Now();
            if (!ticked)
            {
                ticked = true;
                firstTick = now;
            }
            long reference = Volatile.Read(ref anyCallback) ? Interlocked.Read(ref lastCallback) : firstTick;
            if ((now - reference) / TicksPerSecond < StallSeconds)
            {
                DrivingFromMainThread = false;
                return 0;
            }
            DrivingFromMainThread = true;
            int frames = (int)Math.Round(Math.Max(0, Math.Min(seconds, MaxCatchUpSeconds)) * e.SampleRate);
            if (frames == 0 || Interlocked.CompareExchange(ref busy, 1, 0) != 0) return 0;
            try
            {
                for (int done = 0; done < frames; done += scratchL.Length)
                    e.RenderStereo(scratchL, scratchR, 0, Math.Min(scratchL.Length, frames - done));
            }
            catch (Exception ex)
            {
                Engine = null;
                Interlocked.Exchange(ref error, ex);
                return 0;
            }
            finally
            {
                Volatile.Write(ref busy, 0);
            }
            return frames;
        }
    }
}
