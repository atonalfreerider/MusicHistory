#nullable enable
using System;
using System.Collections;
using System.IO;
using UnityEngine;
using UnityEngine.Networking;

namespace MusicHistory.Playback
{
    /// <summary>
    /// Plays prerendered recording previews (tools/render_paths.py) instead of MIDI. Each render
    /// already starts in the previous song's key and tempo and glides to its own, so this player
    /// just streams the file: data/audio/renders/&lt;previous work|_start&gt;__&lt;work&gt;.mp3.
    /// The file's progress is mapped linearly onto the excerpt's beats so the HUD, camera and
    /// watchdog behave as with the synth. The walkthrough uses it only when a render exists for
    /// the exact (previous, next) pair and falls back to MIDI otherwise.
    /// </summary>
    public sealed class PreviewSongPlayer : MonoBehaviour, ISongPlayer, IMorphReadout
    {
        [Tooltip("Seconds of fade when a clip is cut by Stop or a new Play.")]
        public float CutFadeSeconds = 0.25f;

        AudioSource? source;
        SongClip? clip;
        MorphPlan plan = MorphPlan.None;
        bool playing;
        bool paused;
        bool loaded;
        int generation;
        float fade;

        public SongClip? Clip => clip;
        public MorphPlan Plan => plan;
        public MorphPlan CurrentPlan => plan;
        public double PlanStartBpm => clip == null ? 0 : (clip.NativeBpm > 0 ? clip.NativeBpm : 120) * plan.StartTempoRatio;
        public float MorphBars { get; set; } = 2f;
        public bool ApplesToApples { get; set; }
        public bool IsPlaying => playing;
        public double CurrentBeat { get; private set; }

        public bool Paused
        {
            get => paused;
            set
            {
                paused = value;
                if (source == null) return;
                if (value) source.Pause(); else if (playing && source.clip != null) source.UnPause();
            }
        }

        public double CurrentSemitones => clip == null ? 0 : plan.Semitones(CurrentBeat - clip.ExcerptStartBeat);
        public double CurrentBpm => clip == null ? 0 : (clip.NativeBpm > 0 ? clip.NativeBpm : 120) * plan.TempoRatio(CurrentBeat - clip.ExcerptStartBeat);

        public event Action<SongClip>? Started;
        public event Action<SongClip>? Finished;

        /// <summary>The render for playing <paramref name="next"/> after <paramref name="previous"/>, or null.</summary>
        public static string? RenderPath(SongClip? previous, SongClip next)
        {
            string? work = WorkId(next);
            string? data = DataDir(next);
            if (work == null || data == null) return null;
            string prev = previous != null ? WorkId(previous) ?? "_start" : "_start";
            string path = Path.Combine(data, "audio", "renders", $"{prev}__{work}.mp3");
            return File.Exists(path) ? path : null;
        }

        public bool Has(SongClip? previous, SongClip next) => RenderPath(previous, next) != null;

        // MidiPath is <data>/songs/<work_id>/score.mid.
        static string? WorkId(SongClip c) =>
            string.IsNullOrEmpty(c.MidiPath) ? null : Path.GetFileName(Path.GetDirectoryName(c.MidiPath));

        static string? DataDir(SongClip c)
        {
            if (string.IsNullOrEmpty(c.MidiPath)) return null;
            string? songDir = Path.GetDirectoryName(c.MidiPath);
            string? songs = songDir != null ? Path.GetDirectoryName(songDir) : null;
            return songs != null ? Path.GetDirectoryName(songs) : null;
        }

        void Awake() => EnsureSource();

        AudioSource EnsureSource()
        {
            if (source != null) return source;
            // Own child object: the MIDI synth's OnAudioFilterRead on this GameObject would
            // otherwise attach to this AudioSource too and overwrite the preview with its output.
            Transform existing = transform.Find("Preview Audio");
            GameObject host = existing != null ? existing.gameObject : new GameObject("Preview Audio");
            host.transform.SetParent(transform, false);
            source = host.GetComponent<AudioSource>();
            if (source == null) source = host.AddComponent<AudioSource>();
            source.playOnAwake = false;
            source.loop = false;
            source.spatialBlend = 0f;
            return source;
        }

        public void Play(SongClip next, SongClip? previous)
        {
            clip = next ?? throw new ArgumentNullException(nameof(next));
            string path = RenderPath(previous, next) ?? throw new FileNotFoundException($"No preview render for '{next.Title}'.");
            plan = Morph.Plan(previous!, next, MorphBars);
            CurrentBeat = next.ExcerptStartBeat;
            paused = false;
            playing = true;
            loaded = false;
            generation++;
            if (source != null && source.isPlaying && fade <= 0f) source.Stop();
            StartCoroutine(LoadAndPlay(path, next, generation));
        }

        IEnumerator LoadAndPlay(string path, SongClip target, int gen)
        {
            using UnityWebRequest req = UnityWebRequestMultimedia.GetAudioClip(new Uri(path).AbsoluteUri, AudioType.MPEG);
            yield return req.SendWebRequest();
            if (gen != generation) yield break;
            if (req.result != UnityWebRequest.Result.Success)
            {
                Debug.LogWarning($"MusicHistory: could not load preview '{path}' ({req.error}).");
                playing = false;
                Finished?.Invoke(target);
                yield break;
            }
            AudioClip audio = DownloadHandlerAudioClip.GetContent(req);
            AudioSource s = EnsureSource();
            if (s.clip != null && s.clip != audio) Destroy(s.clip);
            s.clip = audio;
            s.volume = 1f;
            fade = 0f;
            s.Play();
            loaded = true;
            Debug.Log($"MusicHistory: playing recording preview {Path.GetFileName(path)} for '{target.Title}' ({audio.length:0.0}s).");
            if (paused) s.Pause();
            Started?.Invoke(target);
        }

        public void Stop()
        {
            generation++;
            playing = false;
            paused = false;
            plan = MorphPlan.None;
            if (source != null && source.isPlaying) fade = Mathf.Max(0.01f, CutFadeSeconds);
        }

        void Update()
        {
            AudioSource? s = source;
            if (s == null) return;
            if (fade > 0f)
            {
                s.volume = Mathf.Max(0f, s.volume - Time.unscaledDeltaTime / fade);
                if (s.volume <= 0f) { s.Stop(); fade = 0f; s.volume = 1f; }
                return;
            }
            if (!playing || !loaded || paused || clip == null || s.clip == null) return;
            float length = Mathf.Max(0.01f, s.clip.length);
            double u = Math.Min(1.0, s.time / length);
            CurrentBeat = clip.ExcerptStartBeat + (clip.ExcerptEndBeat - clip.ExcerptStartBeat) * u;
            if ((!s.isPlaying && s.time <= 0f) || u >= 0.999)
            {
                CurrentBeat = clip.ExcerptEndBeat;
                playing = false;
                Finished?.Invoke(clip);
            }
        }
    }
}
