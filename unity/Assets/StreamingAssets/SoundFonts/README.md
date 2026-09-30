# SoundFonts for standalone builds

This folder is only a placeholder. The SoundFont is not committed: `*.sf2` is gitignored, and
the file is about 490 MB.

In the editor, `MusicHistory.Audio.SongPlayer` loads `<repo>/data/soundfonts/MS_Basic.sf2`,
which `python tools/sf3_to_sf2.py` creates from MuseScore's MIT-licensed `MS Basic.sf3`.

A standalone build cannot reach `<repo>/data`. Before building, copy two files here:

- `MS_Basic.sf2`
- `MS_Basic_License.md` (the MIT notice must travel with the SoundFont)

The player looks here last, as `StreamingAssets/SoundFonts/MS_Basic.sf2`. You can also set
the player's `SoundFontPath`.
