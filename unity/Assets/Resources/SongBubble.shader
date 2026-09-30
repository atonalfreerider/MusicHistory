// A song bubble drawn as a sphere impostor: a camera-facing quad (billboarded in the vertex
// shader, so no per-frame transform updates) whose fragments write the depth of the sphere's
// front surface. Edges that start at the sphere surface are therefore hidden inside the ball
// exactly as they would be by real geometry. Fill = key colour, ring = decade colour; the ring
// glows (HDR) when the song is focused or related. Anti-aliased rim via alpha-to-coverage.
// Properties live in the UnityPerMaterial CBUFFER so the SRP Batcher batches all bubbles.
Shader "MusicHistory/SongBubble"
{
    Properties
    {
        _FillColor ("Fill (key)", Color) = (0.2, 0.5, 0.8, 1)
        _RingColor ("Ring (decade)", Color) = (1, 1, 1, 1)
        _RingWidth ("Ring Width", Range(0, 0.5)) = 0.14
        [HDR] _GlowColor ("Glow Color", Color) = (1, 1, 1, 1)
        _GlowIntensity ("Glow Intensity", Range(0, 8)) = 0
        _DimFactor ("Dim Factor", Range(0, 2)) = 1
    }
    SubShader
    {
        Tags { "RenderType"="TransparentCutout" "Queue"="AlphaTest" "RenderPipeline"="UniversalPipeline" "DisableBatching"="True" "IgnoreProjector"="True" }
        Cull Off
        ZWrite On
        AlphaToMask On
        Pass
        {
            Name "Unlit"
            HLSLPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"

            #define MIN_PIXELS 4.0

            CBUFFER_START(UnityPerMaterial)
                float4 _FillColor;
                float4 _RingColor;
                float4 _GlowColor;
                float _RingWidth;
                float _GlowIntensity;
                float _DimFactor;
            CBUFFER_END

            struct Attributes { float4 positionOS : POSITION; float2 uv : TEXCOORD0; };
            struct Varyings
            {
                float4 positionHCS : SV_POSITION;
                float2 uv : TEXCOORD0;
                float3 centerVS : TEXCOORD1;
                float radius : TEXCOORD2;
            };

            Varyings vert(Attributes input)
            {
                Varyings output;
                float3 centerWS = TransformObjectToWorld(float3(0, 0, 0));
                float3 axisX = float3(UNITY_MATRIX_M[0].x, UNITY_MATRIX_M[1].x, UNITY_MATRIX_M[2].x);
                float diameter = length(axisX);
                // Never smaller than MIN_PIXELS on screen, so far-away songs stay visible dots.
                float3 centerVS0 = TransformWorldToView(centerWS);
                float pixelWorld = 2.0 * max(-centerVS0.z, 1e-3) / (max(_ScreenParams.y, 1.0) * max(abs(UNITY_MATRIX_P[1][1]), 1e-4));
                diameter = max(diameter, MIN_PIXELS * pixelWorld);
                float3 right = UNITY_MATRIX_V[0].xyz;
                float3 up = UNITY_MATRIX_V[1].xyz;
                float3 positionWS = centerWS + (right * input.positionOS.x + up * input.positionOS.y) * diameter;
                output.positionHCS = TransformWorldToHClip(positionWS);
                output.uv = input.uv;
                output.centerVS = centerVS0;
                output.radius = 0.5 * diameter;
                return output;
            }

            half4 frag(Varyings input, out float depth : SV_Depth) : SV_Target
            {
                float2 d = (input.uv - 0.5) * 2.0;          // -1..1 across the disc
                float r = length(d);                         // 0 at the centre, 1 at the rim
                float aa = max(fwidth(r), 1e-4);
                float alpha = saturate((1.0 - r) / aa + 0.5);
                clip(alpha - 0.01);

                // Front surface of the sphere (view space looks down -z, so +z faces the camera).
                float h = sqrt(saturate(1.0 - r * r));
                float3 surfaceVS = input.centerVS + float3(d * input.radius, h * input.radius);
                float4 surfaceCS = TransformWViewToHClip(surfaceVS);
                depth = surfaceCS.z / surfaceCS.w;

                float ringStart = 1.0 - _RingWidth;
                float ring = saturate((r - ringStart) / aa + 0.5);
                half3 color = lerp(_FillColor.rgb, _RingColor.rgb, ring);
                // Soft light from the upper left gives the ball some volume.
                float3 n = float3(d, h);
                float light = 0.72 + 0.28 * saturate(dot(n, normalize(float3(-0.35, 0.45, 0.82))));
                color *= lerp(light, 1.0, ring);
                color *= _DimFactor;
                color += _GlowColor.rgb * (_GlowIntensity * ring);
                return half4(color, alpha);
            }
            ENDHLSL
        }
    }
}
