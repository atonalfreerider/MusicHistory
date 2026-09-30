using System.Collections.Generic;
using System.Linq;
using UnityEngine;

namespace Shapes
{
    public class PolygonPool : MonoBehaviour
    {
#if UNITY_ANDROID
        const int MaximumPoolSize = 300;
#else
        const int MaximumPoolSize = 1000;
#endif

        readonly Queue<Polygon> unusedPolygons = new();

        public Polygon PolygonFromPool()
        {
            if (unusedPolygons.Any())
            {
                Polygon poly = unusedPolygons.Dequeue();
                poly.gameObject.SetActive(true);
                return poly;
            }

            return PolygonFactory.NewPoly(PolygonFactory.Instance.mainMat);
        }

        public void ReleaseBackToPool(Polygon poly)
        {
            if (unusedPolygons.Count >= MaximumPoolSize)

            {
                // must destroy mesh filter's mesh directly otherwise memory leaks
                // see:
                // https://www.reddit.com/r/Unity3D/comments/33t6ao/deleting_an_object_containing_a_procedurally/
                // https://forum.unity.com/threads/memory-problems-with-procedurally-generated-meshes.58469/
                Destroy(poly.meshFilter.sharedMesh);
                Destroy(poly.gameObject);
            }
            else
            {
                poly.gameObject.SetActive(false);
                poly.transform.SetParent(transform, false);

                unusedPolygons.Enqueue(poly);
            }
        }
    }
}