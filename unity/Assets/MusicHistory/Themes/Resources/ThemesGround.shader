// Flat, procedurally drawn disc lying in the ring plane (x-z), for the lyric themes viewer:
// the dial (the ring, faint guide circles and a spoke to every theme) and each theme's pad
// (a soft glow with a crisp rim). Line widths are in screen pixels (fwidth), so the ring stays
// a clean hairline at any zoom. Alpha blended, no depth writes: bubbles (which write depth)
// cover it. Properties live in the UnityPerMaterial CBUFFER so the SRP Batcher batches it.
Shader "MusicHistory/ThemesGround"
{
    Properties
    {
        _FillColor ("Fill", Color) = (1, 1, 1, 0.1)
        _FillPower ("Fill Falloff Power", Float) = 2
        _RingColor ("Ring", Color) = (1, 1, 1, 0.6)
        _RingRadius ("Ring Radius (0..1)", Float) = 0.98
        _RingPixels ("Ring Width (px)", Float) = 1.5
        _GuideColor ("Guide Circles", Color) = (1, 1, 1, 0.05)
        _GuideCount ("Guide Circle Count", Float) = 0
        _SpokeColor ("Spokes", Color) = (1, 1, 1, 0.08)
        _SpokeCount ("Spoke Count", Float) = 0
        _SpokeAngle ("First Spoke Angle (deg, +x toward +z)", Float) = 0
        _SpokePixels ("Spoke Width (px)", Float) = 1
        _SpokeInner ("Spoke Inner Radius (0..1)", Float) = 0.04
    }
    SubShader
    {
        Tags { "RenderType"="Transparent" "Queue"="Transparent" "RenderPipeline"="UniversalPipeline" "IgnoreProjector"="True" }
        Cull Off
        ZWrite Off
        ZTest LEqual
        Blend SrcAlpha OneMinusSrcAlpha
        Pass
        {
            Name "Unlit"
            HLSLPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"

            CBUFFER_START(UnityPerMaterial)
                float4 _FillColor;
                float4 _RingColor;
                float4 _GuideColor;
                float4 _SpokeColor;
                float _FillPower;
                float _RingRadius;
                float _RingPixels;
                float _GuideCount;
                float _SpokeCount;
                float _SpokeAngle;
                float _SpokePixels;
                float _SpokeInner;
            CBUFFER_END

            struct Attributes { float4 positionOS : POSITION; float2 uv : TEXCOORD0; };
            struct Varyings { float4 positionHCS : SV_POSITION; float2 p : TEXCOORD0; };

            Varyings vert(Attributes input)
            {
                Varyings output;
                output.positionHCS = TransformObjectToHClip(input.positionOS.xyz);
                output.p = (input.uv - 0.5) * 2.0;   // -1..1; x along local +x, y along local +z
                return output;
            }

            // Porter-Duff "over" with straight (non-premultiplied) colour.
            float4 Over(float4 dst, float3 rgb, float a)
            {
                float outA = a + dst.a * (1.0 - a);
                float3 outRgb = (rgb * a + dst.rgb * dst.a * (1.0 - a)) / max(outA, 1e-5);
                return float4(outRgb, outA);
            }

            half4 frag(Varyings input) : SV_Target
            {
                float2 p = input.p;
                float r = length(p);
                float aa = max(fwidth(r), 1e-5);            // disc units per pixel
                float edge = saturate((1.0 - r) / aa + 0.5);
                clip(edge - 0.001);

                float4 c = float4(_FillColor.rgb, _FillColor.a * saturate(1.0 - pow(saturate(r), _FillPower)) * edge);

                if (_GuideCount > 0.5)
                {
                    float stepR = _RingRadius / (_GuideCount + 1.0);
                    float nearest = clamp(round(r / stepR), 1.0, _GuideCount);
                    float g = saturate(0.5 - abs(r - nearest * stepR) / aa + 0.5);
                    c = Over(c, _GuideColor.rgb, _GuideColor.a * g);
                }

                if (_SpokeCount > 0.5)
                {
                    float sector = 6.2831853 / _SpokeCount;
                    float ang = atan2(p.y, p.x) - radians(_SpokeAngle);
                    float t = ang / sector;
                    float d = abs(sin((t - round(t)) * sector)) * r;   // distance to the nearest spoke line
                    float along = smoothstep(_SpokeInner, _SpokeInner + 0.12, r) * (1.0 - smoothstep(_RingRadius - 0.01, _RingRadius, r));
                    float s = saturate(_SpokePixels * 0.5 - d / aa + 0.5) * along;
                    c = Over(c, _SpokeColor.rgb, _SpokeColor.a * s);
                }

                float ring = saturate(_RingPixels * 0.5 - abs(r - _RingRadius) / aa + 0.5);
                c = Over(c, _RingColor.rgb, _RingColor.a * ring);
                return half4(c.rgb, c.a);
            }
            ENDHLSL
        }
    }
}
