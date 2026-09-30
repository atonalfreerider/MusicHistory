#nullable enable
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.Universal;

namespace MusicHistory.Viewer
{
    /// <summary>
    /// Background and bloom (merged from Unity-FDG's DarkGreyGradientBackground and
    /// HdrBloomController): a dark vertical gradient sky and a runtime URP bloom volume, so the
    /// HDR glow of highlighted rings and edges blooms while normal edges stay crisp.
    /// </summary>
    public static class SceneLook
    {
        static Material? skyMaterial;
        static Volume? volume;

        public static void Apply(Camera? camera)
        {
            if (camera == null) return;
            ApplyBackground(camera);
            ApplyBloom(camera);
        }

        static void ApplyBackground(Camera camera)
        {
            Shader shader = Shader.Find("MusicHistory/GradientSkybox");
            if (shader == null)
            {
                camera.clearFlags = CameraClearFlags.SolidColor;
                camera.backgroundColor = new Color32(20, 22, 26, 255);
                return;
            }

            if (skyMaterial == null)
            {
                skyMaterial = new Material(shader) { name = "MusicHistory Gradient Sky", hideFlags = HideFlags.DontSave };
                skyMaterial.SetColor("_TopColor", new Color32(38, 42, 50, 255));
                skyMaterial.SetColor("_BottomColor", new Color32(11, 12, 15, 255));
            }
            RenderSettings.skybox = skyMaterial;
            camera.clearFlags = CameraClearFlags.Skybox;
        }

        static void ApplyBloom(Camera camera)
        {
            camera.allowHDR = true;
            camera.GetUniversalAdditionalCameraData().renderPostProcessing = true;
            if (volume != null) return;
            Volume? existing = Object.FindAnyObjectByType<Volume>();
            if (existing != null && existing.name == "MusicHistory Bloom")
            {
                volume = existing;
                return;
            }

            GameObject volumeObject = new("MusicHistory Bloom") { hideFlags = HideFlags.DontSave };
            volume = volumeObject.AddComponent<Volume>();
            volume.isGlobal = true;
            volume.priority = 100f;

            VolumeProfile profile = ScriptableObject.CreateInstance<VolumeProfile>();
            profile.name = "MusicHistory Runtime Bloom";
            profile.hideFlags = HideFlags.DontSave;
            Bloom bloom = profile.Add<Bloom>(true);
            bloom.threshold.Override(1.0f);
            bloom.intensity.Override(.9f);
            bloom.scatter.Override(.65f);
            bloom.highQualityFiltering.Override(true);
            volume.sharedProfile = profile;
        }

        /// <summary>Removes the runtime volume (validation reloads).</summary>
        public static void Reset()
        {
            if (volume != null)
            {
                if (Application.isPlaying) Object.Destroy(volume.gameObject);
                else Object.DestroyImmediate(volume.gameObject);
            }
            volume = null;
        }
    }
}
