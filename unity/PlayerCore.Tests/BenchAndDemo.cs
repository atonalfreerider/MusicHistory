#nullable enable
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using MusicHistory.Playback;

namespace MusicHistory.Audio.Tests
{
    public static class BenchAndDemo
    {
        const int Rate = 48000;

        sealed class RealSong
        {
            public string Name = "";
            public MidiSong Song = null!;
            public SongClip Clip = null!;
        }

        /// <summary>
        /// Two real test MIDIs with the facts the graph would carry (keys from the research
        /// prototypes' Resonance analysis, converted to C = 0; BPM measured from the tempo map).
        /// </summary>
        static (RealSong A, RealSong B) Pair()
        {
            string? dir = Env.PpTestDir;
            if (dir == null) throw new SkipCheck("pp-test MIDI folder not found");
            RealSong Load(string folder, string title, int tonic, double start, double end)
            {
                string path = Path.Combine(dir, folder, "aligned.mid");
                if (!File.Exists(path)) throw new SkipCheck($"{path} missing");
                MidiSong song = MidiSongReader.Read(path);
                double bpm = song.Tempo.MedianBpm(0, song.EndBeat);
                return new RealSong
                {
                    Name = title,
                    Song = song,
                    Clip = new SongClip { Title = title, TonicPc = tonic, NativeBpm = bpm, BeatsPerBar = 4, ExcerptStartBeat = start, ExcerptEndBeat = end },
                };
            }
            // Ticket to Ride: A major (chorus at beat 68); Umbrella: C#/Db major (chorus at beat 192).
            return (Load("Ticket-to-Ride---The-Beatles", "Ticket to Ride", 9, 68, 84),
                    Load("Umbrella-bf6a6793-5b304c", "Umbrella", 1, 192, 212));
        }

        static PlaybackJob Job(long id, RealSong s, MorphPlan plan, bool align) =>
            new PlaybackJob(id, ExcerptBuilder.Build(s.Song, s.Clip.ExcerptStartBeat, s.Clip.ExcerptEndBeat), s.Song.Tempo, plan, s.Clip.BeatsPerBar, align, tag: s.Clip);

        /// <summary>
        /// Plays A's excerpt, then B's with the key/BPM morph, the way the walkthrough does it:
        /// Play(B, A) arrives ~40 ms after A's end (main-thread round trip), then renders to a WAV.
        /// </summary>
        public static void DemoWav(Report r)
        {
            var (a, b) = Pair();
            MorphPlan plan = Morph.Plan(a.Clip, b.Clip, 4);
            r.Note($"{a.Name}: tonic {a.Clip.TonicPc}, {a.Clip.NativeBpm:F1} BPM, beats {a.Clip.ExcerptStartBeat}-{a.Clip.ExcerptEndBeat}");
            r.Note($"{b.Name}: tonic {b.Clip.TonicPc}, {b.Clip.NativeBpm:F1} BPM, beats {b.Clip.ExcerptStartBeat}-{b.Clip.ExcerptEndBeat}");
            r.Note($"morph plan: start {plan.StartSemitones:+0;-0} semitones, tempo ratio {plan.StartTempoRatio:F3} (starts at {b.Clip.NativeBpm * plan.StartTempoRatio:F1} BPM), over {plan.MorphBeats} beats");
            var engine = DeckEngine.CreateMelty(Env.SoundFont, Rate);
            var log = new EventLog();
            engine.Observer = log;
            engine.Post(Job(1, a, MorphPlan.None, false));
            double seconds = 26;
            int frames = (int)(seconds * Rate);
            var left = new float[frames];
            var right = new float[frames];
            int latencyBuffers = -1;
            var semis = new List<(double T, double S, double Bpm)>();
            for (int i = 0; i < frames; i += 1024)
            {
                int n = Math.Min(1024, frames - i);
                engine.RenderStereo(left, right, i, n);
                EngineState s = engine.ReadState();
                if (s.FinishedJobId == 1 && latencyBuffers < 0) latencyBuffers = 0;
                else if (latencyBuffers >= 0 && ++latencyBuffers == 2)
                    engine.Post(Job(2, b, plan, true));
                if (s.ActiveJobId == 2) semis.Add(((i + n) / (double)Rate, s.Semitones, s.Bpm));
            }
            string outDir = Path.Combine(Env.DataDir, "screens");
            string wav = Path.Combine(outDir, "morph_demo_ticket_to_ride_to_umbrella.wav");
            Dsp.WriteWav(wav, left, right, frames, Rate);
            double endA = log.Finished.First(f => f.Job == 1).Sample / Rate;
            double startB = log.Started.First(s => s.Job == 2).Sample / (double)Rate;
            r.Note($"A ends at {endA:F3} s; B joins at {startB:F3} s ({(startB - endA) * 1000:F1} ms late join, pulse kept)");
            foreach (double t in new[] { 0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0 })
            {
                var p = semis.FirstOrDefault(x => x.T >= startB + t);
                if (p.T > 0) r.Note($"  B +{t,4:F1} s: {p.S:+0.000;-0.000;0.000} st, {p.Bpm:F1} BPM");
            }
            double peak = 0;
            for (int i = 0; i < frames; i++) peak = Math.Max(peak, Math.Max(Math.Abs(left[i]), Math.Abs(right[i])));
            r.Metric("peak", Dsp.Db(peak), "dBFS", "F1");
            r.Metric("RMS", Dsp.Db(Dsp.Rms(left, 0, frames)), "dBFS", "F1");
            r.Note($"wrote {wav} ({seconds:F0} s, 48 kHz stereo)");
            Assert.True(peak > 0.05 && peak < 1.0, "demo level out of range");
        }

        static (double Mean, double P99, double Max) Measure(Action renderBuffer, int buffers)
        {
            var times = new double[buffers];
            for (int i = 0; i < buffers; i++)
            {
                long t0 = Stopwatch.GetTimestamp();
                renderBuffer();
                times[i] = (Stopwatch.GetTimestamp() - t0) * 1000.0 / Stopwatch.Frequency;
            }
            Array.Sort(times);
            return (times.Average(), times[(int)(buffers * 0.99)], times[buffers - 1]);
        }

        /// <summary>CPU per 1024-frame buffer at 48 kHz (budget 21.3 ms), .NET 10 on this machine.</summary>
        public static void CpuCost(Report r)
        {
            var (a, b) = Pair();
            var l = new float[1024];
            var rr = new float[1024];
            int voicesMax = 0;

            // Steady state: one deck playing A's excerpt with reverb/chorus.
            var engine = DeckEngine.CreateMelty(Env.SoundFont, Rate);
            engine.Post(Job(1, a, MorphPlan.None, false));
            for (int i = 0; i < 50; i++) engine.RenderStereo(l, rr, 0, 1024);   // warm-up
            var one = Measure(() =>
            {
                engine.RenderStereo(l, rr, 0, 1024);
                voicesMax = Math.Max(voicesMax, engine.DeckSynth(0).ActiveVoices + engine.DeckSynth(1).ActiveVoices);
            }, 300);
            r.Note($"one deck playing (Ticket to Ride chorus): mean {one.Mean:F3} ms, p99 {one.P99:F3} ms, max {one.Max:F3} ms per 1024 frames; up to {voicesMax} voices");

            // Handoff: A's tail ringing on one deck while B starts (with morph) on the other.
            engine = DeckEngine.CreateMelty(Env.SoundFont, Rate);
            engine.Post(Job(1, a, MorphPlan.None, false));
            while (engine.ReadState().FinishedJobId != 1) engine.RenderStereo(l, rr, 0, 1024);
            engine.Post(Job(2, b, Morph.Plan(a.Clip, b.Clip, 4), true));
            voicesMax = 0;
            var two = Measure(() =>
            {
                engine.RenderStereo(l, rr, 0, 1024);
                voicesMax = Math.Max(voicesMax, engine.DeckSynth(0).ActiveVoices + engine.DeckSynth(1).ActiveVoices);
            }, 90);   // ~1.9 s: the whole overlap
            r.Note($"handoff, both decks rendering (tail + morphing clip): mean {two.Mean:F3} ms, p99 {two.P99:F3} ms, max {two.Max:F3} ms; up to {voicesMax} voices");

            // Worst case: two decks both playing dense material (two engines, one clip each).
            var e1 = DeckEngine.CreateMelty(Env.SoundFont, Rate);
            var e2 = DeckEngine.CreateMelty(Env.SoundFont, Rate);
            e1.Post(Job(1, a, MorphPlan.None, false));
            e2.Post(Job(1, b, new MorphPlan(3, 0.8, 16), false));
            for (int i = 0; i < 20; i++) { e1.RenderStereo(l, rr, 0, 1024); e2.RenderStereo(l, rr, 0, 1024); }
            var both = Measure(() => { e1.RenderStereo(l, rr, 0, 1024); e2.RenderStereo(l, rr, 0, 1024); }, 300);
            r.Note($"stress, two decks both playing (one gliding): mean {both.Mean:F3} ms, p99 {both.P99:F3} ms, max {both.Max:F3} ms");

            // Same stress without MeltySynth's reverb/chorus.
            var d1 = DeckEngine.CreateMelty(Env.SoundFont, Rate, reverbAndChorus: false);
            var d2 = DeckEngine.CreateMelty(Env.SoundFont, Rate, reverbAndChorus: false);
            d1.Post(Job(1, a, MorphPlan.None, false));
            d2.Post(Job(1, b, new MorphPlan(3, 0.8, 16), false));
            for (int i = 0; i < 20; i++) { d1.RenderStereo(l, rr, 0, 1024); d2.RenderStereo(l, rr, 0, 1024); }
            var dry = Measure(() => { d1.RenderStereo(l, rr, 0, 1024); d2.RenderStereo(l, rr, 0, 1024); }, 300);
            r.Note($"stress without reverb/chorus: mean {dry.Mean:F3} ms, p99 {dry.P99:F3} ms, max {dry.Max:F3} ms");

            // Sequencer overhead alone (NullSynth): what the beat integrator + dispatch cost.
            var seq = new DeckEngine(new NullSynth(Rate), new NullSynth(Rate));
            seq.Post(Job(1, b, new MorphPlan(3, 0.8, 16), false));
            var sq = Measure(() => seq.RenderStereo(l, rr, 0, 1024), 300);
            r.Note($"sequencer only (dense tempo map, morph, no synthesis): mean {sq.Mean:F4} ms per 1024 frames");
            r.Metric("two-deck handoff mean share of the 21.3 ms buffer", two.Mean / 21.333 * 100, "%", "F1");
        }
    }
}
