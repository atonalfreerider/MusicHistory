#nullable enable
using System;
using System.Reflection;
using UnityEngine;

namespace MusicHistory.Playback
{
    /// <summary>
    /// Finds the walkthrough's player without a compile-time dependency on the audio code:
    /// an <see cref="ISongPlayer"/> component already on the host wins; otherwise the type
    /// <c>MusicHistory.Audio.SongPlayer</c> is looked up by reflection (it exists once the audio
    /// module is in the project) and added; otherwise a <see cref="SilentSongPlayer"/> is used.
    /// </summary>
    public static class SongPlayerDiscovery
    {
        public const string AudioPlayerTypeName = "MusicHistory.Audio.SongPlayer";

        public static ISongPlayer Discover(GameObject host, out string description)
        {
            foreach (MonoBehaviour behaviour in host.GetComponents<MonoBehaviour>())
            {
                if (behaviour is ISongPlayer existing && behaviour is not SilentSongPlayer)
                {
                    description = behaviour.GetType().FullName ?? behaviour.GetType().Name;
                    return existing;
                }
            }

            Type? type = FindAudioPlayerType();
            if (type != null)
            {
                try
                {
                    if (host.AddComponent(type) is ISongPlayer added)
                    {
                        description = type.FullName ?? type.Name;
                        return added;
                    }
                }
                catch (Exception e)
                {
                    Debug.LogWarning($"MusicHistory: could not add {AudioPlayerTypeName} ({e.Message}); using the silent player.");
                }
            }

            description = "silent (no synth found)";
            return Silent(host);
        }

        public static SilentSongPlayer Silent(GameObject host)
        {
            SilentSongPlayer silent = host.GetComponent<SilentSongPlayer>();
            return silent != null ? silent : host.AddComponent<SilentSongPlayer>();
        }

        public static Type? FindAudioPlayerType()
        {
            foreach (Assembly assembly in UnityEngine.Assemblies.CurrentAssemblies.GetLoadedAssemblies())
            {
                Type? type;
                try
                {
                    type = assembly.GetType(AudioPlayerTypeName, false);
                }
                catch (Exception)
                {
                    continue;
                }
                if (type != null && typeof(MonoBehaviour).IsAssignableFrom(type) &&
                    typeof(ISongPlayer).IsAssignableFrom(type) && !type.IsAbstract)
                    return type;
            }
            return null;
        }
    }
}
