using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;

namespace Shapes
{
    /// <summary>
    /// Any solid with polygonal faces. Subset includes the Platonic solids.
    /// 
    /// Euler's formula for polyhedra is
    /// V - E + F = 2
    /// where V is # vertices, E is # edges, F is number of faces
    /// </summary>
    public static class Polyhedra
    {
        /// <summary>
        /// For the purpose of rendering a triangle in a polyhedron, three indices must be defined that refer to three
        /// specific vertices to draw a face.
        /// </summary>
        public struct TriangleIndices
        {
            public readonly int v1;
            public readonly int v2;
            public readonly int v3;

            public TriangleIndices(int v1, int v2, int v3)
            {
                this.v1 = v1;
                this.v2 = v2;
                this.v3 = v3;
            }
        }

        /// <summary>
        /// Flatten a set of triangle indices into a single array of indices.
        /// </summary>
        public static int[] IndicesFromTris(TriangleIndices[] tris)
        {
            int[] ret = new int[tris.Length * 3];
            int count = 0;
            foreach (TriangleIndices triangleIndices in tris)
            {
                ret[count] = triangleIndices.v1;
                ret[count + 1] = triangleIndices.v2;
                ret[count + 2] = triangleIndices.v3;
                count += 3;
            }

            return ret;
        }

        /// <summary>
        /// The center of gravity for any triangle
        /// </summary>
        public static Vector3 Centroid(Vector3 p1, Vector3 p2, Vector3 p3)
        {
            return new Vector3(
                (p1.x + p2.x + p3.x) / 3,
                (p1.y + p2.y + p3.y) / 3,
                (p1.z + p2.z + p3.z) / 3);
        }

        /// <summary>
        /// A pairing of the vertices and triangle indices that define the face of a polyhedron.
        /// </summary>
        public struct VertsAndFaces
        {
            public readonly Vector3[] verts;
            public readonly TriangleIndices[] faces;

            public VertsAndFaces(Vector3[] verts, TriangleIndices[] faces)
            {
                this.verts = verts;
                this.faces = faces;
            }
        }
        
        /// <summary>
        /// Takes a polygon and assigns new geometry to it to be drawn.
        /// </summary>
        /// <param name="poly">The polygon that will have new geometry assigned to be drawn</param>
        /// <param name="verts">The vertices that define the polyhedron</param>
        /// <param name="faces">The triangle indices that define a face to be drawn</param>
        /// <param name="doubleSide">Should the reverse side of any face also be drawn</param>
        public static void NewPolyhedron(Polygon poly, Vector3[] verts, TriangleIndices[] faces, bool doubleSide)
        {
            if (verts.Length > 65534)
            {
                Debug.LogError($"Maximum vertex limit exceeded for polyhedron: {verts.Length}\n  Limit is 65534");
                return;
            }
            
            Mesh sharedMesh = poly.meshFilter.sharedMesh;
            sharedMesh.Clear();

            sharedMesh.vertices = verts;

            List<int> triList = new List<int>();
            for (int i = 0; i < faces.Length; i++)
            {
                triList.Add(faces[i].v1);
                triList.Add(faces[i].v2);
                triList.Add(faces[i].v3);

                if (doubleSide)
                {
                    triList.Add(faces[i].v1);
                    triList.Add(faces[i].v3);
                    triList.Add(faces[i].v2);
                }
            }

            sharedMesh.triangles = triList.ToArray();

            Vector3[] normals = new Vector3[verts.Length];
            for (int i = 0; i < normals.Length; i++)
            {
                normals[i] = verts[i].normalized;
            }

            sharedMesh.normals = normals;

            sharedMesh.RecalculateBounds();

            //poly.meshFilter.sharedMesh = sharedMesh.GenerateWireframeMesh(false, false);
        }

        public static int IcoRecursionLevelFromFaceCount(int numRequredFaces)
        {
            int recursionLevel = 0;
            int req = (int) Math.Pow(4, recursionLevel) * 20;
            while (req < numRequredFaces)
            {
                recursionLevel++;
                req = (int) Math.Pow(4, recursionLevel) * 20;
            }

            return recursionLevel;
        }

        public static int IcoRecursionLevelFromVertCount(int numRequiredVerts)
        {
            int recursionLevel = 0;
            int req = (int) Math.Pow(4, recursionLevel) * (12 - 2) + 2;
            while (req < numRequiredVerts)
            {
                recursionLevel++;
                req = (int) Math.Pow(4, recursionLevel) * (12 - 2) + 2;
            }

            return recursionLevel;
        }
        
        #region PLATONIC SOLIDS

        /// <summary>
        ///        ^
        ///       /|\
        ///      / | \
        ///     /  |  \
        ///     '-.|.-'
        /// </summary>
        public static VertsAndFaces NewTetraVertsAndFaces(float d)
        {
            return new VertsAndFaces(new[]
                {
                    new Vector3(d, d, d),
                    new Vector3(d, -d, -d),
                    new Vector3(-d, d, -d),
                    new Vector3(-d, -d, d)
                },
                new[]
                {
                    new TriangleIndices(0, 1, 2),
                    new TriangleIndices(0, 3, 1),
                    new TriangleIndices(0, 2, 3),
                    new TriangleIndices(1, 3, 2)
                });
        }

        /// <summary>
        ///             _-_.
        ///          _-',^. `-_.
        ///      ._-' ,'   `.   `-_ 
        ///     !`-_._________`-':::
        ///     !   /\        /\::::
        ///     ;  /  \      /..\:::
        ///     ! /    \    /....\::
        ///     !/      \  /......\:
        ///     ;--.___. \/_.__.--;; 
        ///      '-_    `:!;;;;;;;'
        ///         `-_, :!;;;''
        ///             `-!'    
        /// 
        /// The formulas to calculate the number of vertices, edges and faces are:
        /// 
        /// Number of vertices: V = 4^n * (V_0 - 2) + 2
        /// Number of edges: E = 4^n * E_0
        /// Number of faces: F = 4^n * F_0
        /// 
        /// where F_0, V_0 and E_0 are, respectively, the number of faces, vertices and edges of the polyhedron with
        /// triangular faces used for the initial subdivision.
        ///
        /// For a regular icosahedron F_0 = 20, V_0 = 12, and E_0 = 30
        ///
        /// The sequence of vertices for n = 0 ... 11 is
        /// 0    1     2     3      4       5         6        7         8          9           10          11
        /// 12 | 42 | 162 | 642 | 2,562 | 10,242 | 40,962 | 163,842 | 655,362 | 2,621,442 | 10,485,762 | 41,943,042
        ///
        /// The subsequence totals are
        /// 12 | 54 | 216 | 858 | 3,420 | 13,662 | 54,624 | 218,466 | 873,828 | 3,495,270 | 13,981,032 | 55,924,074 
        ///
        /// The sequence of faces for n = 0 ... 11 is
        /// 0    1     2      3       4       5        6         7         8           9           10           11
        /// 20 | 80 | 320 | 1,280 | 5,120 | 20,480 | 81,920 | 327,680 | 1,310,720 | 5,242,880 | 20,971,520 | 83,886,080
        /// 
        /// </summary>
        /// <param name="radius">The distance from the center-point to the initial set of vertices.</param>
        /// <param name="recursionLevel">The amount of times a recursive set of triangles are drawn within the initial
        /// icosahedron.</param>
        /// <returns>A pairing of vertices and faces that includes indices to draw into a polygon.</returns>
        public static VertsAndFaces NewIcoVertsAndFaces(float radius, int recursionLevel)
        {
            List<Vector3> vertList = new List<Vector3>();
            Dictionary<long, int> middlePointIndexCache = new Dictionary<long, int>();

            // create 12 vertices of a icosahedron
            float t = (1 + Mathf.Sqrt(5)) / 2;

            vertList.Add(new Vector3(-1, t, 0).normalized * radius); // 0
            vertList.Add(new Vector3(1, t, 0).normalized * radius); // 1

            vertList.Add(new Vector3(0, 1, t).normalized * radius); // 5
            vertList.Add(new Vector3(0, 1, -t).normalized * radius); // 7

            vertList.Add(new Vector3(-t, 0, 1).normalized * radius); // 11
            vertList.Add(new Vector3(-t, 0, -1).normalized * radius); //10

            vertList.Add(new Vector3(t, 0, -1).normalized * radius); // 8
            vertList.Add(new Vector3(t, 0, 1).normalized * radius); // 9

            vertList.Add(new Vector3(0, -1, -t).normalized * radius); // 6
            vertList.Add(new Vector3(0, -1, t).normalized * radius); // 4


            vertList.Add(new Vector3(-1, -t, 0).normalized * radius); // 2
            vertList.Add(new Vector3(1, -t, 0).normalized * radius); // 3

            // create 20 triangles of the icosahedron
            List<TriangleIndices> faces = new List<TriangleIndices>
            {
                // 5 faces around point 0
                new(0, 2, 1),
                new(0, 1, 3),
                new(0, 4, 2),
                new(0, 3, 5),
                new(0, 5, 4),

                new(1, 6, 3),
                new(1, 2, 7),
                new(1, 7, 6),

                new(3, 6, 8),
                new(3, 8, 5),

                new(2, 4, 9),
                new(2, 9, 7),

                new(4, 5, 10),
                new(4, 10, 9),
                new(5, 8, 10),

                // 5 faces around point 11
                new(6, 7, 11),
                new(7, 9, 11),
                new(9, 10, 11),
                new(8, 11, 10),
                new(8, 6, 11)
            };

            // refine triangles
            for (int i = 0; i < recursionLevel; i++)
            {
                List<TriangleIndices> faces2 = new List<TriangleIndices>();
                foreach (TriangleIndices tri in faces)
                {
                    // replace triangle by 4 triangles
                    int a = GetMiddlePoint(tri.v1, tri.v2, ref vertList, ref middlePointIndexCache, radius);
                    int b = GetMiddlePoint(tri.v2, tri.v3, ref vertList, ref middlePointIndexCache, radius);
                    int c = GetMiddlePoint(tri.v3, tri.v1, ref vertList, ref middlePointIndexCache, radius);

                    faces2.Add(new TriangleIndices(tri.v1, a, c));
                    faces2.Add(new TriangleIndices(tri.v2, b, a));
                    faces2.Add(new TriangleIndices(tri.v3, c, b));
                    faces2.Add(new TriangleIndices(a, b, c));
                }

                faces = faces2;
            }

            return new VertsAndFaces(vertList.ToArray(), faces.ToArray());
        }

        static int GetMiddlePoint(
            int p1,
            int p2,
            ref List<Vector3> vertices,
            ref Dictionary<long, int> cache,
            float radius)
        {
            // return index of point in the middle of p1 and p2

            // first check if we have it already
            bool firstIsSmaller = p1 < p2;
            long smallerIndex = firstIsSmaller ? p1 : p2;
            long greaterIndex = firstIsSmaller ? p2 : p1;
            long key = (smallerIndex << 32) + greaterIndex;

            if (cache.TryGetValue(key, out int ret))
            {
                return ret;
            }

            // not in cache, calculate it
            Vector3 point1 = vertices[p1];
            Vector3 point2 = vertices[p2];
            Vector3 middle = new(
                (point1.x + point2.x) / 2,
                (point1.y + point2.y) / 2,
                (point1.z + point2.z) / 2
            );

            // add vertex makes sure point is on unit sphere
            int i = vertices.Count;
            vertices.Add(middle.normalized * radius);

            // store it, return index
            cache.Add(key, i);

            return i;
        }

        /// <summary>
        /// 
        ///          _----------_,
        ///        ,"__         _-:, 
        ///       /    ""--_--""...:\
        ///      /         |.........\
        ///     /          |..........\
        ///    /,         _'_........./:
        ///    ! -,    _-"   "-_... ,;;:
        ///    \   -_-"         "-_/;;;;
        ///     \   \             /;;;;'
        ///      \   \           /;;;;
        ///       '.  \         /;;;'
        ///         "-_\_______/;;'
        /// 
        /// </summary>
        /// <param name="r">The radius from the center-point to any vertex.</param>
        /// <returns>A pairing of vertices and faces that includes indices to draw into a polygon.</returns>
        public static VertsAndFaces NewDodecVertsAndFaces(float r)
        {
            // Calculate constants that will be used to generate vertices
            float phi = (Mathf.Sqrt(5) - 1) / 2; // The golden ratio

            float a = 1f / Mathf.Sqrt(3);
            float b = a / phi;
            float c = a * phi;

            // Generate each vertex
            List<Vector3> vertices = new List<Vector3>();

            foreach (int i in new[] {-1, 1})
            {
                foreach (int j in new[] {-1, 1})
                {
                    vertices.Add(new Vector3(0, i * c * r, j * b * r));
                    vertices.Add(new Vector3(i * c * r, j * b * r, 0));
                    vertices.Add(new Vector3(i * b * r, 0, j * c * r));

                    vertices.AddRange(new[] {-1, 1}
                        .Select(k => new Vector3(i * a * r, j * a * r, k * a * r)));
                }
            }

            List<TriangleIndices> faces = new List<TriangleIndices>
            {
                // 0, 1, 3, 11, 13
                new(0, 1, 3),
                new(0, 11, 1),
                new(0, 13, 11),
                // 0, 2, 3, 8, 10
                new(10, 2, 8),
                new(10, 3, 2),
                new(10, 0, 3),
                // 0, 10, 12, 13, 18
                new(10, 13, 0),
                new(10, 12, 13),
                new(10, 18, 12),
                // 1, 2, 3, 4, 7
                new(4, 3, 1),
                new(7, 3, 4),
                new(2, 3, 7),
                // 1, 4, 5, 11, 14
                new(1, 11, 4),
                new(11, 14, 4),
                new(14, 5, 4),
                // 2, 6, 7, 8, 9
                new(9, 2, 7),
                new(9, 8, 2),
                new(9, 6, 8),
                // 4, 5, 7, 9, 15
                new(9, 7, 4),
                new(9, 4, 5),
                new(9, 5, 15),
                // 5, 14, 15, 17, 19
                new(19, 14, 17),
                new(19, 5, 14),
                new(19, 15, 5),
                // 6, 8, 10, 16, 18
                new(6, 10, 8),
                new(6, 16, 10),
                new(16, 18, 10),
                // 6, 9, 15, 16, 19
                new(16, 6, 9),
                new(16, 9, 19),
                new(19, 9, 15),
                // 11, 12, 13, 14, 17
                new(11, 13, 14),
                new(13, 12, 14),
                new(12, 17, 14),
                // 12, 16, 17, 18, 19
                new(12, 19, 17),
                new(12, 18, 19),
                new(18, 16, 19)
            };

            VertsAndFaces dhvf = new(vertices.ToArray(), faces.ToArray());

            return dhvf;
        }
        
        #endregion
    }
}