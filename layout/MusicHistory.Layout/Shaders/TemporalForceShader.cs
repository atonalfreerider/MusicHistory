using ComputeSharp;

namespace MusicHistory.Layout.Shaders;

/// <summary>
/// One Jacobi step of the temporal force layout, one thread per node (the successor of GPU-FDG's
/// <c>ForceKernelShader</c>).
///
/// Positions are packed as <c>(u, time, v, mass)</c>: <c>u</c> and <c>v</c> are the two free axes, the
/// time coordinate is copied through unchanged (time is pinned exactly), and the mass rides along
/// so the O(N²) repulsion loop needs one load per pair. Every thread reads only
/// <paramref name="posIn"/> and writes only its own element of <paramref name="posOut"/> and
/// <paramref name="vel"/> (ping-pong buffers), and sums in a fixed order, so the result does not
/// depend on thread scheduling: two runs on one device are bit-identical. GPU-FDG updated its
/// single position buffer in place, which made every run different.
/// </summary>
[ThreadGroupSize(DefaultThreadGroupSizes.X)]
[GeneratedComputeShaderDescriptor]
internal readonly partial struct TemporalForceShader(
    ReadWriteBuffer<float4> posIn,
    ReadWriteBuffer<float4> posOut,
    ReadWriteBuffer<float2> vel,
    ReadOnlyBuffer<int> pinned,
    ReadOnlyBuffer<float> stepScale,
    ReadOnlyBuffer<int> adjStart,
    ReadOnlyBuffer<int> adjNode,
    ReadOnlyBuffer<float> adjK,
    ReadOnlyBuffer<int2> edges,
    int nodeCount,
    int edgeCount,
    float repulsion,
    float softening,
    float restLength,
    float centering,
    float edgeRepulsion,
    float edgeClearance2,
    float damping,
    float temperature) : IComputeShader
{
    public void Execute()
    {
        int i = ThreadIds.X;
        float4 p = posIn[i];
        if (pinned[i] != 0)
        {
            posOut[i] = p;
            vel[i] = new float2(0, 0);
            return;
        }
        float3 pi = p.XYZ;
        float mi = p.W;
        float3 f = new(0, 0, 0);

        // Springs with rest length (tree and secondary edges, k pre-multiplied per entry). With
        // time pinned, an edge spanning more than restLength in time always pulls the free axes
        // together, so lineages stack; the rest length matters only between near-contemporaries.
        int end = adjStart[i + 1];
        for (int a = adjStart[i]; a < end; a++)
        {
            float3 d = posIn[adjNode[a]].XYZ - pi;
            float len = Hlsl.Sqrt(Hlsl.Dot(d, d) + 1e-6f);
            f += d * (adjK[a] * (len - restLength) / len);
        }

        // Softened, mass-weighted repulsion over all pairs (3-D distance, so songs far apart in
        // time barely push each other). j == i contributes exactly 0 (d = 0, d2 = softening).
        float rm = repulsion * mi;
        for (int j = 0; j < nodeCount; j++)
        {
            float4 q = posIn[j];
            float3 d = pi - q.XYZ;
            float d2 = Hlsl.Dot(d, d) + softening;
            f += d * (rm * q.W / (d2 * Hlsl.Sqrt(d2)));
        }

        // Optional clearance from edges this node is not part of (O(N·E), off by default).
        if (edgeRepulsion > 0)
        {
            for (int e = 0; e < edgeCount; e++)
            {
                int2 st = edges[e];
                if (st.X == i || st.Y == i) continue;
                float3 s = posIn[st.X].XYZ;
                float3 seg = posIn[st.Y].XYZ - s;
                float segLen2 = Hlsl.Dot(seg, seg);
                if (segLen2 < 1e-4f) continue;
                float proj = Hlsl.Clamp(Hlsl.Dot(pi - s, seg) / segLen2, 0f, 1f);
                float3 away = pi - (s + seg * proj);
                float dist2 = Hlsl.Dot(away, away) + 1e-6f;
                f += away * (edgeRepulsion * Hlsl.Rsqrt(dist2) / (dist2 + edgeClearance2));
            }
        }

        // Mass-weighted centering toward the time axis, on the free axes only.
        float2 free = new(f.X - pi.X * centering * mi, f.Z - pi.Z * centering * mi);

        // Velocity with damping; per-node step 1/(m + k·Σw + 1) keeps any degree stable without
        // moving the equilibrium; the cooled temperature caps the step.
        float2 v = vel[i] * damping + free * stepScale[i];
        float speed = Hlsl.Length(v);
        if (speed > temperature) v *= temperature / speed;
        if (Hlsl.IsNaN(v.X) || Hlsl.IsNaN(v.Y) || Hlsl.IsInfinite(v.X) || Hlsl.IsInfinite(v.Y)) v = new float2(0, 0);
        vel[i] = v;
        posOut[i] = new float4(pi.X + v.X, pi.Y, pi.Z + v.Y, mi);
    }
}
