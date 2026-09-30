#nullable enable

namespace MusicHistory.Audio
{
    /// <summary>
    /// The synthesizer behind one deck. Every call happens on the audio thread, in block order,
    /// and must not allocate.
    /// </summary>
    public interface IDeckSynth
    {
        int SampleRate { get; }
        /// <summary>Silences everything and restores power-on channel state.</summary>
        void Reset();
        /// <summary>One channel message (status = command | 0-based channel).</summary>
        void Send(int status, int data1, int data2);
        /// <summary>
        /// Continuous transposition of every non-percussion channel, in semitones. Applies to
        /// notes that are already sounding (the glide is audible within one block).
        /// </summary>
        void SetTranspose(double semitones);
        /// <summary>Note-off for every sounding voice (release stage, not a hard cut).</summary>
        void ReleaseAll();
        /// <summary>Renders <paramref name="count"/> frames into the two buffers (overwriting them).</summary>
        void Render(float[] left, float[] right, int offset, int count);
        int ActiveVoices { get; }
    }
}
