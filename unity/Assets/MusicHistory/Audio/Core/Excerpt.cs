#nullable enable
using System;
using System.Collections.Generic;

namespace MusicHistory.Audio
{
    /// <summary>How an excerpt window treats notes near its start.</summary>
    public sealed class ExcerptOptions
    {
        /// <summary>Notes starting at most this many beats before the window are moved onto its first beat (early-played downbeats).</summary>
        public double EarlyToleranceBeats = 0.125;
        /// <summary>
        /// Re-strike notes that are held across the window start (pads, long bass notes). Never on
        /// the drum channel: a drum hit is a one-shot whatever length the file writes, so
        /// re-striking it would add a hit the song does not have.
        /// </summary>
        public bool RestrikeHeldNotes = true;
        /// <summary>...but only when at least this much of the note remains.</summary>
        public double MinHeldRemainingBeats = 0.5;

        public static readonly ExcerptOptions Default = new ExcerptOptions();
    }

    /// <summary>
    /// The playable events of one excerpt [StartBeat, EndBeat): the chase (controller and
    /// program state reached before the start, so instruments are right when entering
    /// mid-song) and the timed events, with note-offs at EndBeat for notes that cross it.
    /// </summary>
    public sealed class ExcerptEvents
    {
        public readonly double StartBeat;
        public readonly double EndBeat;
        /// <summary>Applied in order when the excerpt starts.</summary>
        public readonly MidiMessage[] Chase;
        /// <summary>Sorted by (beat, note-off / control / note-on).</summary>
        public readonly MidiMessage[] Events;
        public readonly int NoteCount;

        public ExcerptEvents(double startBeat, double endBeat, MidiMessage[] chase, MidiMessage[] events, int noteCount)
        {
            StartBeat = startBeat;
            EndBeat = endBeat;
            Chase = chase;
            Events = events;
            NoteCount = noteCount;
        }

        public double LengthBeats => Math.Max(0, EndBeat - StartBeat);
    }

    public static class ExcerptBuilder
    {
        public static ExcerptEvents Build(MidiSong song, double startBeat, double endBeat, ExcerptOptions? options = null)
        {
            options ??= ExcerptOptions.Default;
            double start = Math.Max(0, startBeat);
            double end = Math.Max(start, endBeat);
            var chase = BuildChase(song.Controls, start);

            var events = new List<(MidiMessage Message, int Seq)>();
            int seq = 0, noteCount = 0;
            foreach (var n in song.Notes)
            {
                if (n.On >= end) break;                    // notes are sorted by onset
                if (n.Off <= start) continue;
                double on = n.On;
                if (on < start)
                {
                    bool early = start - on <= options.EarlyToleranceBeats;
                    bool held = options.RestrikeHeldNotes && n.Channel != MidiCommand.PercussionChannel &&
                                n.Off - start >= options.MinHeldRemainingBeats;
                    if (!early && !held) continue;
                    on = start;
                }
                double off = Math.Min(n.Off, end);
                if (off <= on) off = Math.Min(end, on + MidiSongReader.MinNoteBeats);
                events.Add((new MidiMessage(on, MidiCommand.NoteOn | n.Channel, n.Key, n.Velocity), seq++));
                events.Add((new MidiMessage(off, MidiCommand.NoteOff | n.Channel, n.Key, 0), seq++));
                noteCount++;
            }
            foreach (var m in song.Controls)
            {
                if (m.Beat < start) continue;
                if (m.Beat >= end) break;
                events.Add((m, seq++));
            }
            events.Sort((a, b) =>
            {
                int c = a.Message.Beat.CompareTo(b.Message.Beat);
                if (c != 0) return c;
                c = a.Message.Priority.CompareTo(b.Message.Priority);
                return c != 0 ? c : a.Seq.CompareTo(b.Seq);
            });
            var array = new MidiMessage[events.Count];
            for (int i = 0; i < array.Length; i++) array[i] = events[i].Message;
            return new ExcerptEvents(start, end, chase, array, noteCount);
        }

        /// <summary>
        /// Controller state at <paramref name="start"/>, compressed: for plain controllers,
        /// programs, bends and channel pressure only the last value matters, so each is kept
        /// once; the (N)RPN protocol and Reset All Controllers are sequential, so they are kept
        /// in order, with the pending plain values of that channel flushed before a reset.
        /// </summary>
        public static MidiMessage[] BuildChase(MidiMessage[] controls, double start)
        {
            var output = new List<MidiMessage>();
            // pending[channel] maps a state key to the last message for it.
            var pending = new Dictionary<int, MidiMessage>[16];
            for (int c = 0; c < 16; c++) pending[c] = new Dictionary<int, MidiMessage>();
            foreach (var m in controls)
            {
                if (m.Beat >= start) break;
                int ch = m.Channel;
                switch (m.Command)
                {
                    case MidiCommand.ControlChange:
                        int cc = m.Data1;
                        if (cc == MidiCommand.CcAllSoundOff || cc >= MidiCommand.CcAllNotesOff) continue;  // nothing sounds yet; mode messages ignored
                        if (cc == MidiCommand.CcResetAllControllers)
                        {
                            Flush(pending[ch], output);
                            output.Add(m.At(start));
                        }
                        else if (MidiCommand.IsParameterProtocol(cc)) output.Add(m.At(start));
                        else pending[ch][cc] = m;
                        break;
                    case MidiCommand.ProgramChange: pending[ch][128] = m; break;
                    case MidiCommand.PitchBend: pending[ch][129] = m; break;
                    case MidiCommand.ChannelPressure: pending[ch][130] = m; break;
                }
            }
            for (int c = 0; c < 16; c++) Flush(pending[c], output);
            for (int i = 0; i < output.Count; i++) output[i] = output[i].At(start);
            return output.ToArray();
        }

        static void Flush(Dictionary<int, MidiMessage> pending, List<MidiMessage> output)
        {
            if (pending.Count == 0) return;
            // Bank select before the program change (GM semantics), the rest in controller order.
            var keys = new List<int>(pending.Keys);
            keys.Sort((a, b) => Rank(a).CompareTo(Rank(b)));
            foreach (int k in keys) output.Add(pending[k]);
            pending.Clear();
        }

        static int Rank(int key) => key == MidiCommand.CcBankMsb ? -3 : key == MidiCommand.CcBankLsb ? -2 : key == 128 ? -1 : key;
    }
}
