#nullable enable
using System.Globalization;
using UnityEngine;

namespace MusicHistory.Themes
{
    /// <summary>The three singer colours of the themes viewer.</summary>
    public enum SingerColorGroup
    {
        Male,
        Female,
        Neutral
    }

    /// <summary>
    /// Colours and wording of the themes viewer (docs/DESIGN.md §12): male singers blue, female
    /// singers pink, everything else (mixed, nonbinary, unknown, instrumental) neutral grey.
    /// Songs classified from their lyrics are filled; songs classified from the title only are
    /// hollow (dark fill, coloured rim). Themes (anchors, their pads, tethers and score bars) are
    /// gold, so they never read as a singer colour.
    /// </summary>
    public static class ThemesPalette
    {
        public static readonly Color Male = Hex("#4c9bff");
        public static readonly Color Female = Hex("#ff6fb1");
        public static readonly Color Neutral = Hex("#a3a9b2");
        public static readonly Color Theme = Hex("#ffcb6b");
        public static readonly Color ThemeDim = Hex("#b38f4d");
        public const string Muted = "#9aa3ad";

        public static SingerColorGroup Group(SingerGender g) => g switch
        {
            SingerGender.Male => SingerColorGroup.Male,
            SingerGender.Female => SingerColorGroup.Female,
            _ => SingerColorGroup.Neutral
        };

        public static Color GroupColor(SingerColorGroup g) => g switch
        {
            SingerColorGroup.Male => Male,
            SingerColorGroup.Female => Female,
            _ => Neutral
        };

        public static Color SingerColor(SingerGender g) => GroupColor(Group(g));

        /// <summary>Bubble fill: the singer colour (lyrics) or a dark tint of it (title only, hollow look).</summary>
        public static Color Fill(SingerColorGroup g, bool fromLyrics)
        {
            Color c = GroupColor(g);
            return fromLyrics ? c : new Color(c.r * .16f + .02f, c.g * .16f + .02f, c.b * .16f + .025f, 1f);
        }

        /// <summary>Bubble rim: a lighter tint (lyrics) or the singer colour itself (title only).</summary>
        public static Color Ring(SingerColorGroup g, bool fromLyrics)
        {
            Color c = GroupColor(g);
            return fromLyrics ? Color.Lerp(c, Color.white, .35f) : c;
        }

        /// <summary>Rim width as a fraction of the radius: thin for lyrics, thick for the hollow title-only look.</summary>
        public static float RingWidth(bool fromLyrics) => fromLyrics ? .16f : .36f;

        /// <summary>How the hover card names the singer.</summary>
        public static string SingerPhrase(SingerGender g) => g switch
        {
            SingerGender.Male => "male singer",
            SingerGender.Female => "female singer",
            SingerGender.Mixed => "mixed singers",
            SingerGender.Nonbinary => "nonbinary singer",
            SingerGender.Instrumental => "instrumental",
            _ => "singer unknown"
        };

        /// <summary>How the hover card names the text the song was classified from.</summary>
        public static string SourcePhrase(bool fromLyrics) => fromLyrics ? "lyrics" : "title only";

        /// <summary>Scores as the HUD prints them.</summary>
        public static string Score(double score) => score.ToString("0.00", CultureInfo.InvariantCulture);

        public static string ToHex(Color c) => "#" + ColorUtility.ToHtmlStringRGB(c);

        public static string ToHexA(Color c) => "#" + ColorUtility.ToHtmlStringRGBA(c);

        public static Color Hex(string hex) => ColorUtility.TryParseHtmlString(hex, out Color c) ? c : Color.magenta;
    }
}
