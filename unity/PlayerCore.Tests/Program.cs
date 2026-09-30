#nullable enable
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;

namespace MusicHistory.Audio.Tests
{
    /// <summary>
    /// Console self-test for the audio core. Usage:
    ///   dotnet run -c Release                    all checks, including the CPU benchmark and the demo WAV
    ///   dotnet run -c Release -- tempo morph     only checks whose name contains one of the words
    /// Exit code 0 = every selected check passed (skips are reported, not failures).
    /// </summary>
    public static class Program
    {
        static readonly (string Name, Action<Report> Run)[] Checks =
        {
            ("soundfont_loads_and_renders", CoreChecks.SoundFontRenders),
            ("rpn_tuning_repitches_sounding_voice", CoreChecks.RpnTuning),
            ("morph_glide_frequency", CoreChecks.MorphGlideFrequency),
            ("drums_untransposed", CoreChecks.DrumsUntransposed),
            ("tempo_integral_timing", CoreChecks.TempoIntegralTiming),
            ("no_drift_64_bars", CoreChecks.NoDrift64Bars),
            ("dense_tempo_map_real_midi", CoreChecks.DenseTempoMap),
            ("morph_math", CoreChecks.MorphMath),
            ("excerpt_chase", CoreChecks.ExcerptChase),
            ("handoff_tail", CoreChecks.HandoffTail),
            ("pause_resume", CoreChecks.PauseResume),
            ("stop_silences", CoreChecks.StopSilences),
            ("time_scale_clock", CoreChecks.TimeScaleClock),
            ("driver_without_audio_callbacks", CoreChecks.DriverWithoutAudio),
            ("sine_fallback", CoreChecks.SineFallback),
            ("midi_reader", CoreChecks.MidiReader),
            ("cpu_cost_two_decks_48k", BenchAndDemo.CpuCost),
            ("demo_wav", BenchAndDemo.DemoWav),
        };

        public static int Main(string[] args)
        {
            var filters = args.Where(a => !a.StartsWith("-")).ToArray();
            int passed = 0, failed = 0, skipped = 0;
            foreach (var (name, run) in Checks)
            {
                if (filters.Length > 0 && !filters.Any(f => name.Contains(f, StringComparison.OrdinalIgnoreCase))) continue;
                var report = new Report();
                var sw = Stopwatch.StartNew();
                string status;
                try
                {
                    run(report);
                    status = "PASS";
                    passed++;
                }
                catch (SkipCheck e)
                {
                    status = "SKIP: " + e.Message;
                    skipped++;
                }
                catch (CheckFailed e)
                {
                    status = "FAIL: " + e.Message;
                    failed++;
                }
                catch (Exception e)
                {
                    status = "ERROR: " + e;
                    failed++;
                }
                Console.WriteLine($"[{status.Split(':')[0]}] {name} ({sw.Elapsed.TotalSeconds:F1} s)");
                foreach (string line in report.Lines) Console.WriteLine("    " + line);
                if (status != "PASS") Console.WriteLine("    " + status);
            }
            Console.WriteLine($"{passed} passed, {failed} failed, {skipped} skipped");
            return failed == 0 ? 0 : 1;
        }
    }
}
