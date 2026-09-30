#nullable enable
using System;

namespace MusicHistory.Audio
{
    /// <summary>
    /// Fallback synth when no SoundFont is installed, in the spirit of Resonance-2's
    /// MusicSynth: sine voices with a one-pole envelope, plus short noise/sine bursts for the
    /// drum channel so the groove stays audible. Honours channel volume, expression, pan,
    /// sustain pedal and pitch bend (±2 semitones); transposition multiplies each voice's
    /// phase increment, so it glides exactly like the SoundFont path.
    /// </summary>
    public sealed class SineDeckSynth : IDeckSynth
    {
        const int MaxVoices = 48;

        struct Voice
        {
            public bool Active, Released, Drum, Held;
            public int Channel, Key;
            public double Phase, Hz, Amp, Env, Target;
            public double ToneEnv, NoiseEnv, ToneDecay, NoiseDecay;   // drums only
            public int Age;
        }

        readonly int rate;
        readonly Voice[] voices = new Voice[MaxVoices];
        readonly float[] volume = new float[16], expression = new float[16], pan = new float[16];
        readonly float[] bend = new float[16];
        readonly bool[] sustain = new bool[16];
        double transposeRatio = 1;
        uint noise = 0x12345678;
        int age;

        public SineDeckSynth(int sampleRate)
        {
            rate = sampleRate;
            Reset();
        }

        public int SampleRate => rate;

        public int ActiveVoices
        {
            get
            {
                int n = 0;
                for (int i = 0; i < MaxVoices; i++) if (voices[i].Active) n++;
                return n;
            }
        }

        public void Reset()
        {
            Array.Clear(voices, 0, voices.Length);
            for (int c = 0; c < 16; c++)
            {
                volume[c] = 100 / 127f;
                expression[c] = 1f;
                pan[c] = 0.5f;
                bend[c] = 0;
                sustain[c] = false;
            }
        }

        public void SetTranspose(double semitones) => transposeRatio = Math.Pow(2, semitones / 12.0);

        public void ReleaseAll()
        {
            for (int i = 0; i < MaxVoices; i++)
                if (voices[i].Active) { voices[i].Released = true; voices[i].Held = false; voices[i].Target = 0; }
        }

        public void SilenceAll()
        {
            for (int i = 0; i < MaxVoices; i++) voices[i].Active = false;
        }

        public void Send(int status, int data1, int data2)
        {
            int command = status & 0xF0, ch = status & 0x0F;
            switch (command)
            {
                case MidiCommand.NoteOn when data2 > 0:
                    NoteOn(ch, data1, data2);
                    break;
                case MidiCommand.NoteOn:
                case MidiCommand.NoteOff:
                    for (int i = 0; i < MaxVoices; i++)
                    {
                        ref Voice v = ref voices[i];
                        if (!v.Active || v.Released || v.Channel != ch || v.Key != data1 || v.Drum) continue;
                        if (sustain[ch]) v.Held = true;
                        else { v.Released = true; v.Target = 0; }
                    }
                    break;
                case MidiCommand.ControlChange:
                    switch (data1)
                    {
                        case 7: volume[ch] = data2 / 127f; break;
                        case 10: pan[ch] = data2 / 127f; break;
                        case 11: expression[ch] = data2 / 127f; break;
                        case MidiCommand.CcSustain:
                            sustain[ch] = data2 >= 64;
                            if (!sustain[ch])
                                for (int i = 0; i < MaxVoices; i++)
                                    if (voices[i].Active && voices[i].Channel == ch && voices[i].Held)
                                    { voices[i].Held = false; voices[i].Released = true; voices[i].Target = 0; }
                            break;
                        case MidiCommand.CcResetAllControllers:
                            expression[ch] = 1f; bend[ch] = 0; sustain[ch] = false;
                            break;
                        case MidiCommand.CcAllSoundOff:
                        case MidiCommand.CcAllNotesOff:
                            for (int i = 0; i < MaxVoices; i++)
                                if (voices[i].Active && voices[i].Channel == ch) { voices[i].Released = true; voices[i].Target = 0; }
                            break;
                    }
                    break;
                case MidiCommand.PitchBend:
                    bend[ch] = (((data2 << 7) | data1) - 8192) / 8192f * 2f;
                    break;
            }
        }

        void NoteOn(int ch, int key, int velocity)
        {
            int slot = -1, oldest = int.MaxValue;
            for (int i = 0; i < MaxVoices; i++)
            {
                if (!voices[i].Active) { slot = i; break; }
                int score = voices[i].Released ? voices[i].Age - 1_000_000 : voices[i].Age;
                if (score < oldest) { oldest = score; slot = i; }
            }
            double a = velocity / 127.0;
            voices[slot] = new Voice
            {
                Active = true,
                Channel = ch,
                Key = key,
                Drum = ch == MidiCommand.PercussionChannel,
                Hz = 440.0 * Math.Pow(2, (key - 69) / 12.0),
                Amp = a * a,
                Target = 1,
                ToneEnv = 1,
                NoiseEnv = 1,
                // Kick/toms: low pitched thump; cymbals/hats: long/short noise; snare: noise + tone.
                ToneDecay = key <= 36 || (key >= 41 && key <= 50) ? 0.9993 : 0.998,
                NoiseDecay = key == 42 || key == 44 ? 0.9985 : key >= 49 && key <= 59 ? 0.99985 : key <= 36 ? 0.99 : 0.9993,
                Age = ++age,
            };
        }

        public void Render(float[] left, float[] right, int offset, int count)
        {
            Array.Clear(left, offset, count);
            Array.Clear(right, offset, count);
            const double attack = 0.02, release = 0.0015;
            for (int i = 0; i < MaxVoices; i++)
            {
                ref Voice v = ref voices[i];
                if (!v.Active) continue;
                int ch = v.Channel;
                float gain = (float)(v.Amp * volume[ch] * expression[ch] * 0.12);
                float gl = gain * (float)Math.Sqrt(1 - pan[ch]), gr = gain * (float)Math.Sqrt(pan[ch]);
                if (v.Drum)
                {
                    double drumHz = v.Key <= 36 ? 60 : v.Key <= 50 ? 90 + (v.Key - 41) * 12 : 180;
                    double inc = 2 * Math.PI * drumHz / rate;
                    double tone = v.ToneEnv, nz = v.NoiseEnv;
                    for (int s = 0; s < count; s++)
                    {
                        noise = noise * 1664525u + 1013904223u;
                        double n = ((noise >> 9) / 4194304.0 - 1.0) * nz;
                        double t = Math.Sin(v.Phase) * tone * (v.Key <= 50 ? 1.0 : 0.25);
                        v.Phase += inc;
                        float x = (float)((t + n * 0.6) * 1.6);
                        left[offset + s] += x * gl;
                        right[offset + s] += x * gr;
                        tone *= v.ToneDecay;
                        nz *= v.NoiseDecay;
                    }
                    v.ToneEnv = tone;
                    v.NoiseEnv = nz;
                    if (v.Phase > 2 * Math.PI) v.Phase %= 2 * Math.PI;
                    if (tone < 1e-4 && nz < 1e-4) v.Active = false;
                    continue;
                }
                double hz = v.Hz * transposeRatio * Math.Pow(2, bend[ch] / 12.0);
                double step = 2 * Math.PI * hz / rate;
                for (int s = 0; s < count; s++)
                {
                    double k = v.Target > v.Env ? attack : v.Released ? release : 0.00002;
                    if (!v.Released && v.Env >= v.Target * 0.999) v.Target = Math.Max(0.35, v.Target * 0.99999);
                    v.Env += (v.Target - v.Env) * k;
                    double y = Math.Sin(v.Phase) + 0.25 * Math.Sin(2 * v.Phase);
                    v.Phase += step;
                    float x = (float)(y * v.Env);
                    left[offset + s] += x * gl;
                    right[offset + s] += x * gr;
                }
                if (v.Phase > 2 * Math.PI) v.Phase %= 2 * Math.PI;
                if (v.Released && v.Env < 1e-4) v.Active = false;
            }
        }
    }
}
