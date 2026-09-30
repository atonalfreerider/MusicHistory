#nullable enable
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using MeltySynth;
using MusicHistory.Playback;
using UnityEngine;

namespace MusicHistory.Audio
{
    /// <summary>
    /// The walkthrough's synthesizer: plays <see cref="SongClip"/> excerpts through MeltySynth
    /// (General MIDI SoundFont) with the key/BPM morph of <see cref="Morph.Plan"/>.
    ///
    /// Rendering happens in <see cref="OnAudioFilterRead"/> on Unity's audio thread, kept alive by
    /// a silent looping clip on the required AudioSource; that thread only runs the
    /// engine-agnostic <see cref="DeckEngine"/> (no Unity API there). SoundFont loading and MIDI
    /// parsing run on worker threads, finished jobs reach the audio thread through the engine's
    /// queue, and <see cref="Started"/> / <see cref="Finished"/> are raised from
    /// <see cref="Update"/> on the main thread by polling the engine's published state.
    ///
    /// The clock never stops: without a SoundFont a sine fallback synth plays (one warning), and
    /// when Unity delivers no audio callbacks (batchmode, audio disabled) the main thread drives
    /// the engine silently (<see cref="EngineDriver"/>). With <see cref="FollowTimeScale"/> the
    /// music follows Time.timeScale like the silent player does (same pitch, faster clock).
    /// </summary>
    [RequireComponent(typeof(AudioSource))]
    [DisallowMultipleComponent]
    [DefaultExecutionOrder(-100)]   // raise Finished before the director's Update in the same frame
    public sealed class SongPlayer : MonoBehaviour, ISongPlayer
    {
        public const string DefaultSoundFontName = "MS_Basic.sf2";

        [Tooltip("SF2 to load. Empty = $MUSICHISTORY_DATA/soundfonts, then <repo>/data/soundfonts/MS_Basic.sf2, then StreamingAssets/SoundFonts/MS_Basic.sf2.")]
        public string SoundFontPath = "";

        [SerializeField, Tooltip("Bars over which a clip glides from the previous song's key/BPM to its own.")]
        float morphBars = 4f;

        [SerializeField, Tooltip("Play the normalized files (C major / A minor, 120 BPM) without morphing.")]
        bool applesToApples;

        [Tooltip("BPM of the normalized files (graph_meta target_bpm); used when a clip has no normalized file.")]
        public double TargetBpm = 120;

        [Range(0f, 2f)] public float Volume = 1f;

        [Tooltip("MeltySynth reverb and chorus (about a fifth of the synth's CPU). Applied when the SoundFont loads.")]
        public bool ReverbAndChorus = true;

        [Tooltip("Where the next clip enters after a clip ends: next bar line of the old pulse (default), next beat, or at once.")]
        public HandoffGrid Handoff = HandoffGrid.Bar;

        [Tooltip("How long the outgoing clip's release/reverb rings under the next one.")]
        [Range(0.2f, 6f)] public float TailSeconds = 2f;

        [Tooltip("Scale the musical clock by Time.timeScale (0 pauses), like the silent player.")]
        public bool FollowTimeScale = true;

        public bool VerboseLog;

        public event Action<SongClip>? Started;
        public event Action<SongClip>? Finished;

        public float MorphBars
        {
            get => morphBars;
            set => morphBars = Mathf.Max(0f, value);
        }

        public bool ApplesToApples
        {
            get => applesToApples;
            set => applesToApples = value;
        }

        public bool Paused
        {
            get => paused;
            set
            {
                paused = value;
                DeckEngine? e = driver.Engine;
                if (e != null) e.Paused = value;
            }
        }

        public bool IsPlaying => clip != null && playing;
        public double CurrentBeat => beat;
        public double CurrentSemitones => semitoneOffset + semitones;
        public double CurrentBpm => bpm;

        /// <summary>The SoundFont file in use (null while loading or with the sine fallback).</summary>
        public string? LoadedSoundFont { get; private set; }
        public bool UsingFallbackSynth { get; private set; }
        public bool Ready => driver.Engine != null;
        /// <summary>True while no audio callbacks arrive and the main thread keeps the clock.</summary>
        public bool SilentClock => driver.DrivingFromMainThread;

        // ------------------------------------------------------------------ state

        readonly EngineDriver driver = new EngineDriver();
        volatile SoundFont? soundFont;
        int sampleRate;
        AudioSource? source;
        AudioClip? carrier;
        bool warnedSilentClock;

        SongClip? clip;
        long jobId;                          // id of the clip currently requested (main thread)
        long requestCounter;                 // last id handed out (read by workers)
        volatile PlaybackJob? lastJob;       // re-posted after an audio device change
        PlaybackJob? pendingJob;             // built, waiting for the engine (CAS, newest id wins)
        bool playing, paused, startedRaised, finishedRaised;
        double beat, semitones, bpm, semitoneOffset;

        readonly ConcurrentQueue<string> warnings = new ConcurrentQueue<string>();

        static readonly object CacheLock = new object();
        static readonly Dictionary<string, MidiSong> SongCache = new Dictionary<string, MidiSong>(StringComparer.OrdinalIgnoreCase);
        static readonly Queue<string> CacheOrder = new Queue<string>();
        const int CacheSize = 16;

        // ------------------------------------------------------------------ Unity lifecycle

        void Awake()
        {
            sampleRate = AudioSettings.outputSampleRate > 0 ? AudioSettings.outputSampleRate : 48000;
            source = GetComponent<AudioSource>();
            source.playOnAwake = false;
            source.loop = true;
            source.spatialBlend = 0f;
            source.volume = 1f;
            // A silent looping clip keeps the AudioSource, and so OnAudioFilterRead, running.
            carrier = AudioClip.Create("SongPlayer carrier", sampleRate, 1, sampleRate, false);
            source.clip = carrier;
            source.Play();
            AudioSettings.OnAudioConfigurationChanged += OnAudioConfigurationChanged;
            BeginLoad(ResolveSoundFontPath());
        }

        void OnDestroy()
        {
            AudioSettings.OnAudioConfigurationChanged -= OnAudioConfigurationChanged;
            driver.Engine = null;
            if (source != null) source.Stop();
            if (carrier != null) Destroy(carrier);
        }

        void Update()
        {
            while (warnings.TryDequeue(out string? w)) Debug.LogWarning(w);
            Exception? error = driver.TakeError();
            if (error != null) Debug.LogError($"MusicHistory.Audio.SongPlayer: rendering stopped: {error}");
            TryPostPending();
            DeckEngine? e = driver.Engine;
            if (e == null) return;
            e.Settings.MasterGain = Volume;
            e.Settings.Handoff = Handoff;
            e.Settings.TailSeconds = TailSeconds;
            e.Settings.TailFadeSeconds = Math.Min(0.5, TailSeconds * 0.5);
            e.TimeScale = FollowTimeScale ? Time.timeScale : 1f;
            if (driver.MainThreadTick(Time.unscaledDeltaTime) > 0 && !warnedSilentClock)
            {
                warnedSilentClock = true;
                Debug.LogWarning("MusicHistory.Audio.SongPlayer: no audio callbacks (audio disabled or batchmode?); keeping the clip clock on the main thread, silently.");
            }

            SongClip? c = clip;
            if (c == null) return;
            EngineState s = e.ReadState();
            long id = jobId;
            if (s.ActiveJobId == id || s.FinishedJobId == id)
            {
                beat = s.Beat;
                semitones = s.Semitones;
                bpm = s.Bpm;
            }
            if (!startedRaised && s.StartedJobId == id)
            {
                startedRaised = true;
                Started?.Invoke(c);
            }
            if (!finishedRaised && s.FinishedJobId == id && jobId == id)
            {
                finishedRaised = true;
                playing = false;
                Finished?.Invoke(c);
            }
        }

        void OnAudioFilterRead(float[] data, int channels) => driver.AudioCallback(data, channels);

        // ------------------------------------------------------------------ ISongPlayer

        public void Play(SongClip next, SongClip? previous)
        {
            if (next == null) throw new ArgumentNullException(nameof(next));
            bool apples = applesToApples;
            string? path = null;
            double constantSemitones = 0, fixedBpm = 0, offset = 0;
            if (apples && !string.IsNullOrEmpty(next.NormalizedMidiPath) && File.Exists(next.NormalizedMidiPath))
            {
                path = next.NormalizedMidiPath;
                offset = next.NormShift;           // the file itself is already transposed
            }
            else if (!string.IsNullOrEmpty(next.MidiPath) && File.Exists(next.MidiPath))
            {
                path = next.MidiPath;
                if (apples)
                {
                    // No normalized file: transpose and re-tempo the native one instead.
                    constantSemitones = next.NormShift;
                    fixedBpm = TargetBpm > 0 ? TargetBpm : 120;
                }
            }
            if (path == null) throw new FileNotFoundException($"MIDI file for '{next.Title}' not found", next.MidiPath);

            MorphPlan plan = apples ? MorphPlan.None : Morph.Plan(previous!, next, morphBars);
            long id = Interlocked.Increment(ref requestCounter);
            jobId = id;
            clip = next;
            playing = true;
            startedRaised = finishedRaised = false;
            Paused = false;
            semitoneOffset = offset;
            beat = next.ExcerptStartBeat;
            semitones = constantSemitones + plan.StartSemitones;
            bpm = (fixedBpm > 0 ? fixedBpm : next.NativeBpm > 0 ? next.NativeBpm : 120) * plan.StartTempoRatio;
            DeckEngine? e = driver.Engine;
            if (e != null) e.LatestRequestId = id;

            var request = new Request(id, next, path, plan, constantSemitones, fixedBpm, previous != null);
            MidiSong? cached = FromCache(path);
            if (cached != null) Submit(Build(request, cached));
            else Task.Run(() => Submit(Build(request, LoadSong(request))));
        }

        public void Stop()
        {
            long id = Interlocked.Increment(ref requestCounter);
            jobId = id;
            playing = false;
            Paused = false;
            DeckEngine? e = driver.Engine;
            if (e != null)
            {
                e.LatestRequestId = id;
                e.PostStop();
            }
        }

        // ------------------------------------------------------------------ jobs

        sealed class Request
        {
            public readonly long Id;
            public readonly SongClip Clip;
            public readonly string Path;
            public readonly MorphPlan Plan;
            public readonly double ConstantSemitones, FixedBpm;
            public readonly bool Align;

            public Request(long id, SongClip clip, string path, MorphPlan plan, double constantSemitones, double fixedBpm, bool align)
            {
                Id = id;
                Clip = clip;
                Path = path;
                Plan = plan;
                ConstantSemitones = constantSemitones;
                FixedBpm = fixedBpm;
                Align = align;
            }
        }

        static PlaybackJob Build(Request r, MidiSong song)
        {
            ExcerptEvents excerpt = ExcerptBuilder.Build(song, r.Clip.ExcerptStartBeat, r.Clip.ExcerptEndBeat);
            return new PlaybackJob(r.Id, excerpt, song.Tempo, r.Plan, r.Clip.BeatsPerBar, r.Align, r.ConstantSemitones, r.FixedBpm, r.Clip);
        }

        MidiSong LoadSong(Request r)
        {
            try
            {
                var sw = System.Diagnostics.Stopwatch.StartNew();
                MidiSong song = MidiSongReader.Read(r.Path);
                AddToCache(r.Path, song);
                if (VerboseLog) warnings.Enqueue($"SongPlayer: parsed {Path.GetFileName(r.Path)} in {sw.Elapsed.TotalMilliseconds:F0} ms");
                return song;
            }
            catch (Exception ex)
            {
                // Keep the tour's clock: an empty song at the clip's BPM still starts and finishes on time.
                warnings.Enqueue($"MusicHistory.Audio.SongPlayer: cannot read '{r.Path}' ({ex.GetType().Name}: {ex.Message}); playing '{r.Clip.Title}' silently.");
                double bpm = r.Clip.NativeBpm > 0 ? r.Clip.NativeBpm : 120;
                return MidiSong.Empty(bpm, r.Clip.ExcerptEndBeat);
            }
        }

        /// <summary>Main thread or worker: park the job (a newer one is never replaced by an older one), then post if possible.</summary>
        void Submit(PlaybackJob job)
        {
            while (true)
            {
                PlaybackJob? current = Volatile.Read(ref pendingJob);
                if (current != null && current.Id >= job.Id) break;
                if (Interlocked.CompareExchange(ref pendingJob, job, current) == current) break;
            }
            TryPostPending();
        }

        void TryPostPending()
        {
            DeckEngine? e = driver.Engine;
            if (e == null || Volatile.Read(ref pendingJob) == null) return;
            PlaybackJob? job = Interlocked.Exchange(ref pendingJob, null);
            if (job == null || job.Id != Interlocked.Read(ref requestCounter)) return;   // superseded
            e.LatestRequestId = job.Id;
            lastJob = job;
            e.Post(job);
        }

        static MidiSong? FromCache(string path)
        {
            lock (CacheLock) return SongCache.TryGetValue(path, out MidiSong? song) ? song : null;
        }

        static void AddToCache(string path, MidiSong song)
        {
            lock (CacheLock)
            {
                if (SongCache.ContainsKey(path)) return;
                SongCache[path] = song;
                CacheOrder.Enqueue(path);
                while (CacheOrder.Count > CacheSize) SongCache.Remove(CacheOrder.Dequeue());
            }
        }

        // ------------------------------------------------------------------ SoundFont / engine

        string ResolveSoundFontPath()
        {
            if (!string.IsNullOrWhiteSpace(SoundFontPath)) return Path.GetFullPath(SoundFontPath);
            // <repo>/unity/Assets -> <repo>/data/soundfonts, where tools/sf3_to_sf2.py writes it.
            string repoCopy = Path.GetFullPath(Path.Combine(Application.dataPath, "..", "..", "data", "soundfonts", DefaultSoundFontName));
            var candidates = new List<string>();
            string? data = Environment.GetEnvironmentVariable("MUSICHISTORY_DATA");
            if (!string.IsNullOrEmpty(data)) candidates.Add(Path.Combine(data, "soundfonts", DefaultSoundFontName));
            candidates.Add(repoCopy);
            candidates.Add(Path.Combine(Application.streamingAssetsPath, "SoundFonts", DefaultSoundFontName));
            foreach (string c in candidates)
                if (File.Exists(c)) return Path.GetFullPath(c);
            return repoCopy;   // reported in the missing-SoundFont warning
        }

        EngineSettings NewSettings() => new EngineSettings
        {
            MasterGain = Volume,
            Handoff = Handoff,
            TailSeconds = TailSeconds,
            TailFadeSeconds = Math.Min(0.5, TailSeconds * 0.5),
        };

        void BeginLoad(string path)
        {
            int rate = sampleRate;
            bool reverb = ReverbAndChorus;
            EngineSettings settings = NewSettings();
            Task.Run(() =>
            {
                DeckEngine created;
                if (File.Exists(path))
                {
                    try
                    {
                        var sw = System.Diagnostics.Stopwatch.StartNew();
                        var sf = new SoundFont(path);
                        created = DeckEngine.CreateMelty(sf, rate, settings, reverb);
                        soundFont = sf;
                        LoadedSoundFont = path;
                        if (VerboseLog) warnings.Enqueue($"SongPlayer: loaded {path} in {sw.Elapsed.TotalSeconds:F1} s");
                    }
                    catch (Exception ex)
                    {
                        warnings.Enqueue($"MusicHistory.Audio.SongPlayer: could not load SoundFont '{path}' ({ex.GetType().Name}: {ex.Message}); using the sine fallback synth.");
                        created = DeckEngine.CreateSine(rate, settings);
                        UsingFallbackSynth = true;
                    }
                }
                else
                {
                    warnings.Enqueue($"MusicHistory.Audio.SongPlayer: SoundFont not found at '{path}'. Run `python tools/sf3_to_sf2.py` " +
                                     "to create data/soundfonts/MS_Basic.sf2 (or set SoundFontPath). Using the sine fallback synth.");
                    created = DeckEngine.CreateSine(rate, settings);
                    UsingFallbackSynth = true;
                }
                Install(created);
            });
        }

        void Install(DeckEngine created)
        {
            created.Paused = paused;
            created.LatestRequestId = Interlocked.Read(ref requestCounter);
            driver.Engine = created;
            TryPostPending();
        }

        void OnAudioConfigurationChanged(bool deviceWasChanged)
        {
            int rate = AudioSettings.outputSampleRate;
            if (rate == sampleRate || rate <= 0) return;
            sampleRate = rate;
            if (source != null)
            {
                if (carrier != null) Destroy(carrier);
                carrier = AudioClip.Create("SongPlayer carrier", rate, 1, rate, false);
                source.clip = carrier;
                source.loop = true;
                source.Play();
            }
            SoundFont? sf = soundFont;
            DeckEngine rebuilt = sf != null ? DeckEngine.CreateMelty(sf, rate, NewSettings(), ReverbAndChorus) : DeckEngine.CreateSine(rate, NewSettings());
            Install(rebuilt);
            // Jobs do not depend on the sample rate: restart the current clip on the new engine.
            PlaybackJob? job = lastJob;
            if (job != null && playing && job.Id == jobId) rebuilt.Post(job);
        }
    }
}
