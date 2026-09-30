// Hover tethers of the lyric themes viewer: additive light from a song to its top themes, drawn
// over the bubbles (no depth test) so a tether stays visible through a dense cluster. Line
// renderers supply the geometry; brightness comes from _Color. Properties live in the
// UnityPerMaterial CBUFFER so the SRP Batcher batches it.
Shader "MusicHistory/ThemesTether"
{
    Properties
    {
        _Color ("Color", Color) = (1, 0.8, 0.4, 1)
    }
    SubShader
    {
        Tags { "RenderType"="Transparent" "Queue"="Transparent+20" "RenderPipeline"="UniversalPipeline" "IgnoreProjector"="True" }
        Cull Off
        ZWrite Off
        ZTest Always
        Blend One One
        Pass
        {
            Name "Unlit"
            HLSLPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"

            CBUFFER_START(UnityPerMaterial)
                float4 _Color;
            CBUFFER_END

            struct Attributes { float4 positionOS : POSITION; float2 uv : TEXCOORD0; };
            struct Varyings { float4 positionHCS : SV_POSITION; float2 uv : TEXCOORD0; };

            Varyings vert(Attributes input)
            {
                Varyings output;
                output.positionHCS = TransformObjectToHClip(input.positionOS.xyz);
                output.uv = input.uv;
                return output;
            }

            half4 frag(Varyings input) : SV_Target
            {
                // Soft across the line (v = 0..1 across a LineRenderer), brightest in the middle.
                float across = 1.0 - abs(input.uv.y * 2.0 - 1.0);
                float soft = saturate(across * 1.6);
                return half4(_Color.rgb * soft, 1);
            }
            ENDHLSL
        }
    }
}
