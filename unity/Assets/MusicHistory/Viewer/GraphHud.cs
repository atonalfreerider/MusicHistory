#nullable enable
using System.Collections.Generic;
using System.Text;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace MusicHistory.Viewer
{
    /// <summary>
    /// Screen overlay: legend + controls (top left), the focused song's facts (top right: title,
    /// artist, year, key, BPM, main loop, parent chain, tree-edge evidence) and the walkthrough
    /// panel (bottom). Nothing is clickable, so no EventSystem is needed. All strings from the
    /// database are shown inside noparse tags; the database never holds lyrics.
    /// </summary>
    public sealed class GraphHud : MonoBehaviour
    {
        sealed class Panel
        {
            public GameObject Root = null!;
            public RectTransform Rect = null!;
            public TextMeshProUGUI Text = null!;
            public float Width;
        }

        const float Padding = 14f;
        static readonly Color PanelColor = new(.035f, .04f, .05f, .80f);
        public const string Muted = "#9aa3ad";

        Canvas canvas = null!;
        Panel legend = null!, info = null!, tour = null!;
        RectTransform progressFill = null!;
        Image progressFillImage = null!;
        SongGraphLoader loader = null!;
        string legendBody = "";

        public bool HelpVisible { get; private set; }
        public string InfoText => info.Text.text;
        public string TourText => tour.Text.text;
        public string LegendText => legend.Text.text;
        public Canvas Canvas => canvas;

        public void Build(SongGraphLoader owner)
        {
            loader = owner;
            GameObject canvasObject = new("MusicHistory HUD", typeof(RectTransform), typeof(Canvas), typeof(CanvasScaler));
            canvasObject.transform.SetParent(transform, false);
            canvas = canvasObject.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = 300;
            CanvasScaler scaler = canvasObject.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1920, 1080);
            scaler.matchWidthOrHeight = .5f;

            legend = CreatePanel("Legend", new Vector2(0, 1), new Vector2(0, 1), new Vector2(20, -20), 660, TextAlignmentOptions.TopLeft, 17);
            info = CreatePanel("Song Info", new Vector2(1, 1), new Vector2(1, 1), new Vector2(-20, -20), 540, TextAlignmentOptions.TopLeft, 19);
            tour = CreatePanel("Walkthrough", new Vector2(.5f, 0), new Vector2(.5f, 0), new Vector2(0, 16), 1240, TextAlignmentOptions.TopLeft, 19);

            GameObject back = new("Progress", typeof(RectTransform), typeof(CanvasRenderer), typeof(Image));
            back.transform.SetParent(tour.Rect, false);
            Image backImage = back.GetComponent<Image>();
            backImage.color = new Color(1, 1, 1, .12f);
            backImage.raycastTarget = false;
            RectTransform backRect = back.GetComponent<RectTransform>();
            backRect.anchorMin = new Vector2(0, 0);
            backRect.anchorMax = new Vector2(1, 0);
            backRect.pivot = new Vector2(.5f, 0);
            backRect.offsetMin = new Vector2(Padding, 8);
            backRect.offsetMax = new Vector2(-Padding, 13);

            GameObject fill = new("Fill", typeof(RectTransform), typeof(CanvasRenderer), typeof(Image));
            fill.transform.SetParent(backRect, false);
            progressFillImage = fill.GetComponent<Image>();
            progressFillImage.color = new Color(.95f, .96f, .98f, .9f);
            progressFillImage.raycastTarget = false;
            progressFill = fill.GetComponent<RectTransform>();
            progressFill.anchorMin = Vector2.zero;
            progressFill.anchorMax = new Vector2(0, 1);
            progressFill.offsetMin = Vector2.zero;
            progressFill.offsetMax = Vector2.zero;

            SetText(info, "");
            SetText(tour, "");
        }

        Panel CreatePanel(string panelName, Vector2 anchor, Vector2 pivot, Vector2 position, float width,
            TextAlignmentOptions alignment, float fontSize)
        {
            GameObject go = new(panelName, typeof(RectTransform), typeof(CanvasRenderer), typeof(Image));
            go.transform.SetParent(canvas.transform, false);
            Image image = go.GetComponent<Image>();
            image.color = PanelColor;
            image.raycastTarget = false;
            RectTransform rect = go.GetComponent<RectTransform>();
            rect.anchorMin = anchor;
            rect.anchorMax = anchor;
            rect.pivot = pivot;
            rect.anchoredPosition = position;
            rect.sizeDelta = new Vector2(width, 80);

            GameObject textObject = new("Text", typeof(RectTransform), typeof(CanvasRenderer));
            textObject.transform.SetParent(go.transform, false);
            TextMeshProUGUI text = textObject.AddComponent<TextMeshProUGUI>();
            text.fontSize = fontSize;
            text.color = new Color(.93f, .94f, .96f, 1f);
            text.alignment = alignment;
            text.richText = true;
            text.raycastTarget = false;
            text.textWrappingMode = TextWrappingModes.Normal;
            text.lineSpacing = 4;
            RectTransform textRect = text.rectTransform;
            textRect.anchorMin = Vector2.zero;
            textRect.anchorMax = Vector2.one;
            textRect.offsetMin = new Vector2(Padding, Padding);
            textRect.offsetMax = new Vector2(-Padding, -Padding);
            return new Panel { Root = go, Rect = rect, Text = text, Width = width };
        }

        static void SetText(Panel panel, string text, float extraBottom = 0)
        {
            bool show = !string.IsNullOrEmpty(text);
            if (panel.Root.activeSelf != show) panel.Root.SetActive(show);
            if (!show) return;
            if (panel.Text.text != text) panel.Text.text = text;
            Vector2 preferred = panel.Text.GetPreferredValues(text, panel.Width - 2 * Padding, 0);
            panel.Rect.sizeDelta = new Vector2(panel.Width, Mathf.Ceil(preferred.y) + 2 * Padding + extraBottom);
        }

        public static string Esc(string? s) =>
            string.IsNullOrEmpty(s) ? "" : "<noparse>" + s!.Replace("</noparse>", "") + "</noparse>";

        public void SetLegend(string body)
        {
            legendBody = body;
            RefreshLegend();
        }

        public void ToggleHelp()
        {
            HelpVisible = !HelpVisible;
            RefreshLegend();
        }

        void RefreshLegend()
        {
            string help = HelpVisible
                ? $"\n<color={Muted}>Mouse</color>  hover shows influences · click selects · right-drag looks · wheel dollies" +
                  $"\n<color={Muted}>Move</color>  W A S D, Q E (Shift = fast) · R reset view · L all labels · V secondary edges · F live layout (time locked)" +
                  $"\n<color={Muted}>Walkthrough</color>  1 lineage · 2 subtree · 3 chronological · Enter start · Space pause · N/→ next · B/← back · Esc exit · C compare in C / 120 BPM"
                : $"\n<color={Muted}>H</color> controls";
            SetText(legend, legendBody + help);
        }

        public void ShowSong(SongNode? node) => SetText(info, node == null ? "" : SongInfo(node));

        public void ShowTour(string? text, float progress, Color barColor)
        {
            SetText(tour, text ?? "", 10);
            if (string.IsNullOrEmpty(text)) return;
            progressFill.anchorMax = new Vector2(Mathf.Clamp01(progress), 1);
            progressFillImage.color = barColor;
        }

        string SongInfo(SongNode node)
        {
            SongRecord s = node.Song;
            StringBuilder b = new();
            b.Append("<size=125%><b>").Append(Esc(s.Title)).Append("</b></size>\n");
            b.Append(Esc(s.Artist)).Append(" · ").Append(s.Year);
            if (!string.IsNullOrEmpty(s.ReleaseDate) && s.ReleaseDate != s.Year.ToString())
                b.Append($" <color={Muted}>({Esc(s.ReleaseDate)})</color>");
            b.Append('\n');
            b.Append($"<color={Muted}>Key</color> {Esc(s.KeyName)}   <color={Muted}>BPM</color> {SongPalette.Invariant(s.NativeBpm, "0.#")}" +
                     $"   <color={Muted}>Beats/bar</color> {SongPalette.Invariant(s.BeatsPerBar, "0.##")}\n");
            b.Append($"<color={Muted}>Main loop</color> {(string.IsNullOrEmpty(s.MainLoop) ? "—" : Esc(s.MainLoop))}\n");
            b.Append($"<color={Muted}>Lineage</color> {Lineage(node)}\n");
            b.Append($"<color={Muted}>Influenced by</color> {node.Incoming.Count} · <color={Muted}>influences</color> {node.Outgoing.Count}" +
                     $" · <color={Muted}>descendants</color> {s.Descendants}");
            if (s.CanonRank.HasValue) b.Append($" · <color={Muted}>canon rank</color> {s.CanonRank.Value}");
            if (node.TreeEdge != null)
            {
                EdgeRecord e = node.TreeEdge.Record;
                string color = SongPalette.ToHex(SongPalette.ChannelColor(node.TreeEdge.Channel));
                b.Append($"\n<color={Muted}>Via</color> <color={color}>{SongPalette.ChannelLabel(node.TreeEdge.Channel)}</color>" +
                         $" · {SongPalette.Invariant(e.ScoreBits, "0")} bits · z {SongPalette.Invariant(e.Z, "0.0")}");
                if (!string.IsNullOrEmpty(e.Evidence)) b.Append($"\n<size=90%><color={Muted}>{Esc(e.Evidence)}</color></size>");
            }
            if (!string.IsNullOrEmpty(s.Summary)) b.Append($"\n<size=85%><color={Muted}>{Esc(s.Summary)}</color></size>");
            return b.ToString();
        }

        /// <summary>"Root (1950) › … › Parent (1962) › <b>This</b>".</summary>
        public static string Lineage(SongNode node)
        {
            List<SongNode> chain = new();
            for (SongNode? n = node; n != null && chain.Count < 512; n = n.TreeParent) chain.Add(n);
            chain.Reverse();
            if (chain.Count == 1) return "root (no earlier influencer)";
            List<string> parts = new();
            for (int i = 0; i < chain.Count; i++)
            {
                if (chain.Count > 6 && i == 1)
                {
                    parts.Add("…");
                    i = chain.Count - 5;
                }
                SongNode n = chain[i];
                parts.Add(n == node ? $"<b>{Esc(n.Song.Title)}</b>" : $"{Esc(n.Song.Title)} ({n.Song.Year})");
            }
            return string.Join(" › ", parts);
        }

        /// <summary>Draws the HUD into <paramref name="cam"/> (screenshots) or back onto the screen (null).</summary>
        public void RenderInto(Camera? cam)
        {
            if (cam == null)
            {
                canvas.renderMode = RenderMode.ScreenSpaceOverlay;
                canvas.worldCamera = null;
            }
            else
            {
                canvas.renderMode = RenderMode.ScreenSpaceCamera;
                canvas.worldCamera = cam;
                canvas.planeDistance = cam.nearClipPlane + .05f;
            }
            ForceUpdate();
        }

        public void ForceUpdate()
        {
            foreach (Panel p in new[] { legend, info, tour })
                if (p.Root.activeSelf) p.Text.ForceMeshUpdate();
            Canvas.ForceUpdateCanvases();
        }
    }
}
