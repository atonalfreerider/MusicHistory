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
    /// (General MIDI SoundFont) with the key/BPM morph of <see cref="Morph.Plan"/>. The morph starts
    /// at the tempo actually heard: the mean tempo the previous clip played over its last bar
    /// (<see cref="HandoffTempo"/>), against this file's own tempo over the excerpt's first bar.
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
    public sealed class SongPlayer : MonoBehaviour, ISongPlayer, IMorphReadout
    {
        public const string DefaultSoundFontName = "MS_Basic.sf2";

        [Tooltip("SF2 to load. Empty = $MUSICHISTORY_DATA/soundfonts, then <repo>/data/soundfonts/MS_Basic.sf2, then StreamingAssets/SoundFonts/MS_Basic.sf2.")]
        public string SoundFontPath = "";

        [SerializeField, Tooltip("Bars over which a clip glides from the previous song's key/BPM to its own.")]
        float morphBars = 4f;

        [SerializeField, Tooltip("Play the normalized files (graph_meta target_key / target_bpm) without morphing.")]
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

        /// <summary>
        /// The plan the current clip plays with. Until its MIDI is parsed this is a provisional
        /// plan against the file's median BPM; then the job's own plan (against its first-bar tempo).
        /// </summary>
        public MorphPlan CurrentPlan
        {
            get
            {
                PlaybackJob? built = Volatile.Read(ref builtJob);
                return built != null && built.Id == jobId ? built.Plan : currentPlan;
            }
        }

        /// <summary>BPM at the current clip's first beat under <see cref="CurrentPlan"/> (the heard tempo, folded).</summary>
        public double PlanStartBpm
        {
            get
            {
                PlaybackJob? built = Volatile.Read(ref builtJob);
                return built != null && built.Id == jobId ? built.StartBpm : startBpm;
            }
        }

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
        PlaybackJob? builtJob;               // newest job built (its plan is CurrentPlan)
        bool playing, paused, startedRaised, finishedRaised;
        double beat, semitones, bpm, semitoneOffset;
        MorphPlan currentPlan = MorphPlan.None;
        double startBpm;
        // Last position the engine reported for a job (to measure a clip cut short by Stop).
        long trackedJobId;
        double trackedBeat;

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

            EngineState s = e.ReadState();
            if (s.ActiveJobId != 0)
            {
                trackedJobId = s.ActiveJobId;
                trackedBeat = s.Beat;
            }
            SongClip? c = clip;
            if (c == null) return;
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

            // What the listener heard last, measured before this request replaces it. The final
            // plan needs this file's tempo map (Build); until then a median-based one stands in.
            double heard = !apples && previous != null ? HeardBpm(previous) : 0;
            double entry = fixedBpm > 0 ? fixedBpm : next.NativeBpm > 0 ? next.NativeBpm : 120;
            MorphPlan provisional = apples || previous == null
                ? MorphPlan.None
                : Morph.Plan(previous, next, morphBars, heard > 0 ? heard : previous.NativeBpm, entry);
            DeckEngine? e = driver.Engine;
            // A paused clip is replaced, not resumed: stop it before unpausing.
            if (paused && e != null) e.PostStop();
            long id = Interlocked.Increment(ref requestCounter);
            jobId = id;
            clip = next;
            playing = true;
            startedRaised = finishedRaised = false;
            Paused = false;
            semitoneOffset = offset;
            beat = next.ExcerptStartBeat;
            currentPlan = provisional;
            startBpm = entry * provisional.StartTempoRatio;
            semitones = constantSemitones + provisional.StartSemitones;
            bpm = startBpm;
            if (e != null) e.LatestRequestId = id;

            var request = new Request(id, next, previous, path, apples, morphBars, heard, constantSemitones, fixedBpm);
            MidiSong? cached = FromCache(path);
            if (cached != null) Submit(Build(request, cached));
            else Task.Run(() => Submit(Build(request, LoadSong(request))));
        }

        public void Stop()
        {
            long id = Interlocked.Increment(ref requestCounter);
            jobId = id;
            playing = false;
            currentPlan = MorphPlan.None;
            startBpm = 0;
            DeckEngine? e = driver.Engine;
            if (e != null)
            {
                e.LatestRequestId = id;
                e.PostStop();   // before unpausing, so a paused clip never resumes on its way out
            }
            Paused = false;
        }

        /// <summary>
        /// Mean tempo <paramref name="previous"/> played over the last bar it actually played (to its
        /// end, or to where it was cut), when it was this player's last job; 0 = unknown.
        /// </summary>
        double HeardBpm(SongClip previous)
        {
            PlaybackJob? last = lastJob;
            DeckEngine? e = driver.Engine;
            if (last == null || e == null || !SameClip(last.Tag as SongClip, previous)) return 0;
            EngineState s = e.ReadState();
            double reached;
            if (s.ActiveJobId == last.Id) reached = s.Beat;                  // still playing (or waiting to start)
            else if (s.FinishedJobId == last.Id) reached = last.EndBeat;     // ended naturally
            else if (trackedJobId == last.Id) reached = trackedBeat;         // cut short by Stop
            else return 0;
            return HandoffTempo.HeardBpm(last, reached);
        }

        static bool SameClip(SongClip? a, SongClip? b) =>
            ReferenceEquals(a, b) ||
            (a != null && b != null && a.NodeId != 0 && a.NodeId == b.NodeId &&
             a.ExcerptStartBeat == b.ExcerptStartBeat && a.ExcerptEndBeat == b.ExcerptEndBeat);

        // ------------------------------------------------------------------ jobs

        sealed class Request
        {
            public readonly long Id;
            public readonly SongClip Clip;
            public readonly SongClip? Previous;
            public readonly string Path;
            public readonly bool ApplesToApples;
            public readonly double MorphBars, HeardBpm;
            public readonly double ConstantSemitones, FixedBpm;

            public Request(long id, SongClip clip, SongClip? previous, string path, bool applesToApples, double morphBars,
                           double heardBpm, double constantSemitones, double fixedBpm)
            {
                Id = id;
                Clip = clip;
                Previous = previous;
                Path = path;
                ApplesToApples = applesToApples;
                MorphBars = morphBars;
                HeardBpm = heardBpm;
                ConstantSemitones = constantSemitones;
                FixedBpm = fixedBpm;
            }
        }

        /// <summary>Main thread or worker: the excerpt and the plan against this file's own tempo map.</summary>
        static PlaybackJob Build(Request r, MidiSong song)
        {
            ExcerptEvents excerpt = ExcerptBuilder.Build(song, r.Clip.ExcerptStartBeat, r.Clip.ExcerptEndBeat);
            MorphPlan plan = r.ApplesToApples ? MorphPlan.None : HandoffTempo.Plan(r.Previous, r.Clip, r.MorphBars, r.HeardBpm, song.Tempo);
            return new PlaybackJob(r.Id, excerpt, song.Tempo, plan, r.Clip.BeatsPerBar, r.Previous != null,
                                   r.ConstantSemitones, r.FixedBpm, r.Clip);
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
                PlaybackJob? built = Volatile.Read(ref builtJob);
                if (built != null && built.Id >= job.Id) break;
                if (Interlocked.CompareExchange(ref builtJob, job, built) == built) break;
            }
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
