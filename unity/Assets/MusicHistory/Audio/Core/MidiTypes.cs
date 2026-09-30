#nullable enable
using System;

namespace MusicHistory.Audio
{
    /// <summary>
    /// One MIDI channel message placed on the quarter-note beat grid. The sequencer works in
    /// beats, never in ticks or seconds, so a tempo glide is just a different beat rate.
    /// </summary>
    public readonly struct MidiMessage
    {
        public readonly double Beat;
        /// <summary>Command nibble | channel (0-based, 9 = drums).</summary>
        public readonly byte Status;
        public readonly byte Data1;
        public readonly byte Data2;

        public MidiMessage(double beat, int status, int data1, int data2)
        {
            Beat = beat;
            Status = (byte)status;
            Data1 = (byte)(data1 & 0x7F);
            Data2 = (byte)(data2 & 0x7F);
        }

        public int Command => Status & 0xF0;
        public int Channel => Status & 0x0F;
        public bool IsNoteOn => Command == MidiCommand.NoteOn && Data2 > 0;
        public bool IsNoteOff => Command == MidiCommand.NoteOff || (Command == MidiCommand.NoteOn && Data2 == 0);

        /// <summary>Order at equal beats: note-offs first, then controllers/programs, then note-ons.</summary>
        public int Priority => IsNoteOff ? 0 : IsNoteOn ? 2 : 1;

        public MidiMessage At(double beat) => new MidiMessage(beat, Status, Data1, Data2);

        public override string ToString() => $"{Beat:F4} {Status:X2} {Data1} {Data2}";
    }

    /// <summary>A paired note (on/off in beats) as read from a file.</summary>
    public readonly struct NoteSpan
    {
        public readonly double On;
        public readonly double Off;
        public readonly byte Channel;
        public readonly byte Key;
        public readonly byte Velocity;

        public NoteSpan(double on, double off, int channel, int key, int velocity)
        {
            On = on;
            Off = off;
            Channel = (byte)channel;
            Key = (byte)key;
            Velocity = (byte)Math.Max(1, Math.Min(127, velocity));
        }
    }

    public static class MidiCommand
    {
        public const int NoteOff = 0x80;
        public const int NoteOn = 0x90;
        public const int PolyPressure = 0xA0;
        public const int ControlChange = 0xB0;
        public const int ProgramChange = 0xC0;
        public const int ChannelPressure = 0xD0;
        public const int PitchBend = 0xE0;

        public const int PercussionChannel = 9;

        // Controllers the sequencer treats specially.
        public const int CcBankMsb = 0;
        public const int CcDataEntryMsb = 6;
        public const int CcBankLsb = 32;
        public const int CcDataEntryLsb = 38;
        public const int CcSustain = 64;
        public const int CcDataIncrement = 96;
        public const int CcDataDecrement = 97;
        public const int CcNrpnLsb = 98;
        public const int CcNrpnMsb = 99;
        public const int CcRpnLsb = 100;
        public const int CcRpnMsb = 101;
        public const int CcAllSoundOff = 120;
        public const int CcResetAllControllers = 121;
        public const int CcAllNotesOff = 123;

        /// <summary>Controllers that form the (N)RPN parameter protocol: their order matters.</summary>
        public static bool IsParameterProtocol(int cc) =>
            cc == CcDataEntryMsb || cc == CcDataEntryLsb || (cc >= CcDataIncrement && cc <= CcRpnMsb);
    }
}
