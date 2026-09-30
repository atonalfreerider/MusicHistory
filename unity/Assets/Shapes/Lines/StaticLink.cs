using UnityEngine;

namespace Shapes.Lines
{
    public class StaticLink : Polygon
    {
        // Persistent Objects;
        public Transform from;
        public Transform to;

        // Calibration Vars
        public float LW = .01f;
        public bool clipToEndpointSurfaces;
        public float endpointPadding = .06f;
        float length;
        public bool needsGlobalScale = false;

        // ACTION Functions;
        public void LinkFromTo(Transform graphic1, Transform graphic2)
        {
            from = graphic1;
            to = graphic2;
            DrawFromTo(from.transform, to.transform);
        }

        void DrawFromTo(Transform trans1, Transform trans2)
        {
            Vector3 start = trans1.position;
            Vector3 end = trans2.position;
            if (clipToEndpointSurfaces)
            {
                Vector3 direction = end - start;
                float distance = direction.magnitude;
                if (distance > .000001f)
                {
                    direction /= distance;
                    float startInset = EndpointRadius(trans1) + endpointPadding;
                    float endInset = EndpointRadius(trans2) + endpointPadding;
                    float insetScale = Mathf.Min(1f, distance / Mathf.Max(
                        .000001f, startInset + endInset));
                    start += direction * startInset * insetScale;
                    end -= direction * endInset * insetScale;
                }
            }

            transform.position = Vector3.Lerp(start, end, .5f);
            transform.LookAt(end);
            transform.Rotate(Vector3.right, 90);
            SetLength(Vector3.Distance(start, end));
        }

        static float EndpointRadius(Transform endpoint)
        {
            Vector3 scale = endpoint.lossyScale;
            return .5f * Mathf.Max(Mathf.Abs(scale.x), Mathf.Abs(scale.y), Mathf.Abs(scale.z));
        }

        public void DrawFromTo(Vector3 pos1, Vector3 pos2)
        {
            transform.position = Vector3.Lerp(pos1, pos2, .5f);
            transform.LookAt(pos1);
            transform.Rotate(Vector3.right, 90);
            SetLength(Vector3.Distance(pos1, pos2));
        }

        public void UpdateLink()
        {
            DrawFromTo(from.transform, to.transform);
        }

        public void SetLength(float D)
        {
            length = D;
            if (needsGlobalScale)
            {
                Vector3 global = transform.parent.lossyScale;
                transform.localScale = new Vector3(LW / global.x, length / global.y, LW / global.z);
                return;
            }

            transform.localScale = new Vector3(LW, length, LW);
        }

        public float DrawPartial(float prct)
        {
            float D = Vector3.Distance(from.position, to.position);
            SetLength(D * prct);
            return D - length;
        }

        public static StaticLink prototypeStaticLink;

        public static void InitStaticLink(PolygonFactory polygonFactory)
        {
            GameObject newLinkGO = new("StaticLink");
            newLinkGO.transform.SetParent(polygonFactory.transform, false);

            prototypeStaticLink = newLinkGO.AddComponent<StaticLink>();
            PolygonFactory.AddMesh(newLinkGO, prototypeStaticLink, polygonFactory.mainMat);
            prototypeStaticLink.rend = newLinkGO.GetComponent<Renderer>();
            prototypeStaticLink.SetColor(Color.white);

            prototypeStaticLink.DrawRegPoly(1, 6, Mathf.PI / 6f, 1, 0);
            prototypeStaticLink.transform.SetParent(polygonFactory.transform, false);
            prototypeStaticLink.transform.localScale = Vector3.one * .01f;
            
            prototypeStaticLink.gameObject.SetActive(false);
        }
    }
}
