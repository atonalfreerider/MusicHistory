using ComputeSharp;

namespace MusicHistory.Layout.Shaders;

/// <summary>
/// One Jacobi step of the lyric-themes layout (DESIGN.md §12), one thread per song.
///
/// Positions are <c>(x, y, z, 0)</c>. Every song has a zero-rest-length spring to each of the ten
/// pinned anchors with stiffness <c>spring · w_ik</c> (w normalized per song, so the springs sum
/// to <c>spring · (b_i − p_i)</c> with b_i the weighted barycentre), and a softened repulsion from
/// every other song. Like <see cref="TemporalForceShader"/>, every thread reads only
/// <paramref name="posIn"/> and writes only its own element of <paramref name="posOut"/> and
/// <paramref name="vel"/>, and sums in a fixed order: two runs on one device are bit-identical.
///
/// The step is Jacobi-preconditioned: the force is divided by the song's own stiffness (spring
/// plus the bound 2·repulsion·Σ_j 1/(d²+s)^{3/2} of its repulsion Jacobian), which keeps dense
/// clouds stable and never moves an equilibrium. In the plane mode (<paramref name="slabHalf"/> =
/// 0) y is held at exactly 0; in a slab, y has its own spring (spring · flatten) and |y| ≤ slabHalf.
/// </summary>
[ThreadGroupSize(DefaultThreadGroupSizes.X)]
[GeneratedComputeShaderDescriptor]
internal readonly partial struct ThemesForceShader(
    ReadWriteBuffer<float4> posIn,
    ReadWriteBuffer<float4> posOut,
    ReadWriteBuffer<float4> vel,
    ReadOnlyBuffer<float> weights,
    ReadOnlyBuffer<float4> anchors,
    int songCount,
    int anchorCount,
    float spring,
    float repulsion,
    float softening,
    float cutoff2,
    float cutoffShift,
    float slabHalf,
    float flatten,
    float damping,
    float temperature) : IComputeShader
{
    public void Execute()
    {
        int i = ThreadIds.X;
        float3 p = posIn[i].XYZ;
        float3 f = new(0, 0, 0);

        // Springs to all anchors (zero rest length). Σ_k w_ik = 1, so the equilibrium without
        // repulsion is the barycentre Σ_k w_ik A_k.
        int wBase = i * anchorCount;
        for (int k = 0; k < anchorCount; k++)
            f += (anchors[k].XYZ - p) * (spring * weights[wBase + k]);

        // Softened repulsion from every other song (optionally shifted to 0 at the cutoff), and
        // the bound of its Jacobian for the preconditioned step.
        float stiff = 0;
        if (repulsion > 0)
        {
            for (int j = 0; j < songCount; j++)
            {
                if (j == i) continue;
                float3 d = p - posIn[j].XYZ;
                float d2 = Hlsl.Dot(d, d) + softening;
                if (cutoff2 > 0 && d2 >= cutoff2) continue;
                float inv = 1f / (d2 * Hlsl.Sqrt(d2));
                f += d * (repulsion * (inv - cutoffShift));
                stiff += inv;
            }
        }
        float extra = 2f * repulsion * stiff;

        float3 step = new(f.X / (spring + extra), 0, f.Z / (spring + extra));
        if (slabHalf > 0)
        {
            float ky = spring * flatten;
            step.Y = (f.Y - ky * p.Y) / (ky + extra);
        }

        float3 v = vel[i].XYZ * damping + step;
        float speed = Hlsl.Length(v);
        if (speed > temperature) v *= temperature / speed;
        if (Hlsl.IsNaN(v.X) || Hlsl.IsNaN(v.Y) || Hlsl.IsNaN(v.Z) || Hlsl.IsInfinite(v.X) || Hlsl.IsInfinite(v.Y) || Hlsl.IsInfinite(v.Z))
            v = new float3(0, 0, 0);
        float3 q = p + v;
        if (slabHalf > 0)
        {
            if (q.Y > slabHalf)
            {
                q.Y = slabHalf;
                v.Y = 0;
            }
            else if (q.Y < -slabHalf)
            {
                q.Y = -slabHalf;
                v.Y = 0;
            }
        }
        else
        {
            q.Y = 0;
            v.Y = 0;
        }
        vel[i] = new float4(v, 0);
        posOut[i] = new float4(q, 0);
    }
}
