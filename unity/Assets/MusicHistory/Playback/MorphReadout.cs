#nullable enable

namespace MusicHistory.Playback
{
    /// <summary>
    /// Optional companion of <see cref="ISongPlayer"/> for the HUD. Under
    /// <see cref="Morph.Plan(SongClip, SongClip, double, double, double)"/> the plan's
    /// StartTempoRatio is relative to the file's own tempo over the excerpt's first bar, which
    /// only the player knows (it has the tempo map), so the player reports the start BPM itself.
    /// </summary>
    public interface IMorphReadout
    {
        /// <summary>BPM at the current clip's first beat under <see cref="ISongPlayer.CurrentPlan"/> (0 = unknown).</summary>
        double PlanStartBpm { get; }
    }
}
