#nullable enable
using UnityEngine;
using UnityEngine.InputSystem;

namespace MusicHistory.Themes
{
    /// <summary>
    /// Orbit camera for the themes ring: right-drag orbits around a pivot on the ring plane,
    /// left-drag (or middle-drag) pans by dragging the plane under the pointer, the wheel zooms
    /// toward the point under the pointer. Keys: W A S D / arrows pan, Q E rotate, Z X zoom,
    /// Shift is faster. Motion is smoothed; <see cref="SetView"/> with immediate = true (edit-mode
    /// validation) applies at once. All devices are null-checked (none exist in batch mode).
    /// </summary>
    public sealed class ThemesOrbitCamera : MonoBehaviour
    {
        [Tooltip("Degrees per pixel of right-drag.")]
        public float OrbitSensitivity = .22f;
        [Tooltip("Zoom factor per wheel notch.")]
        [Range(.5f, .98f)] public float ZoomStep = .86f;
        [Min(.5f)] public float MinDistance = 4f;
        [Min(1f)] public float MaxDistance = 800f;
        [Range(1f, 89f)] public float MinPitch = 8f;
        [Range(1f, 89.9f)] public float MaxPitch = 89.5f;
        [Tooltip("Smoothing rate (1/s); 0 = no smoothing.")]
        [Min(0f)] public float Smoothing = 14f;
        [Tooltip("Pixels the pointer must travel before a left press becomes a pan instead of a click.")]
        public float DragThresholdPixels = 6f;

        /// <summary>False while a script owns the camera.</summary>
        public bool InputEnabled { get; set; } = true;
        /// <summary>True while the user is dragging (the viewer pauses hover picking).</summary>
        public bool Dragging { get; private set; }

        public Vector3 Pivot => pivot;
        public float Distance => distance;
        public float Yaw => yaw;
        public float Pitch => pitch;

        Vector3 pivot, pivotGoal;
        float distance = 80f, distanceGoal = 80f;
        float yaw, yawGoal, pitch = 60f, pitchGoal = 60f;
        bool initialized;
        Vector2 leftPress;
        bool leftDown, leftPanning;
        Camera? cam;

        Camera Cam => cam != null ? cam : cam = GetComponent<Camera>();

        /// <summary>Points the camera at <paramref name="target"/> from <paramref name="yawDeg"/> / <paramref name="pitchDeg"/> at <paramref name="dist"/>.</summary>
        public void SetView(Vector3 target, float dist, float yawDeg, float pitchDeg, bool immediate)
        {
            pivotGoal = target;
            distanceGoal = Mathf.Clamp(dist, MinDistance, MaxDistance);
            yawGoal = yawDeg;
            pitchGoal = Mathf.Clamp(pitchDeg, MinPitch, MaxPitch);
            if (immediate || !initialized)
            {
                pivot = pivotGoal;
                distance = distanceGoal;
                yaw = yawGoal;
                pitch = pitchGoal;
                initialized = true;
                Apply();
            }
        }

        /// <summary>Adopts a pose placed by someone else (framing): pivot = where the view ray meets the plane y = 0.</summary>
        public void AdoptTransform(bool immediate)
        {
            Transform t = transform;
            Vector3 f = t.forward;
            float d = f.y < -1e-3f ? -t.position.y / f.y : Mathf.Max(distance, 10f);
            Vector3 target = t.position + f * d;
            float yawDeg = Mathf.Atan2(f.x, f.z) * Mathf.Rad2Deg;
            float pitchDeg = Mathf.Asin(Mathf.Clamp(-f.y, -1f, 1f)) * Mathf.Rad2Deg;
            SetView(target, d, yawDeg, pitchDeg, immediate);
        }

        public static Quaternion Rotation(float yawDeg, float pitchDeg) => Quaternion.Euler(pitchDeg, yawDeg, 0f);

        void Apply()
        {
            Quaternion rotation = Rotation(yaw, pitch);
            transform.SetPositionAndRotation(pivot - rotation * Vector3.forward * distance, rotation);
            Camera c = Cam;
            if (c != null) c.farClipPlane = Mathf.Max(c.farClipPlane, distance * 4f + 200f);
        }

        void Update()
        {
            if (!initialized) AdoptTransform(true);
            if (InputEnabled) ReadInput();
            float k = Smoothing > 0 ? 1f - Mathf.Exp(-Smoothing * Time.unscaledDeltaTime) : 1f;
            pivot = Vector3.Lerp(pivot, pivotGoal, k);
            distance = Mathf.Lerp(distance, distanceGoal, k);
            yaw = Mathf.LerpAngle(yaw, yawGoal, k);
            pitch = Mathf.Lerp(pitch, pitchGoal, k);
            Apply();
        }

        void ReadInput()
        {
            Mouse? mouse = Mouse.current;
            Keyboard? keyboard = Keyboard.current;
            bool fast = keyboard != null && (keyboard.leftShiftKey.isPressed || keyboard.rightShiftKey.isPressed);
            float speed = fast ? 3f : 1f;
            Dragging = false;

            if (mouse != null)
            {
                Vector2 pointer = mouse.position.ReadValue();
                Vector2 delta = mouse.delta.ReadValue();
                if (mouse.rightButton.isPressed)
                {
                    yawGoal += delta.x * OrbitSensitivity;
                    pitchGoal = Mathf.Clamp(pitchGoal - delta.y * OrbitSensitivity, MinPitch, MaxPitch);
                    Dragging = delta.sqrMagnitude > 0 || Dragging;
                }

                if (mouse.leftButton.wasPressedThisFrame)
                {
                    leftDown = true;
                    leftPanning = false;
                    leftPress = pointer;
                }
                if (!mouse.leftButton.isPressed) leftDown = leftPanning = false;
                if (leftDown && !leftPanning && (pointer - leftPress).sqrMagnitude > DragThresholdPixels * DragThresholdPixels)
                    leftPanning = true;
                bool panning = leftPanning || mouse.middleButton.isPressed;
                if (panning && delta.sqrMagnitude > 0)
                {
                    PanByPointer(pointer - delta, pointer);
                    Dragging = true;
                }
                if (leftPanning) Dragging = true;

                float scroll = mouse.scroll.ReadValue().y;
                if (Mathf.Abs(scroll) > .01f)
                {
                    // Windows reports 120 per notch unless the Input System normalizes scroll to +-1.
                    float notches = Mathf.Clamp(Mathf.Abs(scroll) > 10f ? scroll / 120f : scroll, -5f, 5f);
                    ZoomToward(pointer, Mathf.Pow(ZoomStep, notches * (fast ? 2f : 1f)));
                }
            }

            if (keyboard == null) return;
            Vector3 move = Vector3.zero;
            Quaternion flat = Quaternion.Euler(0f, yawGoal, 0f);
            if (keyboard.wKey.isPressed || keyboard.upArrowKey.isPressed) move += flat * Vector3.forward;
            if (keyboard.sKey.isPressed || keyboard.downArrowKey.isPressed) move -= flat * Vector3.forward;
            if (keyboard.dKey.isPressed || keyboard.rightArrowKey.isPressed) move += flat * Vector3.right;
            if (keyboard.aKey.isPressed || keyboard.leftArrowKey.isPressed) move -= flat * Vector3.right;
            float dt = Time.unscaledDeltaTime;
            if (move.sqrMagnitude > 0) pivotGoal += move.normalized * (distanceGoal * .8f * speed * dt);
            if (keyboard.qKey.isPressed) yawGoal += 70f * speed * dt;
            if (keyboard.eKey.isPressed) yawGoal -= 70f * speed * dt;
            if (keyboard.zKey.isPressed) distanceGoal = Mathf.Clamp(distanceGoal * Mathf.Pow(.35f, speed * dt), MinDistance, MaxDistance);
            if (keyboard.xKey.isPressed) distanceGoal = Mathf.Clamp(distanceGoal / Mathf.Pow(.35f, speed * dt), MinDistance, MaxDistance);
        }

        /// <summary>Where the pointer's ray meets the ring plane (y = 0), under the goal pose.</summary>
        bool PlanePoint(Vector2 screen, out Vector3 point)
        {
            point = default;
            Camera c = Cam;
            if (c == null) return false;
            // Rays from the goal pose, so smoothing never makes the drag lag or overshoot.
            Quaternion rotation = Rotation(yawGoal, pitchGoal);
            Vector3 position = pivotGoal - rotation * Vector3.forward * distanceGoal;
            Vector3 viewport = c.ScreenToViewportPoint(screen);
            float tanV = Mathf.Tan(c.fieldOfView * .5f * Mathf.Deg2Rad);
            float tanH = tanV * c.aspect;
            Vector3 dir = rotation * new Vector3((viewport.x * 2f - 1f) * tanH, (viewport.y * 2f - 1f) * tanV, 1f);
            if (dir.y > -1e-3f) return false;
            float t = -position.y / dir.y;
            if (t <= 0 || t > distanceGoal * 20f) return false;
            point = position + dir * t;
            return true;
        }

        void PanByPointer(Vector2 from, Vector2 to)
        {
            if (!PlanePoint(from, out Vector3 a) || !PlanePoint(to, out Vector3 b)) return;
            pivotGoal += a - b;
        }

        void ZoomToward(Vector2 pointer, float factor)
        {
            float before = distanceGoal;
            float after = Mathf.Clamp(before * factor, MinDistance, MaxDistance);
            if (PlanePoint(pointer, out Vector3 p))
                pivotGoal += (p - pivotGoal) * (1f - after / before);
            distanceGoal = after;
        }
    }
}
