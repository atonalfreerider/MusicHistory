using System.IO.Compression;
using System.Text;

namespace MusicHistory.Influence;

/// <summary>
/// Minimal reader for numpy <c>.npz</c> archives (a zip of <c>.npy</c> files, format 1.0-3.0): little-endian
/// int64 / int32 arrays and fixed-width unicode (<c>&lt;U</c>n) arrays, C order. Enough for the benchmark's
/// <c>df_corpus.npz</c>.
/// </summary>
internal sealed class Npz : IDisposable
{
    private readonly ZipArchive _zip;

    public Npz(string path) => _zip = ZipFile.OpenRead(path);

    public void Dispose() => _zip.Dispose();

    public IEnumerable<string> Names => _zip.Entries.Select(e => e.FullName.EndsWith(".npy", StringComparison.Ordinal) ? e.FullName[..^4] : e.FullName);

    public bool Has(string name) => _zip.GetEntry(name + ".npy") != null;

    private (string Descr, long Count, byte[] Data) Raw(string name)
    {
        var e = _zip.GetEntry(name + ".npy") ?? throw new FileNotFoundException($"{name}.npy not in the archive");
        using var s = e.Open();
        using var ms = new MemoryStream();
        s.CopyTo(ms);
        var b = ms.ToArray();
        if (b.Length < 10 || b[0] != 0x93 || Encoding.ASCII.GetString(b, 1, 5) != "NUMPY") throw new FormatException($"{name}: not an .npy file");
        int major = b[6];
        int hlen, off;
        if (major == 1)
        {
            hlen = b[8] | (b[9] << 8);
            off = 10;
        }
        else
        {
            hlen = BitConverter.ToInt32(b, 8);
            off = 12;
        }
        string header = Encoding.Latin1.GetString(b, off, hlen);
        string descr = Field(header, "descr");
        if (header.Contains("'fortran_order': True", StringComparison.Ordinal)) throw new FormatException($"{name}: Fortran order");
        string shape = Field(header, "shape").Trim('(', ')', ' ');
        long count = 1;
        foreach (var part in shape.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
            count *= long.Parse(part, System.Globalization.CultureInfo.InvariantCulture);
        var data = b[(off + hlen)..];
        return (descr, count, data);
    }

    private static string Field(string header, string key)
    {
        int i = header.IndexOf("'" + key + "'", StringComparison.Ordinal);
        if (i < 0) throw new FormatException($"npy header without {key}");
        i = header.IndexOf(':', i) + 1;
        while (header[i] == ' ') i++;
        if (header[i] == '\'')
        {
            int j = header.IndexOf('\'', i + 1);
            return header[(i + 1)..j];
        }
        if (header[i] == '(')
        {
            int j = header.IndexOf(')', i);
            return header[i..(j + 1)];
        }
        int k = header.IndexOfAny([',', '}'], i);
        return header[i..k].Trim();
    }

    public long[] Int64(string name)
    {
        var (descr, count, data) = Raw(name);
        var outp = new long[count];
        switch (descr)
        {
            case "<i8":
            case "<u8":
                Buffer.BlockCopy(data, 0, outp, 0, checked((int)(count * 8)));
                break;
            case "<i4":
                for (long i = 0; i < count; i++) outp[i] = BitConverter.ToInt32(data, (int)(i * 4));
                break;
            default:
                throw new FormatException($"{name}: dtype {descr} is not an integer type this reader knows");
        }
        return outp;
    }

    public string[] Strings(string name)
    {
        var (descr, count, data) = Raw(name);
        if (!descr.StartsWith("<U", StringComparison.Ordinal)) throw new FormatException($"{name}: dtype {descr} is not <U");
        int width = int.Parse(descr[2..], System.Globalization.CultureInfo.InvariantCulture);
        var outp = new string[count];
        for (long i = 0; i < count; i++)
        {
            string s = Encoding.UTF32.GetString(data, (int)(i * width * 4), width * 4);
            outp[i] = s.TrimEnd('\0');
        }
        return outp;
    }
}
