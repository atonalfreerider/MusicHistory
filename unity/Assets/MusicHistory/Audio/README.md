# MusicHistory.Audio: SongPlayer

`SongPlayer` (namespace `MusicHistory.Audio`) is the real `ISongPlayer`
(`Assets/MusicHistory/Contracts/ISongPlayer.cs`). It plays each song's excerpt through
MeltySynth with a General MIDI SoundFont. Each song starts in the key and BPM of the song
before it, then glides to its own over `MorphBars` bars (docs/DESIGN.md §11).
`SongPlayerDiscovery` finds it by type name and adds it next to the `WalkthroughDirector`.
Nothing else needs wiring.

## Setup

1. Convert the SoundFont once. This writes about 490 MB to `data/soundfonts/MS_Basic.sf2`
   and copies its MIT license file next to it:
   ```
   .venv\Scripts\python.exe tools\sf3_to_sf2.py
   ```
   The source is `C:\Program Files\MuseScore 4\sound\MS Basic.sf3`. Use `--input` and
   `--output` to change the paths. Details are under **SoundFont** below.
2. That's all. The player looks for the SoundFont in this order:
   - `SoundFontPath` (inspector field)
   - `$MUSICHISTORY_DATA/soundfonts/MS_Basic.sf2`
   - `<repo>/data/soundfonts/MS_Basic.sf2`, resolved as `Application.dataPath/../../data/...`
   - `StreamingAssets/SoundFonts/MS_Basic.sf2`, for standalone builds

   If none exists, it logs one warning and plays a sine-organ fallback, so the tour's
   timing still works.

## Files

| File | What it does |
|---|---|
| `SongPlayer.cs` | MonoBehaviour, `ISongPlayer`. Needs an AudioSource and plays a silent looping clip so `OnAudioFilterRead` keeps running. Loads the SF2 and parses MIDI on worker threads, hands jobs to the audio thread by reference, and raises `Started`/`Finished` from `Update` (execution order −100, so they come before the director's `Update` in the same frame). |
| `Core/MidiSong.cs` | NAudio.Midi reader in lenient mode. Produces paired notes, controllers, programs, bends and pressure on the beat axis, plus the tempo map. Lyric and text events are never read. |
| `Core/TempoMap.cs` | Piecewise-constant tempo: beats ↔ seconds, next change after a beat, beat-weighted median BPM. |
| `Core/Excerpt.cs` | Builds the excerpt window `[start, end)`. The *chase* restores the controller and program state reached before `start`, so instruments are right mid-song: last value per plain controller, (N)RPN sequences and Reset All Controllers kept in order, bank before program. Notes played up to 1/8 beat early are moved onto the downbeat. Notes held across `start` are re-struck. Notes still sounding at `end` get note-offs there. |
| `Core/PlaybackJob.cs` | One clip ready to play: excerpt, tempo map, `MorphPlan`. `Advance(beat, seconds)` integrates `rate(b) = ratio(b) / secondsPerBeat(b)` with the midpoint rule, and is exact across tempo changes. |
| `Core/DeckEngine.cs` | Two decks, 64-frame blocks, handoff (details below). |
| `Core/MeltyDeckSynth.cs` | MeltySynth `Synthesizer` with continuous transposition through RPN 2 (coarse) and RPN 1 (fine, 1/8192 semitone) on every channel except 10. Details below. |
| `Core/SineDeckSynth.cs` | Fallback synth. A port of Resonance-2's `MusicSynth` idea: sine voices, simple noise/tone drums, volume, expression, pan, sustain and bend. |
| `Core/EngineDriver.cs` | Decides who renders. Normally `OnAudioFilterRead` does. If no audio callback arrives for 0.3 s (batchmode, audio disabled), the main thread renders the elapsed time silently, so `CurrentBeat` and `Finished` keep working. A compare-and-swap flag stops both threads from rendering at once, and the audio thread never waits. |

**`DeckEngine` details.** For each 64-frame block:
- it computes the beat reached at the block's middle and end from the tempo integral;
- it sends every event due before the block's middle, so each event goes out at the block
  boundary nearest its time (error at most 0.67 ms at 48 kHz, and no drift);
- it sets the morph transposition, then renders.

When a clip ends:
- its deck keeps ringing for `TailSeconds` (2 s, fading over the last 0.5 s);
- the next clip starts on the other deck on the next **bar line** of the finished clip's
  continued pulse;
- if the next `Play` arrives within `LateJoinSeconds` (0.12 s) of that line, which is the
  normal Finished → director → Play round trip, the clip joins at once, already that far into
  its first beat. The pulse stays continuous and only the first onsets are late.

`Stop` or a new `Play` in the middle of a clip fades the current clip out over 0.35 s.
Pausing releases the sounding notes and freezes the beat. Resuming strikes the held notes
again.

With `FollowTimeScale` (on by default), the musical clock is multiplied by
`Time.timeScale`, the same way the silent player's clock is:
- `2` plays twice as fast at the same pitch;
- `0` pauses;
- the reported BPM stays the musical one.

The viewer's PlayModeBench runs tours at ×30, and with this they still advance on
`Finished`.

**`MeltyDeckSynth` details.** Transposition was checked against MeltySynth 2.4.1: voices that
are already sounding are re-pitched every block, within 0.85 cents. The song's own RPN 1/2
values are captured and added to the transposition instead of being overwritten. After each
write, the song's RPN selection is restored.

Everything under `Core/` is plain C# 9 with no UnityEngine, and the audio-thread path does
not allocate.

## Morph semantics (ISongPlayer)

- `Play(clip, previous)` builds `Morph.Plan(previous, clip, MorphBars)`:
  - transposition starts at `wrap(prevTonic − tonic)`;
  - tempo ratio starts at `P′/N` (the previous BPM folded by ×½/×1/×2);
  - both glide to native with a smoothstep over `MorphBars × BeatsPerBar` beats;
  - with `previous == null`, the clip plays natively.
- Tempo: the playback BPM is the file's own tempo at the current beat × `TempoRatio`. Songs
  with tempo maps keep their own rubato and changes.
- `CurrentBeat` is the position in the file's own beats, starting at `ExcerptStartBeat`.
  `CurrentSemitones` and `CurrentBpm` are what is sounding now.
- `ApplesToApples` plays `NormalizedMidiPath` natively, with no morph, and reports
  `CurrentSemitones = NormShift`. If a clip has no normalized file, the native file is played
  at `+NormShift` and `TargetBpm` instead.
- A missing MIDI file makes `Play` throw `FileNotFoundException`, and the director then uses
  its silent player. If a file cannot be parsed, the player logs a warning and plays silence
  on the clip's clock, so `Finished` still arrives on time.

## Tests (no Unity needed)

`unity/PlayerCore.Tests` compiles `Core/*.cs` and `ISongPlayer.cs` as C# 9 against the same
DLLs Unity uses, and checks:

- tuning;
- the glide frequency;
- that drums are never transposed;
- time-scale following, and the main-thread clock used when no audio callbacks arrive;
- onset timing against the tempo integral (within 1 ms, no drift over 64 bars, and a real
  MIDI with 700 tempo changes);
- morph math;
- the chase;
- handoff and tails;
- pause and stop;
- the sine fallback;
- MIDI reading;
- CPU cost.

It also renders a demo WAV to `data/screens/`.

```
cd unity\PlayerCore.Tests
dotnet run -c Release                 # all checks (the SoundFont checks skip if the SF2 is missing)
dotnet run -c Release -- tempo chase  # only checks whose names contain these words
dotnet build UnityCompileCheck        # compiles SongPlayer.cs + Core against UnityEngine*.dll / netstandard 2.1
```

## Known limits

- MeltySynth ignores SoundFont modulators. `sf3_to_sf2.py` therefore bakes the velocity- and
  key-driven modulators into generators; otherwise MS Basic's pianos render about 30 dB too
  quiet and muffled. Brightness no longer follows velocity. Loudness still does, through
  MeltySynth's own velocity curve.
- The decoded SF2 is about 490 MB, and MeltySynth keeps all of it in memory. Loading takes
  0.3–1 s on a worker thread.
- CPU was measured under .NET 10 on this machine. Unity's Mono JIT is probably 2–4× slower
  (an estimate, not measured). Turning off `ReverbAndChorus` saved about 20 % in the
  two-deck stress test.
