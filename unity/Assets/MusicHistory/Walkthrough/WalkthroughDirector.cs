#nullable enable
using System;
using System.Collections.Generic;
using System.IO;
using MusicHistory.Playback;
using MusicHistory.Viewer;
using UnityEngine;
using UnityEngine.InputSystem;

namespace MusicHistory.Walkthrough
{
    /// <summary>
    /// Guided tours through the influence tree (docs/DESIGN.md §11). Each step flies the camera to
    /// frame the song and its tree parent, grows the tree edge from the influencer, and plays the
    /// song's excerpt through the <see cref="ISongPlayer"/>, starting in the key and BPM of the song
    /// heard just before and gliding to its own (<see cref="Morph.Plan"/>). The next step starts
    /// when the player reports the excerpt finished.
    ///
    /// "The song heard just before" is the clip the tour was on when the step changed, whichever
    /// way it moved: the previous tour step on a forward advance, the step the listener left on
    /// B/←, the same song on a restart (Enter, C). Only the tour's first song plays natively.
    ///
    /// Keys: 1/2/3 mode (lineage, subtree, chronological), Enter start (from the clicked song),
    /// Space pause, N or → next, B or ← previous, Esc exit, C toggles "compare normalized".
    /// </summary>
    public sealed class WalkthroughDirector : MonoBehaviour
    {
        public TourMode Mode = TourMode.Lineage;
        [Min(.1f)] public float FlyDuration = 1.8f;
        [Min(.1f)] public float EdgeGrowDuration = 1.6f;
        [Min(0f)] public float MorphBars = 4f;
        [Tooltip("Play the normalized MIDI (graph_meta target_key, target_bpm) without morphing.")]
        public bool ApplesToApples;
        [Min(1f)] public float FramingMargin = 1.35f;
        [Tooltip("Screen area (normalized) a step is framed into, clear of the HUD panels.")]
        public Rect TourViewport = new(.14f, .24f, .72f, .46f);
        [Min(.5f)] public float MinFramingDistance = 7f;
        [Tooltip("Safety net for a player that stops without raising Finished: advance after this many " +
                 "seconds in which its beat did not move (unpaused). Longer than a MIDI parse plus the " +
                 "wait for the previous song's next bar line.")]
        [Min(1f)] public float WatchdogSeconds = 20f;

        public SongGraphLoader Loader = null!;
        public ISongPlayer? Player { get; private set; }
        public SilentSongPlayer Silent { get; private set; } = null!;
        public string PlayerDescription { get; private set; } = "";
        public bool IsTouring { get; private set; }
        /// <summary>The last step's excerpt has ended.</summary>
        public bool TourComplete { get; private set; }
        public int StepIndex { get; private set; }
        public IReadOnlyList<int> Steps => steps;
        public SongClip? CurrentClip { get; private set; }
        /// <summary>The clip heard just before the current one (null = the current one plays natively).</summary>
        public SongClip? PreviousClip { get; private set; }
        /// <summary>The plan the active player actually plays the current clip with (tempo from what was heard).</summary>
        public MorphPlan CurrentPlan => ActivePlayer?.CurrentPlan ?? MorphPlan.None;
        public ISongPlayer? ActivePlayer { get; private set; }
        public bool Flying => flyT < 1f;
        public string HudText { get; private set; } = "";
        /// <summary>Seconds the active player has made no progress (the watchdog's clock).</summary>
        public double StallSeconds => stallSeconds;

        List<int> steps = new();
        Vector3 flyFromPosition, flyToPosition;
        Quaternion flyFromRotation, flyToRotation;
        float flyT = 1f;
        float edgeT = 1f;
        InfluenceEdge? animatedEdge;
        bool advancePending;
        float hudCountdown;
        // Safety net for a synth that stops without raising Finished: time without beat progress.
        double stallSeconds;
        double lastProgressBeat = double.NegativeInfinity;
        (TourMode, SongNode?, bool, string) idleKey;
        bool idleKeyValid;
        const float HudInterval = 1f / 12f;

        public void Initialize(SongGraphLoader loader)
        {
            Loader = loader;
            Silent = SongPlayerDiscovery.Silent(gameObject);
            Silent.MorphBars = MorphBars;
            Player = SongPlayerDiscovery.Discover(gameObject, out string description);
            PlayerDescription = description;
            Silent.Finished -= OnClipFinished;
            Silent.Finished += OnClipFinished;
            if (Player != null && !ReferenceEquals(Player, Silent))
            {
                Player.Finished -= OnClipFinished;
                Player.Finished += OnClipFinished;
            }
            if (double.TryParse(loader.Data?.MetaValue("target_bpm"), System.Globalization.NumberStyles.Float,
                    System.Globalization.CultureInfo.InvariantCulture, out double target) && target > 0)
                Silent.TargetBpm = target;
            RefreshIdleHud(true);
        }

        /// <summary>
        /// Replaces the discovered player (a custom synth, or a probe in validation). Songs whose
        /// MIDI file is missing still play on the silent clock.
        /// </summary>
        public void UsePlayer(ISongPlayer player, string description)
        {
            if (player == null) throw new ArgumentNullException(nameof(player));
            if (Player != null && !ReferenceEquals(Player, Silent)) Player.Finished -= OnClipFinished;
            Player = player;
            PlayerDescription = description;
            if (!ReferenceEquals(player, Silent))
            {
                player.Finished -= OnClipFinished;
                player.Finished += OnClipFinished;
            }
            RefreshIdleHud(true);
        }

        void OnDestroy()
        {
            if (Silent != null) Silent.Finished -= OnClipFinished;
            if (Player != null) Player.Finished -= OnClipFinished;
        }

        void Update()
        {
            HandleInput();
            Tick(Time.unscaledDeltaTime);
        }

        void HandleInput()
        {
            Keyboard? k = Keyboard.current;
            if (k == null || Loader == null || Loader.Data == null) return;
            if (!IsTouring)
            {
                if (k.digit1Key.wasPressedThisFrame || k.numpad1Key.wasPressedThisFrame) Mode = TourMode.Lineage;
                if (k.digit2Key.wasPressedThisFrame || k.numpad2Key.wasPressedThisFrame) Mode = TourMode.Subtree;
                if (k.digit3Key.wasPressedThisFrame || k.numpad3Key.wasPressedThisFrame) Mode = TourMode.Chronological;
                if (k.cKey.wasPressedThisFrame) ApplesToApples = !ApplesToApples;
                if (k.enterKey.wasPressedThisFrame || k.numpadEnterKey.wasPressedThisFrame)
                    StartTour(Mode, Loader.Highlighter.Selected);
                return;
            }
            if (k.escapeKey.wasPressedThisFrame) { Exit(); return; }
            if (k.spaceKey.wasPressedThisFrame) TogglePause();
            if (k.nKey.wasPressedThisFrame || k.rightArrowKey.wasPressedThisFrame) Next();
            if (k.bKey.wasPressedThisFrame || k.leftArrowKey.wasPressedThisFrame) Previous();
            if (k.cKey.wasPressedThisFrame)
            {
                ApplesToApples = !ApplesToApples;
                GoTo(StepIndex);
            }
            if (k.enterKey.wasPressedThisFrame || k.numpadEnterKey.wasPressedThisFrame) GoTo(StepIndex);
        }

        public bool StartTour(TourMode mode, SongNode? from)
        {
            if (Loader.Data == null) return false;
            List<int> planned = TourPlanner.Plan(Loader.Data, mode, from != null ? from.NodeId : null);
            if (planned.Count == 0) return false;
            Mode = mode;
            steps = planned;
            IsTouring = true;
            // A new tour's first song plays natively, whatever was sounding before.
            CurrentClip = null;
            PreviousClip = null;
            SetCameraInput(false);
            GoTo(0);
            return true;
        }

        public void GoTo(int index)
        {
            if (!IsTouring || steps.Count == 0) return;
            // What the listener heard just before this step: the clip being left, whichever way
            // the tour moves (null only at the tour start).
            SongClip? heard = CurrentClip;
            StepIndex = Mathf.Clamp(index, 0, steps.Count - 1);
            advancePending = false;
            TourComplete = false;
            if (animatedEdge != null) animatedEdge.VisibleFraction = 1f;

            SongNode child = Loader.NodeById(steps[StepIndex]);
            SongNode? parent = child.TreeParent;
            Loader.Highlighter.ShowTourStep(child, parent);
            Loader.Hud.ShowSong(child);

            animatedEdge = child.TreeEdge;
            edgeT = 0f;
            if (animatedEdge != null) animatedEdge.VisibleFraction = 0f;

            Camera? cam = Loader.ViewCamera;
            if (cam != null)
            {
                List<(Vector3, float)> items = new() { (child.transform.position, child.Radius * 1.4f) };
                if (parent != null) items.Add((parent.transform.position, parent.Radius * 1.4f));
                (flyToPosition, flyToRotation) = CameraFraming.Frame(cam, items, Loader.Frame.ViewForward, FramingMargin, MinFramingDistance, TourViewport);
                flyFromPosition = cam.transform.position;
                flyFromRotation = cam.transform.rotation;
                flyT = 0f;
            }

            CurrentClip = child.Song.ToClip();
            PreviousClip = heard;
            ISongPlayer chosen = ChoosePlayer(CurrentClip);
            // Stop only when the step moves to another player object. The same player replaces its
            // own clip: one still playing is cut by the new Play (short fade), and one that ended
            // naturally keeps its bar-line handoff grid, which a Stop would throw away.
            if (ActivePlayer != null && !ReferenceEquals(ActivePlayer, chosen)) ActivePlayer.Stop();
            ActivePlayer = chosen;
            ResetWatchdog();
            try
            {
                chosen.MorphBars = MorphBars;
                chosen.ApplesToApples = ApplesToApples;
                chosen.Play(CurrentClip, PreviousClip!);
            }
            catch (Exception e)
            {
                Debug.LogWarning($"MusicHistory: player failed on '{CurrentClip.Title}' ({e.Message}); continuing silently.");
                if (!ReferenceEquals(chosen, Silent))
                {
                    try
                    {
                        chosen.Stop();
                    }
                    catch (Exception stopError)
                    {
                        Debug.LogWarning($"MusicHistory: player could not stop ({stopError.Message}).");
                    }
                }
                ActivePlayer = Silent;
                Silent.MorphBars = MorphBars;
                Silent.ApplesToApples = ApplesToApples;
                Silent.Play(CurrentClip, PreviousClip);
            }
            UpdateTourHud();
        }

        /// <summary>The real player when this clip's file exists, else the silent clock.</summary>
        ISongPlayer ChoosePlayer(SongClip clip)
        {
            string? path = ApplesToApples && !string.IsNullOrEmpty(clip.NormalizedMidiPath) ? clip.NormalizedMidiPath : clip.MidiPath;
            bool fileExists = !string.IsNullOrEmpty(path) && File.Exists(path);
            if (Player != null && !ReferenceEquals(Player, Silent) && fileExists) return Player;
            return Silent;
        }

        void OnClipFinished(SongClip clip)
        {
            if (!IsTouring || !ReferenceEquals(clip, CurrentClip)) return;
            // Advance on the next tick: the player may still be inside its own update.
            advancePending = true;
        }

        public void Next()
        {
            if (!IsTouring) return;
            if (StepIndex + 1 < steps.Count) GoTo(StepIndex + 1);
        }

        public void Previous()
        {
            if (!IsTouring) return;
            GoTo(Mathf.Max(0, StepIndex - 1));
        }

        public void TogglePause()
        {
            if (ActivePlayer != null) ActivePlayer.Paused = !ActivePlayer.Paused;
        }

        public void Exit()
        {
            if (!IsTouring) return;
            ActivePlayer?.Stop();
            ActivePlayer = null;
            if (animatedEdge != null) animatedEdge.VisibleFraction = 1f;
            animatedEdge = null;
            IsTouring = false;
            TourComplete = false;
            advancePending = false;
            flyT = 1f;
            CurrentClip = null;
            PreviousClip = null;
            Loader.Highlighter.EndTour();
            Loader.Hud.ShowSong(Loader.Highlighter.Focus);
            SetCameraInput(true);
            RefreshIdleHud(true);
        }

        void SetCameraInput(bool enabled)
        {
            Camera? cam = Loader.ViewCamera;
            if (cam == null) return;
            CameraControl control = cam.GetComponent<CameraControl>();
            if (control == null) return;
            control.InputEnabled = enabled;
            if (enabled) control.SyncRotationFromTransform();
        }

        void ResetWatchdog()
        {
            stallSeconds = 0;
            lastProgressBeat = double.NegativeInfinity;
        }

        /// <summary>Advances animations by <paramref name="dt"/> seconds (public so edit-mode validation can drive it).</summary>
        public void Tick(float dt)
        {
            if (Loader == null || Loader.Data == null) return;
            if (!IsTouring)
            {
                RefreshIdleHud(false);
                return;
            }
            if (advancePending)
            {
                advancePending = false;
                if (StepIndex + 1 < steps.Count) GoTo(StepIndex + 1);
                else TourComplete = true;
            }

            ISongPlayer? active = ActivePlayer;
            if (active != null && !active.Paused && !TourComplete && !advancePending)
            {
                // Progress, not a precomputed duration: the real player follows the file's tempo map,
                // which can be far slower than its median BPM in places. The clock counts real
                // time, but stands still while Time.timeScale freezes the music.
                double beat = active.CurrentBeat;
                if (beat > lastProgressBeat + 1e-9)
                {
                    lastProgressBeat = beat;
                    stallSeconds = 0;
                }
                else
                {
                    stallSeconds += dt * Math.Min(1f, Math.Max(0f, Time.timeScale));
                }
                bool silentlyStopped = !ReferenceEquals(active, Silent) && !active.IsPlaying && stallSeconds > 3.0;
                if (stallSeconds > WatchdogSeconds || silentlyStopped)
                {
                    Debug.LogWarning($"MusicHistory: '{CurrentClip?.Title}' made no progress for {stallSeconds:0.0} s and did not report Finished; advancing.");
                    advancePending = true;
                }
            }

            Camera? cam = Loader.ViewCamera;
            if (cam != null && flyT < 1f)
            {
                flyT = Mathf.Min(1f, flyT + dt / FlyDuration);
                float u = CameraFraming.SmoothStep01(flyT);
                cam.transform.SetPositionAndRotation(
                    Vector3.Lerp(flyFromPosition, flyToPosition, u),
                    Quaternion.Slerp(flyFromRotation, flyToRotation, u));
            }
            if (animatedEdge != null && edgeT < 1f)
            {
                // The edge starts growing once the flight is half way, so the eye can follow it.
                if (flyT > .5f) edgeT = Mathf.Min(1f, edgeT + dt / EdgeGrowDuration);
                animatedEdge.VisibleFraction = CameraFraming.SmoothStep01(edgeT);
            }
            // The HUD's live key/BPM/beat readout refreshes at 12 Hz (text rebuilds allocate).
            hudCountdown -= dt;
            if (hudCountdown <= 0f)
            {
                hudCountdown = HudInterval;
                UpdateTourHud();
            }
        }

        /// <summary>
        /// The key the normalized file plays <paramref name="clip"/> in: its home key moved by its
        /// norm_shift, so "A minor" under relative normalization and "C minor" under parallel.
        /// </summary>
        public static string NormalizedKeyName(SongClip clip) => SongPalette.KeyName(clip.TonicPc + clip.NormShift, clip.Minor);

        void UpdateTourHud()
        {
            if (CurrentClip == null || steps.Count == 0) return;
            SongNode child = Loader.NodeById(CurrentClip.NodeId);
            SongNode? parent = child.TreeParent;
            ISongPlayer? p = ActivePlayer;
            double length = Math.Max(1e-6, CurrentClip.ExcerptEndBeat - CurrentClip.ExcerptStartBeat);
            double into = p != null ? Math.Max(0, p.CurrentBeat - CurrentClip.ExcerptStartBeat) : 0;
            string state = TourComplete ? "tour complete" : p != null && p.Paused ? "paused" : "playing";
            string playerName = ReferenceEquals(p, Silent)
                ? (Player != null && !ReferenceEquals(Player, Silent) ? "silent (MIDI file missing)" : "silent (no synth)")
                : "synth";
            string channelColor = child.TreeEdge != null ? SongPalette.ToHex(SongPalette.ChannelColor(child.TreeEdge.Channel)) : "#f4f6fb";

            string header = $"<b>WALKTHROUGH</b> · {Mode.ToString().ToLowerInvariant()} · step {StepIndex + 1}/{steps.Count} · {state}" +
                            $" · <color={GraphHud.Muted}>{playerName}</color>";
            string pair = parent != null
                ? $"{GraphHud.Esc(parent.Song.Title)} ({parent.Song.Year}) <color={channelColor}>→</color> <b>{GraphHud.Esc(child.Song.Title)}</b> ({child.Song.Year})" +
                  $" · <color={GraphHud.Muted}>{GraphHud.Esc(child.Song.Artist)}</color>"
                : $"<b>{GraphHud.Esc(child.Song.Title)}</b> ({child.Song.Year}) · <color={GraphHud.Muted}>{GraphHud.Esc(child.Song.Artist)} · root of its tree</color>";
            if (child.TreeEdge != null && !string.IsNullOrEmpty(child.TreeEdge.Record.Evidence))
                pair += $"\n<size=85%><color={GraphHud.Muted}>{GraphHud.Esc(child.TreeEdge.Record.Evidence)}</color></size>";

            string morph;
            if (ApplesToApples)
            {
                morph = $"Key {GraphHud.Esc(child.Song.KeyName)} → {NormalizedKeyName(CurrentClip)} (normalized) · BPM {Fmt(Silent.TargetBpm, "0")} (normalized)";
            }
            else
            {
                // The player's own plan: its tempo ratio is relative to the file's entry tempo, so
                // the start BPM comes from the player when it knows it.
                MorphPlan plan = CurrentPlan;
                int entryTonic = CurrentClip.EntryTonic;
                bool entryMinor = CurrentClip.EntryIsMinor;
                string startKey = SongPalette.KeyName(entryTonic + (int)Math.Round(plan.StartSemitones), entryMinor);
                string nativeKey = SongPalette.KeyName(entryTonic, entryMinor);
                double startBpm = p is IMorphReadout readout && readout.PlanStartBpm > 0
                    ? readout.PlanStartBpm
                    : CurrentClip.NativeBpm * plan.StartTempoRatio;
                double semis = p?.CurrentSemitones ?? plan.StartSemitones;
                double bpm = p?.CurrentBpm ?? startBpm;
                string nowKey = SongPalette.PitchName(entryTonic + (int)Math.Round(semis), entryMinor);
                morph = $"Key {startKey} → {nativeKey} <color={GraphHud.Muted}>(now {nowKey}, {Fmt(semis, "+0.0;-0.0;0.0")} st)</color>" +
                        $" · BPM {Fmt(startBpm, "0.#")} → {Fmt(CurrentClip.NativeBpm, "0.#")}" +
                        $" <color={GraphHud.Muted}>(now {Fmt(bpm, "0.0")})</color>";
            }
            morph += $" · beat {Fmt(into, "0.0")}/{Fmt(length, "0")}";
            string keys = $"<size=85%><color={GraphHud.Muted}>Space pause · N/→ next · B/← back · Esc exit · C {(ApplesToApples ? "native key and tempo" : "compare in C / 120 BPM")}</color></size>";
            string text = $"{header}\n{pair}\n{morph}\n{keys}";
            HudText = text;
            Loader.Hud.ShowTour(text, (float)(into / length), SongPalette.Hex(channelColor));
        }

        void RefreshIdleHud(bool force)
        {
            if (Loader == null || Loader.Hud == null || Loader.Data == null) return;
            SongNode? selected = Loader.Highlighter != null ? Loader.Highlighter.Selected : null;
            (TourMode, SongNode?, bool, string) key = (Mode, selected, ApplesToApples, PlayerDescription);
            if (!force && idleKeyValid && key.Equals(idleKey)) return;
            idleKey = key;
            idleKeyValid = true;
            string from = selected != null
                ? $"from <b>{GraphHud.Esc(selected.Song.Title)}</b>"
                : Mode switch
                {
                    TourMode.Lineage => "along the deepest lineage (click a song to choose)",
                    TourMode.Subtree => "through the largest tree (click a song to choose)",
                    _ => "from the first song (click a song to choose)"
                };
            string text = $"<b>Walkthrough</b> <b>{Mode.ToString().ToLowerInvariant()}</b> <color={GraphHud.Muted}>(1/2/3)</color>" +
                          $" · Enter: play {from}" +
                          $" · <color={GraphHud.Muted}>C {(ApplesToApples ? "in C / 120 BPM" : "native keys")} · {PlayerDescription}</color>";
            HudText = text;
            Loader.Hud.ShowTour(text, 0f, Color.clear);
        }

        static string Fmt(double v, string format) => v.ToString(format, System.Globalization.CultureInfo.InvariantCulture);
    }
}
