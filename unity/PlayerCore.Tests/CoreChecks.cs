#nullable enable
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using MusicHistory.Playback;
using NAudio.Midi;

namespace MusicHistory.Audio.Tests
{
    public static class CoreChecks
    {
        const int Rate = 48000;

        static PlaybackJob Job(long id, MidiSong song, double start, double end, MorphPlan plan, bool align = false, double bpb = 4) =>
            new PlaybackJob(id, ExcerptBuilder.Build(song, start, end), song.Tempo, plan, bpb, align);

        /// <summary>Renders <paramref name="seconds"/> of the engine into new buffers.</summary>
        static (float[] L, float[] R) Render(DeckEngine engine, double seconds, int buffer = 1024)
        {
            int frames = (int)(seconds * engine.SampleRate);
            var l = new float[frames];
            var r = new float[frames];
            for (int i = 0; i < frames; i += buffer) engine.RenderStereo(l, r, i, Math.Min(buffer, frames - i));
            return (l, r);
        }

        static MidiSong SineNote(double lengthBeats, int key = 69) => new SongBuilder()
            .Tempo(0, 120)
            .Program(0, 0, 80, bank: 8)            // bank 8 / 80 = "Sine Wave" in MS Basic
            .Note(0, lengthBeats, 0, key, 100)
            .Build();

        // ------------------------------------------------------------------ SoundFont

        /// <summary>The converted MS Basic SF2 loads in MeltySynth and every tested preset (and the drum kit) sounds.</summary>
        public static void SoundFontRenders(Report r)
        {
            var sw = Stopwatch.StartNew();
            var sf = new MeltySynth.SoundFont(Env.SoundFontPath);
            r.Metric("load", sw.Elapsed.TotalSeconds, "s", "F2");
            r.Note($"{sf.Presets.Count} presets, {sf.SampleHeaders.Count} samples, {sf.WaveData.Length * 2 / 1e6:F0} MB of 16-bit sample data");
            var levels = new List<double>();
            var probes = new (int Channel, int Program, int Key, string Name)[]
            {
                (0, 0, 60, "Grand Piano"), (0, 4, 60, "Tine EP"), (0, 16, 60, "Drawbar Organ"), (0, 24, 55, "Nylon Guitar"),
                (0, 29, 52, "Overdrive Guitar"), (0, 33, 40, "Fingered Bass"), (0, 48, 60, "Strings"), (0, 52, 64, "Choir"),
                (0, 56, 67, "Trumpet"), (0, 65, 62, "Alto Sax"), (0, 73, 72, "Flute"), (0, 81, 64, "Saw Lead"),
                (0, 89, 60, "Warm Pad"), (9, 0, 36, "Kick"), (9, 0, 38, "Snare"), (9, 0, 42, "Closed Hat"), (9, 0, 49, "Crash"),
            };
            foreach (var (ch, prog, key, name) in probes)
            {
                var synth = new MeltyDeckSynth(sf, Rate, reverbAndChorus: false);
                synth.Send(0xC0 | ch, prog, 0);
                synth.Send(0x90 | ch, key, 100);
                var l = new float[Rate / 2];
                var rr = new float[Rate / 2];
                synth.Render(l, rr, 0, l.Length);
                double db = Dsp.Db(Math.Max(Dsp.Rms(l, 0, l.Length), Dsp.Rms(rr, 0, rr.Length)));
                levels.Add(db);
                r.Note($"{(ch == 9 ? "drums" : "prog " + prog),-8} {name,-16} {db,6:F1} dBFS (first 0.5 s)");
                Assert.True(db > -60, $"{name} is silent ({db:F1} dBFS)");
            }
            levels.Sort();
            r.Metric("level spread (max - min)", levels[levels.Count - 1] - levels[0], "dB", "F1");
        }

        // ------------------------------------------------------------------ transposition

        /// <summary>RPN 2 + RPN 1 re-pitch a note that is already sounding (MeltySynth, no vendoring needed).</summary>
        public static void RpnTuning(Report r)
        {
            var synth = new MeltyDeckSynth(Env.SoundFont, Rate, reverbAndChorus: false);
            synth.Send(0xB0, 0, 8);
            synth.Send(0xC0, 80, 0);
            synth.Send(0x90, 69, 100);
            var l = new float[Rate];
            var rr = new float[Rate];
            synth.Render(l, rr, 0, Rate / 2);
            double f0 = Dsp.Pitch(l, Rate / 4, 4096, Rate);
            r.Metric("native A4", f0, "Hz", "F2");
            Assert.True(Math.Abs(Dsp.Cents(f0, 440)) < 3, $"native pitch {f0:F2} Hz");
            double worst = 0;
            foreach (double semis in new[] { 3.0, 2.5, -0.25, -5.7, 5.49, 0.0 })
            {
                synth.SetTranspose(semis);
                synth.Render(l, rr, 0, Rate / 4);
                double f = Dsp.Pitch(l, 2048, 4096, Rate);
                double err = Dsp.Cents(f, 440 * Math.Pow(2, semis / 12));
                worst = Math.Max(worst, Math.Abs(err));
                r.Note($"transpose {semis,6:F2} st on the sounding note -> {f:F2} Hz ({err:+0.00;-0.00;0.00} cents)");
            }
            r.Metric("worst tuning error", worst, "cents", "F2");
            Assert.True(worst < 3, $"tuning error {worst:F2} cents");
        }

        /// <summary>A held note under a +3 -> 0 morph starts a minor third up and lands on pitch.</summary>
        public static void MorphGlideFrequency(Report r)
        {
            var engine = DeckEngine.CreateMelty(Env.SoundFont, Rate, reverbAndChorus: false);
            var plan = new MorphPlan(3, 1, 8);
            engine.Post(Job(1, SineNote(16), 0, 16, plan));
            var (l, _) = Render(engine, 8.5);
            // 120 BPM, ratio 1: beat b is at b/2 seconds.
            foreach (double beat in new[] { 0.25, 2.0, 4.0, 6.0, 9.0, 15.0 })
            {
                if (beat / 2 * Rate + 4096 > l.Length) continue;
                int at = (int)(beat / 2 * Rate) - 1024;
                double f = Dsp.Pitch(l, Math.Max(0, at), 2048, Rate);
                double expected = 440 * Math.Pow(2, plan.Semitones(beat) / 12);
                double err = Dsp.Cents(f, expected);
                r.Note($"beat {beat,5:F2}: plan {plan.Semitones(beat):+0.000;-0.000;0.000} st, expected {expected:F2} Hz, measured {f:F2} Hz ({err:+0.0;-0.0;0.0} cents)");
                Assert.True(Math.Abs(err) < 6, $"glide pitch at beat {beat}: {err:F1} cents");
            }
            double start = Dsp.Pitch(l, 2400, 2048, Rate), end = Dsp.Pitch(l, (int)(4.5 * Rate), 2048, Rate);
            r.Metric("start frequency", start, "Hz (expect 523.25)", "F2");
            r.Metric("end frequency", end, "Hz (expect 440.00)", "F2");
        }

        /// <summary>Channel 10 is never transposed: a drum groove renders identically with and without a morph.</summary>
        public static void DrumsUntransposed(Report r)
        {
            var b = new SongBuilder().Tempo(0, 120);
            for (int beat = 0; beat < 8; beat++)
            {
                b.Note(beat, beat + 0.25, 9, beat % 2 == 0 ? 36 : 38, 110);
                b.Note(beat, beat + 0.1, 9, 42, 80).Note(beat + 0.5, beat + 0.6, 9, 42, 70);
            }
            b.Note(6.5, 7, 9, 45, 100).Note(7, 7.5, 9, 47, 100);
            MidiSong drums = b.Build();
            float[] Run(MidiSong song, MorphPlan plan)
            {
                var engine = DeckEngine.CreateMelty(Env.SoundFont, Rate);
                engine.Post(Job(1, song, 0, 8, plan));
                return Render(engine, 4.5).L;
            }
            var plain = Run(drums, MorphPlan.None);
            var morphed = Run(drums, new MorphPlan(5, 1, 16));
            double maxDiff = 0;
            for (int i = 0; i < plain.Length; i++) maxDiff = Math.Max(maxDiff, Math.Abs(plain[i] - morphed[i]));
            r.Metric("drum RMS", Dsp.Rms(plain, 0, plain.Length), "", "F4");
            r.Metric("max |difference| drums, +5 st morph vs none", maxDiff, "", "E2");
            Assert.True(Dsp.Rms(plain, 0, plain.Length) > 1e-3, "drums are silent");
            Assert.True(maxDiff == 0, "drum channel changed under transposition");
            // Control: a pitched part in the same song does change.
            var withBass = new SongBuilder().Tempo(0, 120).Program(0, 1, 33).Note(0, 8, 1, 40, 100);
            foreach (var n in drums.Notes) withBass.Note(n.On, n.Off, n.Channel, n.Key, n.Velocity);
            var a = Run(withBass.Build(), MorphPlan.None);
            var c = Run(withBass.Build(), new MorphPlan(5, 1, 16));
            double diff = 0;
            for (int i = 0; i < a.Length; i++) diff = Math.Max(diff, Math.Abs(a[i] - c[i]));
            r.Metric("max |difference| with a bass part (control)", diff, "", "F4");
            Assert.True(diff > 1e-3, "pitched channel was not transposed");
        }

        // ------------------------------------------------------------------ timing

        static (double MaxMs, double MeanMs, double LastMs, int Count) OnsetErrors(PlaybackJob job, int rate, IEnumerable<(long Sample, double Beat)> onsets, long startSample)
        {
            double max = 0, sum = 0, last = 0;
            int n = 0;
            foreach (var (sample, beat) in onsets)
            {
                double expected = startSample + Timing.Seconds(job, job.StartBeat, beat) * rate;
                double errMs = (sample - expected) / rate * 1000;
                max = Math.Max(max, Math.Abs(errMs));
                sum += errMs;
                last = errMs;
                n++;
            }
            return (max, n > 0 ? sum / n : 0, last, n);
        }

        static (double MaxMs, double MeanMs, double LastMs, int Count) RunTiming(MidiSong song, double start, double end, MorphPlan plan, int rate, int buffer = 1024)
        {
            var engine = new DeckEngine(new NullSynth(rate), new NullSynth(rate));
            var log = new EventLog();
            engine.Observer = log;
            var job = Job(1, song, start, end, plan);
            engine.Post(job);
            double seconds = Timing.Seconds(job, start, end) + 0.5;
            int frames = (int)(seconds * rate);
            var l = new float[buffer];
            var rr = new float[buffer];
            for (int i = 0; i < frames; i += buffer) engine.RenderStereo(l, rr, 0, buffer);
            Assert.True(log.Started.Count == 1 && log.Finished.Count == 1, "job did not start and finish exactly once");
            // Reconstruct the beat of each dispatched note-on from the excerpt's own event list.
            var onsets = new List<(long, double)>();
            var ons = job.Excerpt.Events.Where(e => e.IsNoteOn).ToArray();
            var dispatched = log.Dispatched.Where(d => d.Message.IsNoteOn).ToArray();
            Assert.True(ons.Length == dispatched.Length, $"dispatched {dispatched.Length} of {ons.Length} note-ons");
            for (int i = 0; i < ons.Length; i++) onsets.Add((dispatched[i].Sample, ons[i].Beat));
            return OnsetErrors(job, rate, onsets, log.Started[0].Sample);
        }

        /// <summary>Onsets follow the tempo integral (tempo map x morph) within 1 ms, at 48 and 44.1 kHz, with no drift.</summary>
        public static void TempoIntegralTiming(Report r)
        {
            var b = new SongBuilder().Tempo(0, 120).Tempo(64, 90).Tempo(128, 150).Tempo(200.5, 100);
            for (int beat = 0; beat < 256; beat++) b.Note(beat, beat + 0.5, 0, 60 + beat % 12);
            MidiSong song = b.Build();
            var plan = new MorphPlan(-4, 0.7, 16);
            foreach (int rate in new[] { 48000, 44100 })
            {
                var e = RunTiming(song, 0, 256, plan, rate);
                r.Note($"{rate} Hz, 4 tempo segments, ratio 0.70->1 over 16 beats, 64 bars: {e.Count} onsets, max |err| {e.MaxMs:F3} ms, mean {e.MeanMs:+0.000;-0.000;0.000} ms, last {e.LastMs:+0.000;-0.000;0.000} ms");
                Assert.True(e.MaxMs < 1.0, $"onset error {e.MaxMs:F3} ms at {rate} Hz");
            }
            // Excerpt from the middle, across tempo changes, with a speed-up morph.
            var mid = RunTiming(song, 60, 140, new MorphPlan(2, 1.4, 16), 48000, 441);
            r.Note($"excerpt 60..140 (crosses 2 changes), ratio 1.40->1, odd 441-frame buffers: max |err| {mid.MaxMs:F3} ms");
            Assert.True(mid.MaxMs < 1.0, $"mid-song excerpt error {mid.MaxMs:F3} ms");
        }

        public static void NoDrift64Bars(Report r)
        {
            var b = new SongBuilder().Tempo(0, 120);
            for (int beat = 0; beat <= 256; beat++) b.Note(beat, beat + 0.25, 0, 60);
            var e = RunTiming(b.Build(), 0, 257, MorphPlan.None, 48000);
            r.Note($"120 BPM, 257 onsets over 64 bars: max |err| {e.MaxMs:F3} ms, mean {e.MeanMs:+0.0000;-0.0000;0.0000} ms, beat 256 at {e.LastMs:+0.000;-0.000;0.000} ms");
            Assert.True(e.MaxMs < 1.0 && Math.Abs(e.LastMs) < 1.0, "drift over 64 bars");
            var odd = new SongBuilder().Tempo(0, 97.3);
            for (int beat = 0; beat <= 256; beat++) odd.Note(beat, beat + 0.25, 0, 60);
            var e2 = RunTiming(odd.Build(), 0, 257, new MorphPlan(0, 1.1, 8), 44100, 480);
            r.Note($"97.3 BPM at 44.1 kHz, 480-frame buffers, ratio 1.1->1: max |err| {e2.MaxMs:F3} ms, beat 256 at {e2.LastMs:+0.000;-0.000;0.000} ms");
            Assert.True(e2.MaxMs < 1.0, "drift at 97.3 BPM");
        }

        /// <summary>A real audio-aligned MIDI with a tempo event every 10 ms still lands every onset within 1 ms.</summary>
        public static void DenseTempoMap(Report r)
        {
            string? dir = Env.PpTestDir;
            if (dir == null) throw new SkipCheck("pp-test MIDI folder not found");
            string path = Path.Combine(dir, "Ticket-to-Ride---The-Beatles", "aligned.mid");
            if (!File.Exists(path)) throw new SkipCheck("Ticket to Ride test MIDI missing");
            var sw = Stopwatch.StartNew();
            MidiSong song = MidiSongReader.Read(path);
            r.Metric("parse", sw.Elapsed.TotalMilliseconds, "ms", "F1");
            r.Note($"{song.Tempo.Count} tempo segments, {song.Notes.Length} notes, {song.Controls.Length} controls, end beat {song.EndBeat:F1}");
            var e = RunTiming(song, 68, 132, new MorphPlan(-4, 0.72, 16), 48000);
            r.Note($"excerpt 68..132 with morph ratio 0.72->1: {e.Count} onsets, max |err| {e.MaxMs:F3} ms, mean {e.MeanMs:+0.000;-0.000;0.000} ms");
            Assert.True(e.MaxMs < 1.0, $"dense tempo map onset error {e.MaxMs:F3} ms");
        }

        // ------------------------------------------------------------------ morph math

        public static void MorphMath(Report r)
        {
            Assert.True(Morph.Wrap(8) == -4 && Morph.Wrap(6) == -6 && Morph.Wrap(-6) == -6 && Morph.Wrap(5) == 5
                        && Morph.Wrap(-7) == 5 && Morph.Wrap(11) == -1 && Morph.Wrap(0) == 0 && Morph.Wrap(-13) == -1, "Wrap");
            SongClip Clip(int tonic, double bpm, double bpb = 4) => new SongClip { TonicPc = tonic, NativeBpm = bpm, BeatsPerBar = bpb };
            var p = Morph.Plan(Clip(9, 125), Clip(1, 87), 4);
            Assert.Near(p.StartSemitones, -4, 1e-12, "A -> C# semitones");
            Assert.Near(p.StartTempoRatio, 125.0 / 87, 1e-12, "125 -> 87 ramps literally from 125 (0.52 octave, below the fold threshold)");
            Assert.Near(Morph.Plan(Clip(0, 150), Clip(0, 80), 4).StartTempoRatio, 75.0 / 80, 1e-12, "150 -> 80 (0.91 octave) folds to 75");
            Assert.Near(p.MorphBeats, 16, 1e-12, "4 bars of 4/4");
            Assert.Near(Morph.Plan(Clip(0, 70), Clip(0, 140), 4).StartTempoRatio, 1, 1e-12, "70 -> 140 is a double-time match");
            Assert.Near(Morph.Plan(Clip(0, 200), Clip(0, 90), 4).StartTempoRatio, 100.0 / 90, 1e-12, "200 -> 90 folds to 100");
            Assert.Near(Morph.Plan(Clip(0, 120), Clip(0, 120, 3), 4).MorphBeats, 12, 1e-12, "3/4: 12 beats");
            Assert.True(Morph.Plan(null!, Clip(0, 120), 4).MorphBeats == 0, "no previous clip = native");
            Assert.Near(p.Progress(0), 0, 0, "progress(0)");
            Assert.Near(p.Progress(8), 0.5, 1e-12, "progress(mid)");
            Assert.Near(p.Progress(16), 1, 0, "progress(end)");
            Assert.True(p.Progress(1e-4) / 1e-4 < 1e-3 && (1 - p.Progress(16 - 1e-4)) / 1e-4 < 1e-3, "smoothstep slope is zero at both ends");
            Assert.Near(p.Semitones(0), -4, 0, "start semitones");
            Assert.Near(p.Semitones(16), 0, 0, "end semitones");
            Assert.Near(p.TempoRatio(0), 125.0 / 87, 1e-12, "start ratio");
            Assert.Near(p.TempoRatio(40), 1, 0, "native after the morph");

            // The engine reports the same numbers while playing.
            var song = new SongBuilder().Tempo(0, 87).Note(0, 32, 0, 60).Build();
            var engine = new DeckEngine(new NullSynth(Rate), new NullSynth(Rate));
            engine.Post(Job(7, song, 0, 32, p));
            var buf = new float[64];
            engine.RenderStereo(buf, buf, 0, 64);
            EngineState s0 = engine.ReadState();
            r.Note($"engine at start: beat {s0.Beat:F4}, {s0.Semitones:F4} st, {s0.Bpm:F3} BPM (plan: -4 st, 125.000 BPM)");
            Assert.Near(s0.Semitones, -4, 0.01, "published start semitones");
            Assert.Near(s0.Bpm, 125.0, 0.1, "published start BPM");
            var bigL = new float[1024];
            var bigR = new float[1024];
            while (engine.ReadState().Beat < 17) engine.RenderStereo(bigL, bigR, 0, 1024);
            EngineState s1 = engine.ReadState();
            r.Note($"engine after the morph: beat {s1.Beat:F3}, {s1.Semitones:F4} st, {s1.Bpm:F3} BPM");
            Assert.Near(s1.Semitones, 0, 1e-12, "semitones after morph");
            Assert.Near(s1.Bpm, 87, 1e-9, "BPM after morph");
        }

        // ------------------------------------------------------------------ excerpt

        public static void ExcerptChase(Report r)
        {
            SongBuilder Notes(SongBuilder b) => b
                .Note(0, 1, 0, 40)            // before: not played
                .Note(4, 12, 0, 45)           // held across the start: re-struck at 8
                .Note(6, 7.5, 0, 43)          // ends before: dropped
                .Note(7.95, 9, 0, 47)         // early downbeat: moved to 8
                .Note(10, 11, 0, 48)
                .Note(14, 20, 0, 50)          // crosses the end: note-off at 16
                .Note(16, 17, 0, 52);         // at the end: excluded
            SongBuilder Controls(SongBuilder b, double at) => b
                .Program(at, 0, 33, bank: 0)
                .Control(at, 0xB0, 7, 90)
                .Control(at, 0xB0, 10, 30)
                .Control(at, 0xB0, 101, 0).Control(at, 0xB0, 100, 0).Control(at, 0xB0, 6, 12).Control(at, 0xB0, 38, 0)
                .Control(at, 0xE0, 10000 & 127, 10000 >> 7)
                .Control(at, 0xC0 | 1, 48)
                .Control(at == 0 ? 2 : at, 0xB0, 7, 70);
            MidiSong early = Controls(Notes(new SongBuilder().Tempo(0, 120)), 0).Build();
            var ex = ExcerptBuilder.Build(early, 8, 16);
            string Fmt(MidiMessage m) => $"{m.Status:X2}:{m.Data1}:{m.Data2}";
            string chase = string.Join(" ", ex.Chase.Select(Fmt));
            r.Note($"chase: {chase}");
            Assert.True(ex.Chase.All(m => m.Beat == 8), "chase is applied at the start beat");
            Assert.True(ex.Chase.Any(m => m.Status == 0xC0 && m.Data1 == 33) && ex.Chase.Any(m => m.Status == 0xC1 && m.Data1 == 48), "programs chased");
            Assert.True(ex.Chase.Count(m => m.Status == 0xB0 && m.Data1 == 7) == 1 && ex.Chase.Single(m => m.Status == 0xB0 && m.Data1 == 7).Data2 == 70, "only the last volume is chased");
            Assert.True(chase.Contains("B0:101:0 B0:100:0 B0:6:12 B0:38:0"), "RPN sequence kept in order");
            Assert.True(ex.Chase.Any(m => m.Status == 0xE0 && ((m.Data2 << 7) | m.Data1) == 10000), "pitch bend chased");
            int bank = Array.FindIndex(ex.Chase, m => m.Status == 0xB0 && m.Data1 == 0), prog = Array.FindIndex(ex.Chase, m => m.Status == 0xC0);
            Assert.True(bank >= 0 && bank < prog, "bank select precedes the program change");
            var ons = ex.Events.Where(e => e.IsNoteOn).Select(e => (e.Beat, (int)e.Data1)).ToArray();
            r.Note("note-ons: " + string.Join(", ", ons.Select(o => $"{o.Item2}@{o.Beat}")));
            Assert.True(ons.SequenceEqual(new[] { (8.0, 45), (8.0, 47), (10.0, 48), (14.0, 50) }), "excerpt note-ons");
            Assert.True(ex.Events.Any(e => e.IsNoteOff && e.Data1 == 50 && e.Beat == 16), "note-off at the excerpt end");
            Assert.True(ex.Events.Last().Beat <= 16, "nothing after the end");

            // Audio: entering mid-song with the chase sounds exactly like a file that sets
            // everything at the excerpt start; without the chase it would be a piano.
            MidiSong atStart = Controls(Notes(new SongBuilder().Tempo(0, 120)), 8).Build();
            MidiSong none = Notes(new SongBuilder().Tempo(0, 120)).Build();
            float[] Run(MidiSong s)
            {
                var engine = DeckEngine.CreateMelty(Env.SoundFont, Rate);
                engine.Post(Job(1, s, 8, 16, MorphPlan.None));
                return Render(engine, 4.2).L;
            }
            float[] a = Run(early), b = Run(atStart), c = Run(none);
            double dAB = 0, dAC = 0;
            for (int i = 0; i < a.Length; i++)
            {
                dAB = Math.Max(dAB, Math.Abs(a[i] - b[i]));
                dAC = Math.Max(dAC, Math.Abs(a[i] - c[i]));
            }
            r.Metric("max |diff| chased vs controls-at-start", dAB, "", "E2");
            r.Metric("max |diff| chased vs no controls (piano)", dAC, "", "F4");
            Assert.True(dAB < 1e-6, "chased excerpt differs from the reference");
            Assert.True(dAC > 1e-3, "chase made no audible difference");
        }

        // ------------------------------------------------------------------ handoff

        static MidiSong Chords(int root, int program, double bars = 4, double bpm = 120)
        {
            var b = new SongBuilder().Tempo(0, bpm).Program(0, 0, program).Program(0, 1, 33);
            for (int bar = 0; bar < bars; bar++)
            {
                double t = bar * 4;
                foreach (int k in new[] { 0, 4, 7 }) b.Note(t, t + 3.9, 0, root + k, 90);
                for (int q = 0; q < 4; q++) b.Note(t + q, t + q + 0.9, 1, root - 24, 100);
                for (int q = 0; q < 8; q++) b.Note(t + q * 0.5, t + q * 0.5 + 0.1, 9, q % 4 == 0 ? 36 : q % 4 == 2 ? 38 : 42, 90);
            }
            return b.Build();
        }

        sealed class Rig
        {
            public DeckEngine Engine = null!;
            public MeteredSynth A = null!, B = null!;
            public EventLog Log = new EventLog();
            long clock;
            public Rig(bool melty = true)
            {
                IDeckSynth a = melty ? new MeltyDeckSynth(Env.SoundFont, Rate) : (IDeckSynth)new SineDeckSynth(Rate);
                IDeckSynth b = melty ? new MeltyDeckSynth(Env.SoundFont, Rate) : (IDeckSynth)new SineDeckSynth(Rate);
                A = new MeteredSynth(a, () => clock);
                B = new MeteredSynth(b, () => clock);
                Engine = new DeckEngine(A, B) { Observer = Log };
            }
            public MeteredSynth Deck(int i) => i == 0 ? A : B;
            /// <summary>Renders in 1024-frame buffers; <paramref name="after"/> runs after each buffer (the "main thread").</summary>
            public void Run(double seconds, Action? after = null, float[]? outL = null, int outOffset = 0)
            {
                var l = new float[1024];
                var rr = new float[1024];
                int frames = (int)(seconds * Rate);
                for (int i = 0; i < frames; i += 1024)
                {
                    int n = Math.Min(1024, frames - i);
                    // MeteredSynth reads the clock through this closure: advance it per 64-frame block.
                    for (int k = 0; k < n; k += 64)
                    {
                        clock = Engine.ReadState().SampleClock;
                        Engine.RenderStereo(l, rr, k, Math.Min(64, n - k));
                    }
                    if (outL != null) Array.Copy(l, 0, outL, outOffset + i, n);
                    after?.Invoke();
                }
            }
        }

        public static void HandoffTail(Report r)
        {
            MidiSong x = Chords(60, 0), y = Chords(65, 48);
            var clipX = new SongClip { TonicPc = 0, NativeBpm = 120, BeatsPerBar = 4 };
            var clipY = new SongClip { TonicPc = 5, NativeBpm = 120, BeatsPerBar = 4 };
            MorphPlan plan = Morph.Plan(clipX, clipY, 4);
            const int bar = 4 * Rate / 2;   // 4 beats at 120 BPM

            // 1) Realistic main-thread latency: Play arrives ~43 ms after the end -> late join on the grid.
            {
                var rig = new Rig();
                rig.Engine.Post(Job(1, x, 0, 16, MorphPlan.None));
                long posted = -1;
                int buffersSinceEnd = -1;
                rig.Run(12, () =>
                {
                    if (rig.Engine.ReadState().FinishedJobId == 1 && buffersSinceEnd < 0) buffersSinceEnd = 0;
                    else if (buffersSinceEnd >= 0 && ++buffersSinceEnd == 2 && posted < 0)
                    {
                        posted = rig.Engine.ReadState().SampleClock;
                        rig.Engine.Post(Job(2, y, 0, 16, plan, align: true));
                    }
                });
                double end = rig.Log.Finished.Single(f => f.Job == 1).Sample;
                var startY = rig.Log.Started.Single(s => s.Job == 2);
                int deckX = rig.Log.Started.Single(s => s.Job == 1).Deck;
                r.Note($"late join: X ended at sample {end:F1} (expected {16 * Rate / 2}), Play posted at {posted} (+{(posted - end) / Rate * 1000:F1} ms), Y started at {startY.Sample} on deck {startY.Deck}");
                Assert.Near(end, 16 * Rate / 2, 33, "X end sample");
                Assert.True(startY.Deck != deckX, "Y must use the other deck");
                // Y's second beat (beat 1) should land exactly one (morphed) beat after X's end.
                var yBeat1 = rig.Log.Dispatched.First(d => d.Job == 2 && d.Message.IsNoteOn && d.Message.Beat >= 1);
                var jobY = Job(2, y, 0, 16, plan, align: true);
                double expected = end + Timing.Seconds(jobY, 0, 1) * Rate;
                double errMs = (yBeat1.Sample - expected) / Rate * 1000;
                r.Metric("Y beat 1 vs continued grid", errMs, "ms", "+0.000;-0.000;0.000");
                Assert.True(Math.Abs(errMs) < 1.0, $"grid continuity error {errMs:F3} ms");
                MeteredSynth tail = rig.Deck(deckX);
                double early = tail.MaxRms((long)end + Rate / 10, (long)end + (long)(1.4 * Rate));
                double late = tail.MaxRms((long)end + (long)(2.05 * Rate), (long)end + 6 * Rate);
                r.Metric("outgoing deck RMS 0.1-1.4 s after the end", Dsp.Db(early), "dBFS", "F1");
                r.Note($"outgoing deck last rendered {(tail.LastRenderedSample - end) / Rate:F3} s after the end");
                Assert.True(early > 1e-4, "no release tail on the outgoing deck");
                Assert.True(late == 0 && tail.LastRenderedSample < end + 2.05 * Rate, "outgoing deck still rendering after 2 s");
                Assert.True(rig.Deck(startY.Deck).MaxRms(startY.Sample, startY.Sample + Rate) > 1e-3, "incoming deck silent");
            }

            // 2) Slow arrival (0.5 s): wait for the next bar line of X's continued pulse.
            {
                var rig = new Rig();
                rig.Engine.Post(Job(1, x, 0, 16, MorphPlan.None));
                bool posted = false;
                rig.Run(12, () =>
                {
                    var s = rig.Engine.ReadState();
                    if (!posted && s.FinishedJobId == 1 && s.SampleClock >= 16 * Rate / 2 + Rate / 2)
                    {
                        posted = true;
                        rig.Engine.Post(Job(2, y, 0, 16, plan, align: true));
                    }
                });
                double end = rig.Log.Finished.Single(f => f.Job == 1).Sample;
                long startY = rig.Log.Started.Single(s => s.Job == 2).Sample;
                r.Note($"slow arrival: Y started {(startY - end) / Rate:F4} s after X's end (one bar = {(double)bar / Rate:F4} s)");
                Assert.Near(startY - end, bar, 33, "Y starts on the next bar line");
            }

            // 3) Interrupt (Next pressed mid-song): Y starts at once, X fades within 0.35 s.
            {
                var rig = new Rig();
                rig.Engine.Post(Job(1, x, 0, 16, MorphPlan.None));
                long postedAt = -1;
                rig.Run(6, () =>
                {
                    if (postedAt < 0 && rig.Engine.ReadState().SampleClock >= 3 * Rate)
                    {
                        postedAt = rig.Engine.ReadState().SampleClock;
                        rig.Engine.Post(Job(2, y, 0, 16, plan, align: true));
                    }
                });
                long startY = rig.Log.Started.Single(s => s.Job == 2).Sample;
                int deckX = rig.Log.Started.Single(s => s.Job == 1).Deck;
                Assert.True(rig.Log.Finished.All(f => f.Job != 1), "an interrupted clip must not report Finished");
                r.Note($"interrupt: Y started {(startY - postedAt) / (double)Rate * 1000:F1} ms after Play; X last rendered {(rig.Deck(deckX).LastRenderedSample - postedAt) / (double)Rate:F3} s after Play");
                Assert.True(startY - postedAt <= 64, "Y should start in the next block");
                Assert.True(rig.Deck(deckX).LastRenderedSample <= postedAt + 0.35 * Rate + 64, "interrupted deck rang too long");
            }
        }

        public static void PauseResume(Report r)
        {
            var song = new SongBuilder().Tempo(0, 120).Program(0, 0, 19).Note(0, 32, 0, 60).Note(0, 32, 0, 64).Note(0, 32, 0, 67).Build();
            var engine = DeckEngine.CreateMelty(Env.SoundFont, Rate);
            engine.Post(Job(1, song, 0, 32, MorphPlan.None));
            var before = Render(engine, 1.0).L;
            double beatAtPause = engine.ReadState().Beat;
            engine.Paused = true;
            var paused = Render(engine, 2.0).L;
            double beatAfterPause = engine.ReadState().Beat;
            engine.Paused = false;
            var resumed = Render(engine, 0.5).L;
            double rBefore = Dsp.Rms(before, Rate / 2, Rate / 2), rPaused = Dsp.Rms(paused, (int)(1.5 * Rate), Rate / 2), rResumed = Dsp.Rms(resumed, Rate / 4, Rate / 4);
            r.Note($"beat at pause {beatAtPause:F3}, after 2 s paused {beatAfterPause:F3}; RMS before {Dsp.Db(rBefore):F1} dB, late in pause {Dsp.Db(rPaused):F1} dB, after resume {Dsp.Db(rResumed):F1} dB");
            Assert.Near(beatAfterPause, beatAtPause, 1e-12, "beat advanced while paused");
            Assert.True(rPaused < rBefore * 0.05, "notes kept sounding while paused");
            Assert.True(rResumed > rBefore * 0.5, "held notes not re-struck on resume");
        }

        public static void StopSilences(Report r)
        {
            var engine = DeckEngine.CreateMelty(Env.SoundFont, Rate);
            engine.Post(Job(1, Chords(60, 0), 0, 16, MorphPlan.None));
            Render(engine, 1.0);
            engine.PostStop();
            var after = Render(engine, 1.0).L;
            double tail = Dsp.Rms(after, 0, Rate / 4), rest = Dsp.Rms(after, (int)(0.4 * Rate), (int)(0.6 * Rate));
            r.Note($"after Stop: first 0.25 s {Dsp.Db(tail):F1} dB, from 0.4 s on {Dsp.Db(rest):F1} dB; FinishedJobId {engine.ReadState().FinishedJobId}");
            Assert.True(rest == 0, "audio continues after Stop");
            Assert.True(engine.ReadState().FinishedJobId == 0, "Stop must not report Finished");
        }

        /// <summary>TimeScale speeds the musical clock (the bench runs tours at x30) and 0 pauses.</summary>
        public static void TimeScaleClock(Report r)
        {
            var b = new SongBuilder().Tempo(0, 120);
            for (int beat = 0; beat < 64; beat++) b.Note(beat, beat + 0.5, 0, 60);
            MidiSong song = b.Build();
            var engine = new DeckEngine(new NullSynth(Rate), new NullSynth(Rate));
            var log = new EventLog();
            engine.Observer = log;
            engine.TimeScale = 30;
            engine.Post(Job(1, song, 0, 64, MorphPlan.None));
            var l = new float[1024];
            var rr = new float[1024];
            int buffers = 0;
            while (engine.ReadState().FinishedJobId != 1 && buffers < 10000) { engine.RenderStereo(l, rr, 0, 1024); buffers++; }
            double seconds = buffers * 1024.0 / Rate;
            r.Note($"64 beats at 120 BPM with TimeScale 30: finished after {seconds:F3} s of audio (32 s / 30 = {32.0 / 30:F3} s); reported BPM {engine.ReadState().Bpm:F1}");
            Assert.Near(seconds, 32.0 / 30, 1024.0 / Rate + 1e-9, "x30 clock");
            Assert.Near(engine.ReadState().Bpm, 120, 1e-9, "reported BPM stays musical");
            var e2 = new DeckEngine(new NullSynth(Rate), new NullSynth(Rate));
            e2.Post(Job(1, song, 0, 64, MorphPlan.None));
            e2.RenderStereo(l, rr, 0, 1024);
            e2.TimeScale = 0;
            double before = e2.ReadState().Beat;
            for (int i = 0; i < 50; i++) e2.RenderStereo(l, rr, 0, 1024);
            Assert.Near(e2.ReadState().Beat, before, 0, "TimeScale 0 must freeze the clock");
            e2.TimeScale = 1;
            e2.RenderStereo(l, rr, 0, 1024);
            Assert.True(e2.ReadState().Beat > before, "clock resumes");
        }

        /// <summary>No audio callbacks (batchmode): the main thread keeps the clock; callbacks take over again.</summary>
        public static void DriverWithoutAudio(Report r)
        {
            long now = 0;
            var driver = new EngineDriver { Now = () => now, TicksPerSecond = 1000 };   // milliseconds
            var engine = new DeckEngine(new NullSynth(Rate), new NullSynth(Rate));
            driver.Engine = engine;
            var song = new SongBuilder().Tempo(0, 120).Note(0, 1, 0, 60).Note(7, 8, 0, 62).Build();
            engine.Post(Job(1, song, 0, 8, MorphPlan.None));
            int rendered = 0;
            for (int frame = 0; frame < 300 && engine.ReadState().FinishedJobId != 1; frame++)
            {
                now += 16;
                rendered += driver.MainThreadTick(0.016);
            }
            r.Note($"no callbacks: main thread rendered {rendered} frames ({rendered / (double)Rate:F3} s), finished={engine.ReadState().FinishedJobId == 1}, driving={driver.DrivingFromMainThread}");
            Assert.True(engine.ReadState().FinishedJobId == 1 && driver.DrivingFromMainThread, "clip must finish without audio callbacks");
            Assert.Near(rendered / (double)Rate, 4.0, 0.05, "main-thread clock renders the clip in real time once the 0.3 s stall window passed");
            // Audio callbacks arrive: the main thread stops rendering.
            var data = new float[2048];
            driver.AudioCallback(data, 2);
            now += 16;
            Assert.True(driver.MainThreadTick(0.016) == 0 && !driver.DrivingFromMainThread, "callbacks must take over");
            now += 400;
            Assert.True(driver.MainThreadTick(0.016) > 0, "stall detected again after 0.3 s");
        }

        public static void SineFallback(Report r)
        {
            var engine = DeckEngine.CreateSine(Rate);
            var song = new SongBuilder().Tempo(0, 120).Note(0, 16, 0, 69).Note(0, 0.2, 9, 36).Note(1, 1.2, 9, 38).Build();
            var plan = new MorphPlan(3, 1, 8);
            engine.Post(Job(1, song, 0, 16, plan));
            var l = Render(engine, 6).L;
            double start = Dsp.Pitch(l, (int)(0.6 * Rate), 2048, Rate), end = Dsp.Pitch(l, (int)(4.6 * Rate), 2048, Rate);
            double expStart = 440 * Math.Pow(2, plan.Semitones(1.2) / 12);
            r.Note($"sine fallback: {start:F2} Hz at beat 1.2 (expect {expStart:F2}), {end:F2} Hz after the morph (expect 440)");
            Assert.True(Math.Abs(Dsp.Cents(start, expStart)) < 8 && Math.Abs(Dsp.Cents(end, 440)) < 3, "sine fallback pitch");
            Assert.True(Dsp.Rms(l, 0, Rate / 10) > 1e-3, "drum/tone burst silent");
        }

        // ------------------------------------------------------------------ MIDI reading

        public static void MidiReader(Report r)
        {
            string path = Path.Combine(Path.GetTempPath(), $"musichistory-audio-{Guid.NewGuid():N}.mid");
            try
            {
                var events = new MidiEventCollection(1, 96);
                events.AddTrack();
                events.AddTrack();
                events.AddEvent(new TempoEvent(600000, 0), 0);                   // 100 BPM
                events.AddEvent(new TempoEvent(400000, 192), 0);                 // 150 BPM from beat 2
                events.AddEvent(new TextEvent("placeholder", MetaEventType.Lyric, 48), 0);   // must be ignored
                events.AddEvent(new PatchChangeEvent(0, 2, 33), 1);
                var on = new NoteOnEvent(0, 2, 60, 100, 96);
                events.AddEvent(on, 1);
                events.AddEvent(on.OffEvent, 1);
                events.AddEvent(new NoteOnEvent(96, 2, 62, 90, 0), 1);
                events.AddEvent(new NoteEvent(240, 2, MidiCommandCode.NoteOn, 62, 0), 1);   // velocity-0 note-off
                events.AddEvent(new ControlChangeEvent(120, 2, MidiController.MainVolume, 80), 1);
                events.AddEvent(new PitchWheelChangeEvent(130, 2, 9000), 1);
                events.AddEvent(new NoteOnEvent(288, 10, 36, 110, 24), 1);
                events.AddEvent(new NoteEvent(312, 10, MidiCommandCode.NoteOff, 36, 0), 1);
                events.AddEvent(new NoteOnEvent(300, 2, 64, 80, 0), 1);           // never closed: closed at track end
                events.AddEvent(new MetaEvent(MetaEventType.EndTrack, 0, 0), 0);
                events.AddEvent(new MetaEvent(MetaEventType.EndTrack, 0, 480), 1);
                events.PrepareForExport();
                MidiFile.Export(path, events);
                MidiSong s = MidiSongReader.Read(path);
                r.Note($"notes: {string.Join(", ", s.Notes.Select(n => $"ch{n.Channel + 1}:{n.Key}@{n.On}-{n.Off}"))}");
                Assert.True(s.Notes.Length == 4, "four notes");
                Assert.True(s.Notes[0].Key == 60 && s.Notes[0].On == 0 && s.Notes[0].Off == 1 && s.Notes[0].Channel == 1, "note 60");
                Assert.True(s.Notes.Any(n => n.Key == 62 && n.On == 1 && n.Off == 2.5), "velocity-0 note-off pairs");
                Assert.True(s.Notes.Any(n => n.Key == 36 && n.Channel == 9 && n.On == 3 && n.Off == 3.25), "drum on channel 10");
                Assert.True(s.Notes.Any(n => n.Key == 64 && n.Off == 5), "hanging note closed at the track end");
                Assert.Near(s.Tempo.BpmAt(1), 100, 1e-9, "tempo 1");
                Assert.Near(s.Tempo.BpmAt(3), 150, 1e-9, "tempo 2");
                Assert.Near(s.Tempo.SecondsAt(3), 2 * 0.6 + 0.4, 1e-12, "seconds at beat 3");
                Assert.Near(s.Tempo.BeatAt(1.6), 3, 1e-12, "beat at 1.6 s");
                Assert.True(s.Controls.Select(c => c.Status).SequenceEqual(new byte[] { 0xC1, 0xB1, 0xE1 }), "controls in order");
                Assert.True(((s.Controls[2].Data2 << 7) | s.Controls[2].Data1) == 9000, "pitch wheel value");
            }
            finally
            {
                if (File.Exists(path)) File.Delete(path);
            }
            string? dir = Env.PpTestDir;
            if (dir == null) { r.Note("pp-test folder not found: real-file parse skipped"); return; }
            int ok = 0, failed = 0;
            double worstMs = 0, totalMs = 0;
            foreach (string f in Directory.GetFiles(dir, "*.mid", SearchOption.AllDirectories))
            {
                var sw = Stopwatch.StartNew();
                try
                {
                    MidiSong song = MidiSongReader.Read(f);
                    ExcerptBuilder.Build(song, 0, song.EndBeat);
                    ok++;
                }
                catch (Exception)
                {
                    failed++;
                }
                double ms = sw.Elapsed.TotalMilliseconds;
                totalMs += ms;
                worstMs = Math.Max(worstMs, ms);
            }
            r.Note($"real files: {ok} parsed, {failed} failed; parse+excerpt mean {totalMs / Math.Max(1, ok + failed):F1} ms, worst {worstMs:F1} ms (.NET 10, includes JIT on the first)");
            Assert.True(failed == 0, $"{failed} real MIDI files failed to parse");
        }
    }
}
