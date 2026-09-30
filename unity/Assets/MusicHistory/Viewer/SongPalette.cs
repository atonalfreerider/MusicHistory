#nullable enable
using System;
using System.Globalization;
using UnityEngine;

namespace MusicHistory.Viewer
{
    /// <summary>How an influence edge is coloured: by the channel that carried the influence.</summary>
    public enum EdgeChannel
    {
        Chord,
        Melody,
        Both,
        Bass,
        Loop
    }

    /// <summary>
    /// Colours and key names shared by bubbles, edges, the HUD and the legend.
    ///
    /// Key colour: the hue is the key signature's place on the circle of fifths (C major and
    /// A minor share a hue, G/E minor the next one, and so on), so songs that share a pitch
    /// collection look alike, exactly as the relative normalization compares them; minor keys
    /// are darker. Decade colour (the bubble ring) is a sequential ramp from the 1940s to the
    /// 2020s. Edges: chord warm, melody cool, melody+harmony white, bass green, loop gold.
    /// </summary>
    public static class SongPalette
    {
        static readonly string[] SharpNames = { "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B" };
        static readonly string[] FlatNames = { "C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B" };

        /// <summary>Pitch classes spelled with flats (DESIGN.md §3): F Bb Eb Ab Db Gb major, D G C F Bb Eb minor.</summary>
        static bool UsesFlats(int pc, bool minor) => minor
            ? pc is 2 or 7 or 0 or 5 or 10 or 3
            : pc is 5 or 10 or 3 or 8 or 1 or 6;

        public static int Wrap12(int pc) => ((pc % 12) + 12) % 12;

        public static string PitchName(int pc, bool minor)
        {
            pc = Wrap12(pc);
            return (UsesFlats(pc, minor) ? FlatNames : SharpNames)[pc];
        }

        public static string KeyName(int tonicPc, bool minor) => $"{PitchName(tonicPc, minor)} {(minor ? "minor" : "major")}";

        /// <summary>Circle-of-fifths index of the key signature: C/Am = 0, G/Em = 1, ..., F/Dm = 11.</summary>
        public static int FifthsIndex(int tonicPc, bool minor)
        {
            int majorTonic = minor ? Wrap12(tonicPc + 3) : Wrap12(tonicPc);
            return (majorTonic * 7) % 12;
        }

        /// <summary>Hue of C major; rotates the wheel so C sits in a calm blue-green.</summary>
        const float HueOffset = .47f;

        public static Color KeyColor(int tonicPc, bool minor)
        {
            float hue = Mathf.Repeat(FifthsIndex(tonicPc, minor) / 12f + HueOffset, 1f);
            return minor ? Color.HSVToRGB(hue, .70f, .66f) : Color.HSVToRGB(hue, .52f, .98f);
        }

        static readonly Color[] DecadeStops =
        {
            Hex("#9b7fd1"), // 1940s
            Hex("#6f8fe0"), // 1950s
            Hex("#45b4d6"), // 1960s
            Hex("#3cc6a0"), // 1970s
            Hex("#8fd05a"), // 1980s
            Hex("#e8cf4a"), // 1990s
            Hex("#f5a142"), // 2000s
            Hex("#f06e6e"), // 2010s
            Hex("#f2f2f2"), // 2020s
        };

        static readonly Color EarlyColor = Hex("#b39c83");

        public const int FirstDecade = 1940;
        public const int LastDecade = 2020;

        public static Color DecadeColor(int year)
        {
            if (year < FirstDecade) return EarlyColor;
            int index = Mathf.Clamp((year - FirstDecade) / 10, 0, DecadeStops.Length - 1);
            return DecadeStops[index];
        }

        public static Color ChannelColor(EdgeChannel channel) => channel switch
        {
            EdgeChannel.Chord => Hex("#ff9a3c"),
            EdgeChannel.Melody => Hex("#4aa8ff"),
            EdgeChannel.Both => Hex("#f4f6fb"),
            EdgeChannel.Bass => Hex("#43d17a"),
            EdgeChannel.Loop => Hex("#ffcf4a"),
            _ => Color.white
        };

        public static string ChannelLabel(EdgeChannel channel) => channel switch
        {
            EdgeChannel.Chord => "chords",
            EdgeChannel.Melody => "melody",
            EdgeChannel.Both => "melody + harmony",
            EdgeChannel.Bass => "bass",
            EdgeChannel.Loop => "loop",
            _ => channel.ToString()
        };

        /// <summary>White when the influence carries both melody and harmony (chord or loop), else the primary channel.</summary>
        public static EdgeChannel Classify(string? channels, string? primary)
        {
            string list = (channels ?? "").ToLowerInvariant();
            bool melody = list.Contains("melody");
            bool harmony = list.Contains("chord") || list.Contains("loop");
            if (melody && harmony) return EdgeChannel.Both;
            return (primary ?? "").Trim().ToLowerInvariant() switch
            {
                "melody" => EdgeChannel.Melody,
                "bass" => EdgeChannel.Bass,
                "loop" => EdgeChannel.Loop,
                "chord" => EdgeChannel.Chord,
                _ => melody ? EdgeChannel.Melody : EdgeChannel.Chord
            };
        }

        public static Color Hex(string hex)
        {
            if (ColorUtility.TryParseHtmlString(hex, out Color color)) return color;
            throw new ArgumentException($"bad colour {hex}");
        }

        public static string ToHex(Color color) => "#" + ColorUtility.ToHtmlStringRGB(color);

        public static string Invariant(double value, string format) => value.ToString(format, CultureInfo.InvariantCulture);
    }
}
