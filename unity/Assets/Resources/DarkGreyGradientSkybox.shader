Shader "MusicHistory/GradientSkybox"
{
    Properties
    {
        _TopColor ("Top", Color) = (0.19, 0.20, 0.22, 1)
        _BottomColor ("Bottom", Color) = (0.06, 0.07, 0.08, 1)
    }
    SubShader
    {
        Tags { "Queue"="Background" "RenderType"="Background" "PreviewType"="Skybox" }
        Cull Off
        ZWrite Off
        Pass
        {
            CGPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "UnityCG.cginc"

            struct appdata { float4 vertex : POSITION; };
            struct v2f { float4 position : SV_POSITION; float3 direction : TEXCOORD0; };
            fixed4 _TopColor;
            fixed4 _BottomColor;

            v2f vert(appdata input)
            {
                v2f output;
                output.position = UnityObjectToClipPos(input.vertex);
                output.direction = input.vertex.xyz;
                return output;
            }

            fixed4 frag(v2f input) : SV_Target
            {
                float height = normalize(input.direction).y * .5 + .5;
                height = smoothstep(0.05, 0.95, height);
                return lerp(_BottomColor, _TopColor, height);
            }
            ENDCG
        }
    }
}
