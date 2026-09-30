// Unlit HDR edge/line colour: _Color + _GlowColor * _GlowIntensity (values above 1 feed bloom).
// Blend and depth writes are material properties: resting edges are additive light trails
// (dense bundles read as brighter flows instead of an opaque tangle), highlighted edges are
// opaque. Properties live in the UnityPerMaterial CBUFFER so the SRP Batcher batches every edge.
Shader "MusicHistory/GlowingEdge"
{
    Properties
    {
        _Color ("Base Color", Color) = (0.5, 0.5, 0.5, 1)
        [HDR] _GlowColor ("Glow Color", Color) = (1, 1, 1, 1)
        _GlowIntensity ("Glow Intensity", Range(0, 8)) = 2
        [Enum(UnityEngine.Rendering.BlendMode)] _SrcBlend ("Src Blend", Float) = 1
        [Enum(UnityEngine.Rendering.BlendMode)] _DstBlend ("Dst Blend", Float) = 0
        [Toggle] _ZWrite ("Z Write", Float) = 1
    }
    SubShader
    {
        Tags { "RenderType"="Opaque" "Queue"="Geometry" "RenderPipeline"="UniversalPipeline" }
        Cull Back
        ZWrite [_ZWrite]
        Blend [_SrcBlend] [_DstBlend]
        Pass
        {
            Name "Unlit"
            HLSLPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"

            CBUFFER_START(UnityPerMaterial)
                float4 _Color;
                float4 _GlowColor;
                float _GlowIntensity;
                float _SrcBlend;
                float _DstBlend;
                float _ZWrite;
            CBUFFER_END

            struct Attributes { float4 positionOS : POSITION; };
            struct Varyings { float4 positionHCS : SV_POSITION; };

            Varyings vert(Attributes input)
            {
                Varyings output;
                output.positionHCS = TransformObjectToHClip(input.positionOS.xyz);
                return output;
            }

            half4 frag(Varyings input) : SV_Target
            {
                half3 hdrColor = _Color.rgb + _GlowColor.rgb * _GlowIntensity;
                return half4(hdrColor, 1);
            }
            ENDHLSL
        }
    }
}
