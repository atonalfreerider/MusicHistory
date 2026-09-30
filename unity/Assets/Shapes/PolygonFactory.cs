using Shapes.Lines;
using UnityEngine;

namespace Shapes
{
    public class PolygonFactory : MonoBehaviour
    {
        public static PolygonFactory Instance;
        public PolygonPool PolygonPool;
        public Material mainMat;

        // regular polygons
        [HideInInspector] public Polygon tetra, icosahedron0;
        
        void Awake()
        {
            Instance = this;
            BuildPolygons();
            StaticLink.InitStaticLink(this);
        }

        // INIT
        void BuildPolygons()
        {
            tetra = NewPoly(mainMat);
            Polyhedra.VertsAndFaces tetraVertsAndFaces = Polyhedra.NewTetraVertsAndFaces(1);

            tetra.Draw3DPoly(tetraVertsAndFaces.verts, Polyhedra.IndicesFromTris(tetraVertsAndFaces.faces));
            tetra.name = "tetrahedron";
            tetra.SetColor(Color.white);
            tetra.transform.SetParent(transform, false);
            tetra.gameObject.SetActive(false);
            
            icosahedron0 = NewPoly(mainMat);
            Polyhedra.VertsAndFaces ivaf = Polyhedra.NewIcoVertsAndFaces(1, 0);
            Polyhedra.NewPolyhedron(icosahedron0, ivaf.verts, ivaf.faces, false);
            icosahedron0.name = "icosahedron0";
            icosahedron0.SetColor(Color.white);
            icosahedron0.transform.SetParent(transform, false);
            icosahedron0.gameObject.SetActive(false);
        }

        public static Polygon NewPoly(Material passMat)
        {
            Polygon newPoly = new GameObject("Polygon").AddComponent<Polygon>();
            AddMesh(newPoly.gameObject, newPoly, passMat);
            newPoly.rend = newPoly.gameObject.GetComponent<Renderer>();

            return newPoly;
        }

        public static void AddMesh(GameObject polyGO, Polygon basePoly, Material passMat)
        {
            // add mesh;
            MeshFilter filter = polyGO.AddComponent<MeshFilter>();
            filter.sharedMesh = new Mesh();
            MeshRenderer meshRend = polyGO.AddComponent<MeshRenderer>();
            meshRend.sharedMaterial = passMat;
            meshRend.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            meshRend.receiveShadows = false;
            meshRend.lightProbeUsage = UnityEngine.Rendering.LightProbeUsage.Off;
            basePoly.meshFilter = filter;
        }
    }
}