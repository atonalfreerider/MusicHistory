#nullable enable
using UnityEngine;
using UnityEngine.InputSystem;

namespace MusicHistory.Viewer
{
    /// <summary>
    /// Free-fly camera (from Unity-FDG): right mouse drag looks around, W/A/S/D move, Q/E move
    /// down/up, the scroll wheel dollies, Shift moves faster.
    ///
    /// Fork additions: <see cref="InputEnabled"/> lets the walkthrough own the camera, and
    /// <see cref="SyncRotationFromTransform"/> re-reads yaw/pitch after a scripted flight so the
    /// next right-drag continues from where the camera is instead of snapping back. All input
    /// devices are null-checked (no keyboard or mouse in batch mode).
    /// </summary>
    public class CameraControl : MonoBehaviour
    {
        public float Sensitivity = 1f;
        public float Speed = 1f;
        [Tooltip("Multiplier while Shift is held.")]
        public float FastMultiplier = 4f;
        [Tooltip("Fraction of Speed moved per scroll notch.")]
        public float ScrollDolly = .25f;

        /// <summary>False while a script (the walkthrough) drives the camera.</summary>
        public bool InputEnabled { get; set; } = true;

        const float MinimumPitch = -85f;
        const float MaximumPitch = 85f;

        // x = yaw, y = pitch (degrees), as composed in Update.
        Vector2 rotation = Vector2.zero;
        bool synced;

        void Awake() => SyncRotationFromTransform();

        /// <summary>Adopts the transform's current orientation as the mouse-look state.</summary>
        public void SyncRotationFromTransform()
        {
            Vector3 forward = transform.forward;
            float yaw = Mathf.Atan2(forward.x, forward.z) * Mathf.Rad2Deg;
            float pitch = Mathf.Asin(Mathf.Clamp(forward.y, -1f, 1f)) * Mathf.Rad2Deg;
            rotation = new Vector2(yaw, Mathf.Clamp(pitch, MinimumPitch, MaximumPitch));
            synced = true;
        }

        void Update()
        {
            if (!InputEnabled) return;
            if (!synced) SyncRotationFromTransform();
            Mouse? mouse = Mouse.current;
            if (mouse != null && mouse.rightButton.isPressed)
            {
                rotation += mouse.delta.ReadValue() * Sensitivity;
                rotation.x = Mathf.Repeat(rotation.x + 180f, 360f) - 180f;
                rotation.y = Mathf.Clamp(rotation.y, MinimumPitch, MaximumPitch);
                transform.localRotation = Quaternion.AngleAxis(rotation.x, Vector3.up) *
                                          Quaternion.AngleAxis(rotation.y, -Vector3.right);
            }

            float speed = Speed;
            Keyboard? keyboard = Keyboard.current;
            if (keyboard != null && (keyboard.leftShiftKey.isPressed || keyboard.rightShiftKey.isPressed))
                speed *= FastMultiplier;
            if (mouse != null)
            {
                // Windows reports 120 per notch unless the Input System normalizes scroll to +-1.
                float scroll = mouse.scroll.ReadValue().y;
                if (Mathf.Abs(scroll) > .01f)
                {
                    float notches = Mathf.Clamp(Mathf.Abs(scroll) > 10f ? scroll / 120f : scroll, -5f, 5f);
                    transform.position += transform.forward * (notches * ScrollDolly * speed);
                }
            }

            MoveCamera(keyboard, speed);
        }

        void MoveCamera(Keyboard? keyboard, float speed)
        {
            if (keyboard == null) return;
            Vector3 move = Vector3.zero;
            if (keyboard.wKey.isPressed) move += transform.forward;
            if (keyboard.sKey.isPressed) move -= transform.forward;
            if (keyboard.dKey.isPressed) move += transform.right;
            if (keyboard.aKey.isPressed) move -= transform.right;
            if (keyboard.eKey.isPressed) move += Vector3.up;
            if (keyboard.qKey.isPressed) move += Vector3.down;
            if (move.sqrMagnitude > 0) transform.position += move.normalized * (Time.unscaledDeltaTime * speed);
        }

        /// <summary>True while the user is steering (hover picking pauses meanwhile).</summary>
        public static bool UserIsNavigating()
        {
            if (Mouse.current != null && Mouse.current.rightButton.isPressed) return true;
            Keyboard? k = Keyboard.current;
            if (k == null) return false;
            return k.wKey.isPressed || k.aKey.isPressed || k.sKey.isPressed ||
                   k.dKey.isPressed || k.qKey.isPressed || k.eKey.isPressed;
        }
    }
}
