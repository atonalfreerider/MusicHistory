#nullable enable
using System;
using System.Collections.Generic;
using System.IO;
using MeltySynth;

namespace MusicHistory.Audio.Tests
{
    public sealed class CheckFailed : Exception
    {
        public CheckFailed(string message) : base(message) { }
    }

    public sealed class SkipCheck : Exception
    {
        public SkipCheck(string message) : base(message) { }
    }

    /// <summary>Collects measured numbers for the report printed after each check.</summary>
    public sealed class Report
    {
        public readonly List<string> Lines = new List<string>();
        public void Metric(string name, double value, string unit = "", string format = "F3") =>
            Lines.Add($"{name} = {value.ToString(format, System.Globalization.CultureInfo.InvariantCulture)}{(unit.Length > 0 ? " " + unit : "")}");
        public void Note(string text) => Lines.Add(text);
    }

    public static class Assert
    {
        public static void True(bool condition, string message)
        {
            if (!condition) throw new CheckFailed(message);
        }

        public static void Near(double actual, double expected, double tolerance, string what)
        {
            if (!(Math.Abs(actual - expected) <= tolerance))
                throw new CheckFailed($"{what}: {actual:G6} not within {tolerance:G3} of {expected:G6}");
        }
    }

    public static class Env
    {
        static SoundFont? soundFont;
        static bool searched;

        /// <summary>The repository root (folder holding docs/DESIGN.md), found from the executable upwards.</summary>
        public static string RepoRoot
        {
            get
            {
                string? dir = AppContext.BaseDirectory;
                while (dir != null)
                {
                    if (File.Exists(Path.Combine(dir, "docs", "DESIGN.md"))) return dir;
                    dir = Path.GetDirectoryName(dir);
                }
                throw new InvalidOperationException("repository root not found");
            }
        }

        public static string DataDir =>
            Environment.GetEnvironmentVariable("MUSICHISTORY_DATA") is string d && d.Length > 0 ? d : Path.Combine(RepoRoot, "data");

        public static string SoundFontPath =>
            Environment.GetEnvironmentVariable("MUSICHISTORY_SF2") is string p && p.Length > 0
                ? p : Path.Combine(DataDir, "soundfonts", "MS_Basic.sf2");

        /// <summary>The converted SoundFont, loaded once; checks that need it skip when it is missing.</summary>
        public static SoundFont SoundFont
        {
            get
            {
                if (!searched)
                {
                    searched = true;
                    if (File.Exists(SoundFontPath)) soundFont = new SoundFont(SoundFontPath);
                }
                return soundFont ?? throw new SkipCheck($"no SoundFont at {SoundFontPath} (run tools/sf3_to_sf2.py)");
            }
        }

        /// <summary>Folder with real MIDI files (the research prototypes' pp-test set) or null.</summary>
        public static string? PpTestDir
        {
            get
            {
                string? env = Environment.GetEnvironmentVariable("MUSICHISTORY_PPTEST");
                if (!string.IsNullOrEmpty(env) && Directory.Exists(env)) return env;
                string guess = Path.Combine(Path.GetTempPath(), "claude", "C--Users-johnb-Desktop-MusicHistory",
                    "fdf70476-fc2d-494c-ab1e-43c07c356046", "scratchpad", "pp-test");
                return Directory.Exists(guess) ? guess : null;
            }
        }
    }

    public static class Dsp
    {
        public static double Rms(float[] x, int from, int count)
        {
            from = Math.Max(0, from);
            count = Math.Min(count, x.Length - from);
            if (count <= 0) return 0;
            double s = 0;
            for (int i = 0; i < count; i++) s += x[from + i] * (double)x[from + i];
            return Math.Sqrt(s / count);
        }

        public static double Db(double rms) => 20 * Math.Log10(rms + 1e-12);

        /// <summary>
        /// Fundamental frequency by autocorrelation: the first lag whose correlation reaches 90 %
        /// of the maximum (avoids octave errors on sub-multiples), refined by a parabola.
        /// </summary>
        public static double Pitch(float[] x, int from, int length, int rate, double minHz = 60, double maxHz = 2000)
        {
            int minLag = (int)(rate / maxHz), maxLag = (int)(rate / minHz);
            if (from + length + maxLag + 2 > x.Length) length = x.Length - from - maxLag - 2;
            var c = new double[maxLag + 2];
            double best = double.MinValue;
            for (int lag = minLag - 1; lag <= maxLag + 1; lag++)
            {
                double s = 0;
                for (int i = 0; i < length; i++) s += x[from + i] * (double)x[from + i + lag];
                c[lag] = s;
                if (lag >= minLag && lag <= maxLag) best = Math.Max(best, s);
            }
            for (int lag = minLag; lag <= maxLag; lag++)
            {
                if (c[lag] < 0.9 * best || c[lag] < c[lag - 1] || c[lag] < c[lag + 1]) continue;
                double y0 = c[lag - 1], y1 = c[lag], y2 = c[lag + 1];
                double den = y0 - 2 * y1 + y2;
                double d = den != 0 ? 0.5 * (y0 - y2) / den : 0;
                return rate / (lag + d);
            }
            return 0;
        }

        public static double Cents(double hz, double reference) => 1200 * Math.Log(hz / reference, 2);

        public static void WriteWav(string path, float[] left, float[] right, int frames, int rate)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
            using (var w = new BinaryWriter(File.Create(path)))
            {
                int dataBytes = frames * 4;
                w.Write(new[] { (byte)'R', (byte)'I', (byte)'F', (byte)'F' });
                w.Write(36 + dataBytes);
                w.Write(new[] { (byte)'W', (byte)'A', (byte)'V', (byte)'E', (byte)'f', (byte)'m', (byte)'t', (byte)' ' });
                w.Write(16);
                w.Write((short)1);
                w.Write((short)2);
                w.Write(rate);
                w.Write(rate * 4);
                w.Write((short)4);
                w.Write((short)16);
                w.Write(new[] { (byte)'d', (byte)'a', (byte)'t', (byte)'a' });
                w.Write(dataBytes);
                for (int i = 0; i < frames; i++)
                {
                    w.Write((short)Math.Round(Math.Max(-1, Math.Min(1, left[i])) * 32767));
                    w.Write((short)Math.Round(Math.Max(-1, Math.Min(1, right[i])) * 32767));
                }
            }
        }
    }

    /// <summary>A synth that renders silence and records what it is told (for timing checks).</summary>
    public sealed class NullSynth : IDeckSynth
    {
        public readonly List<(int Status, int Data1, int Data2)> Messages = new List<(int, int, int)>();
        public double Transpose;
        public int Resets;
        public NullSynth(int rate) { SampleRate = rate; }
        public int SampleRate { get; }
        public void Reset() => Resets++;
        public void Send(int status, int data1, int data2) => Messages.Add((status, data1, data2));
        public void SetTranspose(double semitones) => Transpose = semitones;
        public void ReleaseAll() { }
        public void Render(float[] left, float[] right, int offset, int count)
        {
            Array.Clear(left, offset, count);
            Array.Clear(right, offset, count);
        }
        public int ActiveVoices => 0;
    }

    /// <summary>Wraps a deck synth and logs the RMS of every block it renders, stamped with the engine clock.</summary>
    public sealed class MeteredSynth : IDeckSynth
    {
        readonly IDeckSynth inner;
        readonly Func<long> clock;
        public readonly List<(long Sample, double Rms)> Blocks = new List<(long, double)>();

        public MeteredSynth(IDeckSynth inner, Func<long> clock)
        {
            this.inner = inner;
            this.clock = clock;
        }

        public int SampleRate => inner.SampleRate;
        public int ActiveVoices => inner.ActiveVoices;
        public void Reset() => inner.Reset();
        public void Send(int status, int data1, int data2) => inner.Send(status, data1, data2);
        public void SetTranspose(double semitones) => inner.SetTranspose(semitones);
        public void ReleaseAll() => inner.ReleaseAll();

        public void Render(float[] left, float[] right, int offset, int count)
        {
            inner.Render(left, right, offset, count);
            double s = 0;
            for (int i = 0; i < count; i++) s += left[offset + i] * (double)left[offset + i] + right[offset + i] * (double)right[offset + i];
            Blocks.Add((clock(), Math.Sqrt(s / (2 * count))));
        }

        /// <summary>Highest block RMS within [from, to) samples (0 when the deck did not render there).</summary>
        public double MaxRms(long from, long to)
        {
            double m = 0;
            foreach (var b in Blocks) if (b.Sample >= from && b.Sample < to) m = Math.Max(m, b.Rms);
            return m;
        }

        public long LastRenderedSample => Blocks.Count > 0 ? Blocks[Blocks.Count - 1].Sample : -1;
    }

    /// <summary>Records engine callbacks (single-threaded tests only).</summary>
    public sealed class EventLog : IEngineObserver
    {
        public readonly List<(int Deck, long Job, long Sample)> Started = new List<(int, long, long)>();
        public readonly List<(int Deck, long Job, long Sample, MidiMessage Message)> Dispatched = new List<(int, long, long, MidiMessage)>();
        public readonly List<(int Deck, long Job, double Sample)> Finished = new List<(int, long, double)>();
        public Action<PlaybackJob>? OnFinishedHook;

        public void OnStarted(int deck, PlaybackJob job, long sample) => Started.Add((deck, job.Id, sample));
        public void OnDispatch(int deck, PlaybackJob job, long sample, in MidiMessage message) => Dispatched.Add((deck, job.Id, sample, message));
        public void OnFinished(int deck, PlaybackJob job, double exactEndSample)
        {
            Finished.Add((deck, job.Id, exactEndSample));
            OnFinishedHook?.Invoke(job);
        }
    }

    /// <summary>Builds small songs directly in the beat domain.</summary>
    public sealed class SongBuilder
    {
        readonly List<NoteSpan> notes = new List<NoteSpan>();
        readonly List<MidiMessage> controls = new List<MidiMessage>();
        readonly List<KeyValuePair<double, double>> tempos = new List<KeyValuePair<double, double>>();

        public SongBuilder Tempo(double beat, double bpm)
        {
            tempos.Add(new KeyValuePair<double, double>(beat, 60e6 / bpm));
            return this;
        }

        public SongBuilder Note(double on, double off, int channel, int key, int velocity = 100)
        {
            notes.Add(new NoteSpan(on, off, channel, key, velocity));
            return this;
        }

        public SongBuilder Control(double beat, int status, int data1, int data2 = 0)
        {
            controls.Add(new MidiMessage(beat, status, data1, data2));
            return this;
        }

        public SongBuilder Program(double beat, int channel, int program, int bank = -1)
        {
            if (bank >= 0) Control(beat, MidiCommand.ControlChange | channel, 0, bank);
            return Control(beat, MidiCommand.ProgramChange | channel, program);
        }

        public MidiSong Build()
        {
            var n = new List<NoteSpan>(notes);
            n.Sort((a, b) => a.On.CompareTo(b.On));
            var c = new List<MidiMessage>(controls);
            // stable by beat
            var indexed = new List<(MidiMessage M, int I)>();
            for (int i = 0; i < c.Count; i++) indexed.Add((c[i], i));
            indexed.Sort((a, b) => a.M.Beat != b.M.Beat ? a.M.Beat.CompareTo(b.M.Beat) : a.I.CompareTo(b.I));
            var arr = new MidiMessage[indexed.Count];
            for (int i = 0; i < arr.Length; i++) arr[i] = indexed[i].M;
            double end = 0;
            foreach (var x in n) end = Math.Max(end, x.Off);
            return new MidiSong(480, new TempoMap(tempos), n.ToArray(), arr, end);
        }
    }

    public static class Timing
    {
        /// <summary>
        /// Reference wall time from <paramref name="from"/> to <paramref name="to"/> under a job:
        /// the integral of secondsPerBeat(b) / ratio(b), by composite Simpson per tempo segment
        /// (fine enough to be exact at the microsecond level).
        /// </summary>
        public static double Seconds(PlaybackJob job, double from, double to)
        {
            double total = 0, b = from;
            while (b < to)
            {
                double segEnd = job.FixedBpm > 0 ? to : Math.Min(to, job.Tempo.NextChangeAfter(b));
                double spb = job.FixedBpm > 0 ? 60 / job.FixedBpm : job.Tempo.SecondsPerBeatAt(b);
                double len = segEnd - b;
                int n = Math.Max(16, (int)Math.Ceiling(len * 400));
                if ((n & 1) == 1) n++;
                double h = len / n, s = 0;
                for (int i = 0; i <= n; i++)
                {
                    double x = b + i * h;
                    double f = spb / job.Plan.TempoRatio(x - job.StartBeat);
                    s += f * (i == 0 || i == n ? 1 : (i & 1) == 1 ? 4 : 2);
                }
                total += s * h / 3;
                b = segEnd;
            }
            return total;
        }
    }
}
