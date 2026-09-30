#nullable enable
using System;
using System.Collections.Generic;
using System.IO;
using NAudio.Midi;

namespace MusicHistory.Audio
{
    /// <summary>
    /// A MIDI file reduced to what playback needs: paired notes, the other channel messages
    /// (controllers, programs, bends, channel pressure) and the tempo map, all in beats.
    /// Immutable once built, so the audio thread can read it without locks.
    /// </summary>
    public sealed class MidiSong
    {
        public readonly int Ppq;
        public readonly TempoMap Tempo;
        /// <summary>Sorted by onset.</summary>
        public readonly NoteSpan[] Notes;
        /// <summary>Non-note channel messages, in file order within a beat (stable sort).</summary>
        public readonly MidiMessage[] Controls;
        public readonly double EndBeat;

        public MidiSong(int ppq, TempoMap tempo, NoteSpan[] notes, MidiMessage[] controls, double endBeat)
        {
            Ppq = ppq;
            Tempo = tempo;
            Notes = notes;
            Controls = controls;
            EndBeat = endBeat;
        }

        /// <summary>A song with no events (used when a file cannot be read, so timing still works).</summary>
        public static MidiSong Empty(double bpm, double endBeat) =>
            new MidiSong(480, TempoMap.Constant(bpm), Array.Empty<NoteSpan>(), Array.Empty<MidiMessage>(), endBeat);
    }

    /// <summary>
    /// Reads Standard MIDI Files with NAudio.Midi in lenient mode (web MIDI files are messy).
    /// Meta events other than tempo are ignored; in particular lyric and text events are never
    /// looked at, so their text is dropped with the parser's objects.
    /// </summary>
    public static class MidiSongReader
    {
        /// <summary>Shortest note kept (beats); zero-length notes would start and stop in one block.</summary>
        public const double MinNoteBeats = 1.0 / 128;

        public static MidiSong Read(string path)
        {
            using (var stream = File.OpenRead(path))
                return Read(stream);
        }

        public static MidiSong Read(Stream stream)
        {
            var file = new MidiFile(stream, false);
            int ppq = file.DeltaTicksPerQuarterNote;
            if (ppq <= 0 || ppq >= 0x8000) throw new InvalidDataException("SMPTE time division is not supported");
            double perTick = 1.0 / ppq;

            var notes = new List<NoteSpan>();
            var controls = new List<(MidiMessage Message, long Seq)>();
            var tempos = new List<KeyValuePair<double, double>>();
            long seq = 0;
            double end = 0;
            var open = new Dictionary<int, Queue<(long Tick, int Velocity)>>();

            for (int t = 0; t < file.Tracks; t++)
            {
                open.Clear();
                long lastTick = 0;
                foreach (MidiEvent e in file.Events[t])
                {
                    long tick = e.AbsoluteTime;
                    if (tick > lastTick) lastTick = tick;
                    double beat = tick * perTick;
                    int ch = Math.Max(1, Math.Min(16, e.Channel)) - 1;
                    switch (e)
                    {
                        case NoteEvent note when note.CommandCode == MidiCommandCode.NoteOn || note.CommandCode == MidiCommandCode.NoteOff:
                        {
                            int key = note.NoteNumber & 0x7F;
                            int id = (ch << 7) | key;
                            bool on = note.CommandCode == MidiCommandCode.NoteOn && note.Velocity > 0;
                            if (on)
                            {
                                if (!open.TryGetValue(id, out var q)) open[id] = q = new Queue<(long, int)>();
                                q.Enqueue((tick, note.Velocity));
                            }
                            else if (open.TryGetValue(id, out var q) && q.Count > 0)
                            {
                                var (onTick, vel) = q.Dequeue();
                                AddNote(notes, onTick * perTick, beat, ch, key, vel);
                            }
                            break;
                        }
                        case ControlChangeEvent cc:
                            controls.Add((new MidiMessage(beat, MidiCommand.ControlChange | ch, (int)cc.Controller, cc.ControllerValue), seq++));
                            break;
                        case PatchChangeEvent pc:
                            controls.Add((new MidiMessage(beat, MidiCommand.ProgramChange | ch, pc.Patch, 0), seq++));
                            break;
                        case PitchWheelChangeEvent pw:
                            controls.Add((new MidiMessage(beat, MidiCommand.PitchBend | ch, pw.Pitch & 0x7F, (pw.Pitch >> 7) & 0x7F), seq++));
                            break;
                        case ChannelAfterTouchEvent at:
                            controls.Add((new MidiMessage(beat, MidiCommand.ChannelPressure | ch, at.AfterTouchPressure, 0), seq++));
                            break;
                        case TempoEvent te:
                            tempos.Add(new KeyValuePair<double, double>(beat, te.MicrosecondsPerQuarterNote));
                            break;
                    }
                }
                // Notes still open at the end of their track are closed there.
                foreach (var kv in open)
                    foreach (var (onTick, vel) in kv.Value)
                        AddNote(notes, onTick * perTick, lastTick * perTick, kv.Key >> 7, kv.Key & 0x7F, vel);
                end = Math.Max(end, lastTick * perTick);
            }

            notes.Sort((a, b) =>
            {
                int c = a.On.CompareTo(b.On);
                if (c != 0) return c;
                c = a.Channel.CompareTo(b.Channel);
                return c != 0 ? c : a.Key.CompareTo(b.Key);
            });
            controls.Sort((a, b) =>
            {
                int c = a.Message.Beat.CompareTo(b.Message.Beat);
                return c != 0 ? c : a.Seq.CompareTo(b.Seq);
            });
            var controlArray = new MidiMessage[controls.Count];
            for (int i = 0; i < controlArray.Length; i++) controlArray[i] = controls[i].Message;
            foreach (var n in notes) end = Math.Max(end, n.Off);
            return new MidiSong(ppq, new TempoMap(tempos), notes.ToArray(), controlArray, end);
        }

        static void AddNote(List<NoteSpan> notes, double on, double off, int ch, int key, int vel)
        {
            notes.Add(new NoteSpan(on, Math.Max(off, on + MinNoteBeats), ch, key, vel));
        }
    }
}
