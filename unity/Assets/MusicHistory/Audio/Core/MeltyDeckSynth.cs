#nullable enable
using System;
using MeltySynth;

namespace MusicHistory.Audio
{
    /// <summary>
    /// A MeltySynth <see cref="Synthesizer"/> with continuous transposition.
    ///
    /// Transposition uses the standard tuning RPNs: RPN 2 (coarse, semitones) + RPN 1 (fine,
    /// 14-bit, ±1 semitone) on every channel except 10. MeltySynth adds the channel tune to the
    /// pitch of every voice each 64-sample block, so sounding notes glide (verified by the
    /// PlayerCore.Tests frequency checks). The song's own RPN 1/2 settings are intercepted and
    /// added to ours instead of being overwritten, and the song's RPN selection is restored
    /// after each write so its later data-entry messages (e.g. pitch-bend range) still land
    /// where it meant them to.
    /// </summary>
    public sealed class MeltyDeckSynth : IDeckSynth
    {
        const int NullParam = 127;

        readonly Synthesizer synth;
        // What the song selected / set, per channel.
        readonly int[] rpnMsb = new int[16], rpnLsb = new int[16];
        readonly bool[] nrpnSelected = new bool[16];
        readonly int[] songCoarse = new int[16], songFine = new int[16];
        // What we last wrote (int.MinValue = unknown).
        readonly int[] writtenCoarse = new int[16], writtenFine = new int[16];
        double transpose;

        public MeltyDeckSynth(SoundFont soundFont, int sampleRate, bool reverbAndChorus = true, int maxPolyphony = 64)
        {
            var settings = new SynthesizerSettings(sampleRate)
            {
                BlockSize = DeckEngine.BlockSize,
                MaximumPolyphony = maxPolyphony,
                EnableReverbAndChorus = reverbAndChorus,
            };
            synth = new Synthesizer(soundFont, settings);
            ResetTracking();
        }

        public Synthesizer Synthesizer => synth;
        public int SampleRate => synth.SampleRate;
        public int ActiveVoices => synth.ActiveVoiceCount;

        public float MasterVolume
        {
            get => synth.MasterVolume;
            set => synth.MasterVolume = value;
        }

        public void Reset()
        {
            synth.Reset();
            ResetTracking();
            ApplyTuning(force: false);
        }

        void ResetTracking()
        {
            for (int c = 0; c < 16; c++)
            {
                rpnMsb[c] = rpnLsb[c] = NullParam;
                nrpnSelected[c] = false;
                songCoarse[c] = 0;
                songFine[c] = 8192;
                writtenCoarse[c] = 0;       // power-on state of the synth
                writtenFine[c] = 8192;
            }
        }

        public void Send(int status, int data1, int data2)
        {
            int command = status & 0xF0, ch = status & 0x0F;
            if (command == MidiCommand.ControlChange)
            {
                switch (data1)
                {
                    case MidiCommand.CcRpnMsb: rpnMsb[ch] = data2; nrpnSelected[ch] = false; break;
                    case MidiCommand.CcRpnLsb: rpnLsb[ch] = data2; nrpnSelected[ch] = false; break;
                    case MidiCommand.CcNrpnMsb:
                    case MidiCommand.CcNrpnLsb: nrpnSelected[ch] = true; break;
                    case MidiCommand.CcDataEntryMsb:
                    case MidiCommand.CcDataEntryLsb:
                        if (!nrpnSelected[ch] && rpnMsb[ch] == 0 && (rpnLsb[ch] == 1 || rpnLsb[ch] == 2))
                        {
                            CaptureSongTuning(ch, data1, data2);
                            return;   // folded into our own tuning write
                        }
                        break;
                    case MidiCommand.CcResetAllControllers:
                        synth.ProcessMidiMessage(ch, command, data1, data2);
                        // MeltySynth clears the RPN selection; make sure our tune is still in place.
                        rpnMsb[ch] = rpnLsb[ch] = NullParam;
                        nrpnSelected[ch] = false;
                        writtenCoarse[ch] = writtenFine[ch] = int.MinValue;
                        ApplyTuning(force: false);
                        return;
                }
            }
            synth.ProcessMidiMessage(ch, command, data1, data2);
        }

        void CaptureSongTuning(int ch, int cc, int value)
        {
            if (rpnLsb[ch] == 2)
            {
                if (cc == MidiCommand.CcDataEntryMsb) songCoarse[ch] = value - 64;
            }
            else if (cc == MidiCommand.CcDataEntryMsb) songFine[ch] = (value << 7) | (songFine[ch] & 0x7F);
            else songFine[ch] = (songFine[ch] & 0x3F80) | value;
            writtenCoarse[ch] = writtenFine[ch] = int.MinValue;
            ApplyTuning(force: false);
        }

        public void SetTranspose(double semitones)
        {
            if (semitones == transpose) return;
            transpose = semitones;
            ApplyTuning(force: false);
        }

        void ApplyTuning(bool force)
        {
            for (int ch = 0; ch < 16; ch++)
            {
                if (ch == MidiCommand.PercussionChannel) continue;
                double total = songCoarse[ch] + (songFine[ch] - 8192) / 8192.0 + transpose;
                int coarse = (int)Math.Round(total);
                int fine = (int)Math.Round(8192 + (total - coarse) * 8192);
                if (fine > 16383) fine = 16383;
                if (fine < 0) fine = 0;
                coarse = Math.Max(-64, Math.Min(63, coarse));
                bool coarseChanged = force || coarse != writtenCoarse[ch];
                bool fineChanged = force || fine != writtenFine[ch];
                if (!coarseChanged && !fineChanged) continue;
                synth.ProcessMidiMessage(ch, MidiCommand.ControlChange, MidiCommand.CcRpnMsb, 0);
                if (coarseChanged)
                {
                    synth.ProcessMidiMessage(ch, MidiCommand.ControlChange, MidiCommand.CcRpnLsb, 2);
                    synth.ProcessMidiMessage(ch, MidiCommand.ControlChange, MidiCommand.CcDataEntryMsb, coarse + 64);
                    writtenCoarse[ch] = coarse;
                }
                if (fineChanged)
                {
                    synth.ProcessMidiMessage(ch, MidiCommand.ControlChange, MidiCommand.CcRpnLsb, 1);
                    synth.ProcessMidiMessage(ch, MidiCommand.ControlChange, MidiCommand.CcDataEntryMsb, fine >> 7);
                    synth.ProcessMidiMessage(ch, MidiCommand.ControlChange, MidiCommand.CcDataEntryLsb, fine & 0x7F);
                    writtenFine[ch] = fine;
                }
                // Restore the song's own parameter selection.
                synth.ProcessMidiMessage(ch, MidiCommand.ControlChange, MidiCommand.CcRpnMsb, rpnMsb[ch]);
                synth.ProcessMidiMessage(ch, MidiCommand.ControlChange, MidiCommand.CcRpnLsb, rpnLsb[ch]);
            }
        }

        public void ReleaseAll() => synth.NoteOffAll(false);

        public void SilenceAll() => synth.NoteOffAll(true);

        public void Render(float[] left, float[] right, int offset, int count) =>
            synth.Render(left.AsSpan(offset, count), right.AsSpan(offset, count));
    }
}
