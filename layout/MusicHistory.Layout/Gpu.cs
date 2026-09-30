using System.Globalization;
using ComputeSharp;

namespace MusicHistory.Layout;

/// <summary>DirectX 12 device selection (ComputeSharp). WARP is Windows' software rasterizer: slow, but always there.</summary>
internal static class Gpu
{
    public static GraphicsDevice Select(string spec)
    {
        try
        {
            if (string.IsNullOrEmpty(spec) || spec.Equals("default", StringComparison.OrdinalIgnoreCase))
                return GraphicsDevice.GetDefault();
            var all = GraphicsDevice.EnumerateDevices().ToList();
            if (spec.Equals("warp", StringComparison.OrdinalIgnoreCase))
                return all.FirstOrDefault(d => !d.IsHardwareAccelerated)
                       ?? throw new GpuException("no WARP (software) DirectX 12 device is available");
            if (int.TryParse(spec, NumberStyles.Integer, CultureInfo.InvariantCulture, out int index))
                return index >= 0 && index < all.Count
                    ? all[index]
                    : throw new GpuException($"--device {index}: there are {all.Count} devices (see 'devices')");
            return all.FirstOrDefault(d => d.Name.Contains(spec, StringComparison.OrdinalIgnoreCase))
                   ?? throw new GpuException($"--device '{spec}' matches no device (see 'devices')");
        }
        catch (GpuException)
        {
            throw;
        }
        catch (Exception ex)
        {
            throw new GpuException($"no usable DirectX 12 device ({ex.GetType().Name}: {ex.Message})", ex);
        }
    }

    public static string Describe(GraphicsDevice d) =>
        d.IsHardwareAccelerated ? d.Name : $"{d.Name} (software)";

    public static IEnumerable<string> List()
    {
        int i = 0;
        foreach (var d in GraphicsDevice.EnumerateDevices())
            yield return $"{i++}: {Describe(d)}, {d.DedicatedMemorySize / (1024 * 1024)} MB dedicated";
    }
}
