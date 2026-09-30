using System.Globalization;
using System.Runtime.CompilerServices;
using System.Text;

namespace MusicHistory.Influence;

/// <summary>
/// 64-bit FNV-1a over <c>kind|n|tokens</c> (DESIGN.md §8.2). The prefix <c>kind|n|</c> is ASCII;
/// every token follows as a little-endian int32, so an n-gram's identity does not depend on how
/// its tokens would print.
/// </summary>
internal static class Fnv
{
    public const ulong Offset = 14695981039346656037UL;
    public const ulong Prime = 1099511628211UL;

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public static ulong Int(ulong h, int v)
    {
        h = (h ^ (byte)v) * Prime;
        h = (h ^ (byte)(v >> 8)) * Prime;
        h = (h ^ (byte)(v >> 16)) * Prime;
        return (h ^ (byte)(v >> 24)) * Prime;
    }

    public static ulong Bytes(ulong h, ReadOnlySpan<byte> bytes)
    {
        foreach (byte b in bytes) h = (h ^ b) * Prime;
        return h;
    }

    public static ulong Prefix(string kind, int n) =>
        Bytes(Offset, Encoding.ASCII.GetBytes(kind + "|" + n.ToString(CultureInfo.InvariantCulture) + "|"));

    public static ulong Text(string s) => Bytes(Offset, Encoding.UTF8.GetBytes(s));
}

/// <summary>xoshiro256** seeded through SplitMix64: fast, portable and fully deterministic.</summary>
internal struct Rng
{
    private ulong _s0, _s1, _s2, _s3;

    public Rng(ulong seed)
    {
        _s0 = SplitMix(ref seed);
        _s1 = SplitMix(ref seed);
        _s2 = SplitMix(ref seed);
        _s3 = SplitMix(ref seed);
    }

    private static ulong SplitMix(ref ulong x)
    {
        ulong z = x += 0x9E3779B97F4A7C15UL;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9UL;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBUL;
        return z ^ (z >> 31);
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public ulong Next()
    {
        ulong result = RotL(_s1 * 5, 7) * 9;
        ulong t = _s1 << 17;
        _s2 ^= _s0;
        _s3 ^= _s1;
        _s1 ^= _s2;
        _s0 ^= _s3;
        _s2 ^= t;
        _s3 = RotL(_s3, 45);
        return result;
    }

    /// <summary>Uniform integer in [0, n) (multiply-shift; bias below 2^-32 is irrelevant here).</summary>
    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public int Below(int n) => (int)(((Next() >> 32) * (ulong)(uint)n) >> 32);

    public double NextDouble() => (Next() >> 11) * (1.0 / (1UL << 53));

    private static ulong RotL(ulong x, int k) => (x << k) | (x >> (64 - k));

    /// <summary>Seed for the surrogates of one pair and channel ("seeded by pair and channel").</summary>
    public static ulong Seed(string aId, string bId, string channel) => Fnv.Text(aId + "|" + bId + "|" + channel);
}

/// <summary>Open-addressing set of 64-bit hashes (0 is stored out of band). Reusable: <see cref="Clear"/>
/// only touches the slots that were used.</summary>
internal sealed class HashSet64
{
    private ulong[] _slots;
    private int[] _used;
    private int _count, _mask;
    private bool _hasZero;

    public HashSet64(int capacity = 64)
    {
        int cap = 16;
        while (cap < capacity * 2) cap <<= 1;
        _slots = new ulong[cap];
        _used = new int[cap];
        _mask = cap - 1;
    }

    public int Count => _count + (_hasZero ? 1 : 0);

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    private static int Mix(ulong h) => (int)((h * 0x9E3779B97F4A7C15UL) >> 33);

    public bool Add(ulong h)
    {
        if (h == 0)
        {
            bool added = !_hasZero;
            _hasZero = true;
            return added;
        }
        if ((_count + 1) * 2 > _slots.Length) Grow();
        int i = Mix(h) & _mask;
        while (true)
        {
            ulong s = _slots[i];
            if (s == 0)
            {
                _slots[i] = h;
                _used[_count++] = i;
                return true;
            }
            if (s == h) return false;
            i = (i + 1) & _mask;
        }
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    public bool Contains(ulong h)
    {
        if (h == 0) return _hasZero;
        int i = Mix(h) & _mask;
        while (true)
        {
            ulong s = _slots[i];
            if (s == h) return true;
            if (s == 0) return false;
            i = (i + 1) & _mask;
        }
    }

    /// <summary>The members in insertion order (0 last, when present).</summary>
    public IEnumerable<ulong> Keys()
    {
        for (int k = 0; k < _count; k++) yield return _slots[_used[k]];
        if (_hasZero) yield return 0;
    }

    public void Clear()
    {
        for (int k = 0; k < _count; k++) _slots[_used[k]] = 0;
        _count = 0;
        _hasZero = false;
    }

    private void Grow()
    {
        var old = _slots;
        int oldCount = _count;
        var oldUsed = _used;
        _slots = new ulong[old.Length * 2];
        _used = new int[_slots.Length];
        _mask = _slots.Length - 1;
        _count = 0;
        for (int k = 0; k < oldCount; k++)
        {
            ulong h = old[oldUsed[k]];
            int i = Mix(h) & _mask;
            while (_slots[i] != 0) i = (i + 1) & _mask;
            _slots[i] = h;
            _used[_count++] = i;
        }
    }
}
