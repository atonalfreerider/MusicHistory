using System.Collections;
using UnityEngine;

namespace Shapes
{
    public class Polygon : MonoBehaviour
    {
        // Persistent References
        public MeshFilter meshFilter;
        public Renderer rend;

        // Link Vars
        Coroutine colorAnimator;
        static readonly int Color = Shader.PropertyToID("_Color");

        #region DRAW Functions

        public void DrawRegPoly(float radius, int sides, float offSet, float h, float taper)
        {
            Vector3[] verts = new Vector3[sides];
            int numTriangles = sides - 2;
            int[] indices = new int[numTriangles * 3];

            float alpha = 2 * Mathf.PI / sides;
            for (int ii = 0; ii < sides; ii++)
            {
                verts[ii] = new Vector3(
                    radius * Mathf.Sin(alpha * ii + offSet),
                    0,
                    radius * Mathf.Cos(alpha * ii + offSet));
            }

            for (int ii = 0; ii < numTriangles; ii++)
            {
                indices[ii * 3 + 0] = 0;
                indices[ii * 3 + 1] = ii + 1;
                indices[ii * 3 + 2] = ii + 2;
            }

            if (h <= float.Epsilon)
            {
                Draw3DPoly(verts, MirrorIndices(indices, 0));
            }
            else
            {
                Extrude(verts, indices, h, false, true, taper);
            }
        }

        public static int[] MirrorIndices(int[] indices, int vertLength)
        {
            // copy the indices twice (mirror the second set of indices)
            int[] retIndices = new int[indices.Length * 2];
            int subCount = 0;
            for (int ii = 0; ii < indices.Length; ii++)
            {
                retIndices[ii] = indices[ii];
                retIndices[ii + indices.Length] = indices[ii + subCount] + vertLength;

                switch (subCount)
                {
                    case 0:
                        subCount = 1;
                        break;
                    case 1:
                        subCount = -1;
                        break;
                    default:
                        subCount = 0;
                        break;
                }
            }

            return retIndices;
        }

        protected void Extrude(Vector3[] vertices, int[] indices, float h,
            bool centerPoint, bool fillTopAndBottom, float taper)
        {
            Vector3[] extVertices = new Vector3[vertices.Length * 2];
            // create mirror of main contour at the depth of the extrusion and copy both contours into a new vertices[]
            for (int ii = 0; ii < vertices.Length; ii++)
            {
                extVertices[ii] = new Vector3(vertices[ii].x * (1 - taper), h * .5f, vertices[ii].z * (1 - taper));
                extVertices[ii + vertices.Length] = new Vector3(vertices[ii].x * (1 + taper), -h * .5f,
                    vertices[ii].z * (1 + taper));
            }

            // create indices
            int[] extIndices = new int[vertices.Length * 6];
            int fillIndOffset = 0;

            if (fillTopAndBottom)
            {
                extIndices = new int[indices.Length * 2 + vertices.Length * 6];
                fillIndOffset = indices.Length * 2;

                int[] mirInd = MirrorIndices(indices, vertices.Length);
                for (int ii = 0; ii < mirInd.Length; ii++)
                    extIndices[ii] = mirInd[ii];
            }

            // link up the two contours
            int mod = centerPoint ? 1 : 0;

            for (int ii = mod; ii < vertices.Length - 1; ii++)
            {
                extIndices[(ii - mod) * 6 + 0 + fillIndOffset] = ii + 0;
                extIndices[(ii - mod) * 6 + 1 + fillIndOffset] = ii + vertices.Length;
                extIndices[(ii - mod) * 6 + 2 + fillIndOffset] = ii + vertices.Length + 1;

                extIndices[(ii - mod) * 6 + 3 + fillIndOffset] = ii + 0;
                extIndices[(ii - mod) * 6 + 4 + fillIndOffset] = ii + vertices.Length + 1;
                extIndices[(ii - mod) * 6 + 5 + fillIndOffset] = ii + 1;
            }

            // close external surface end to start;
            extIndices[extIndices.Length - 1] = vertices.Length + mod;
            extIndices[extIndices.Length - 3] = 0 + mod;
            extIndices[extIndices.Length - 2] = vertices.Length - 1;
            extIndices[extIndices.Length - 4] = extVertices.Length - 1;
            extIndices[extIndices.Length - 6] = vertices.Length + mod;
            extIndices[extIndices.Length - 5] = vertices.Length - 1;

            Draw3DPoly(extVertices, extIndices);
        }

        public void Draw3DPoly(Vector3[] vertices, int[] indices)
        {
            // https://docs.unity3d.com/ScriptReference/Mesh.html
            
            // Retrieve the mesh
            Mesh sharedMesh = meshFilter.sharedMesh;

            sharedMesh.Clear();
            sharedMesh.vertices = vertices;
            sharedMesh.triangles = indices;
            sharedMesh.RecalculateNormals();
            sharedMesh.RecalculateBounds();

            // Set up game object with mesh
            meshFilter.sharedMesh = sharedMesh;
        }

        #endregion

        #region ACTION Functions

        /// <summary>
        /// This brightens theme colors that are too dark on the dark background for the wireframes.
        /// 
        /// The real solution to this problem is that themes should include separate colors for the wireframes, 
        /// and then we can remove this method.
        /// </summary>
        static Color BrightenedColor(Color color)
        {
            float brightnessMultiplier = (color.r + color.g + color.b) / 3;

            if (color.r + color.g + color.b < 1.5f)
            {
                brightnessMultiplier = 2 - brightnessMultiplier;
            }

            return color * brightnessMultiplier;
        }

        void ChangeColorProperties(Color color)
        {
            rend.material.SetColor(Color, color);
        }

        public void SetColor(Color color)
        {
            if (colorAnimator != null)
            {
                StopCoroutine(colorAnimator);
            }

            ChangeColorProperties(color);
        }

        public void SetColor(Color color, float animationDuration)
        {
            if (colorAnimator != null)
            {
                StopCoroutine(colorAnimator);
            }

            if (animationDuration <= float.Epsilon)
            {
                SetColor(color);
            }
            else
            {
                colorAnimator = StartCoroutine(
                    BlendColorsAnimator(color, animationDuration));
            }
        }

        IEnumerator BlendColorsAnimator(Color color, float animationDuration)
        {
            float elapsedTime = 0;
            while (elapsedTime < animationDuration)
            {
                ChangeColorProperties(
                    UnityEngine.Color.Lerp(
                        rend.material.color,
                        color,
                        Time.deltaTime / (animationDuration - elapsedTime)));

                elapsedTime += Time.deltaTime;
                yield return null;
            }

            ChangeColorProperties(color);
            colorAnimator = null;
        }

        #endregion
    }
}