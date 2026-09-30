#nullable enable
using System.Collections.Generic;
using System.Text;
using MusicHistory.Viewer;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace MusicHistory.Themes
{
    /// <summary>
    /// Screen overlay of the themes viewer: the legend (top left: what the ring, positions and
    /// colours mean, plus the controls), the hover card next to the focused song (title, artist,
    /// year, singer, text source and the top three themes with bars and scores — nothing else,
    /// and never lyrics), a status line at the bottom (what is playing, the theme filter) and the
    /// "back to the influence graph" button (top right). Nothing uses the EventSystem: the viewer
    /// hit-tests the button itself. Every string from the database is escaped (noparse).
    /// </summary>
    public sealed class ThemesHud : MonoBehaviour
    {
        sealed class Panel
        {
            public GameObject Root = null!;
            public RectTransform Rect = null!;
            public TextMeshProUGUI Text = null!;
            public Image Background = null!;
            public float Width;
        }

        sealed class ThemeRow
        {
            public TextMeshProUGUI Label = null!;
            public TextMeshProUGUI Score = null!;
            public RectTransform Fill = null!;
            public RectTransform Track = null!;
        }

        const float Padding = 14f;
        const float CardWidth = 430f;
        const float RowHeight = 42f;
        static readonly Color PanelColor = new(.035f, .04f, .05f, .82f);
        static readonly Color CardColor = new(.03f, .035f, .045f, .92f);
        static readonly Color ButtonColor = new(.06f, .07f, .09f, .85f);
        static readonly Color ButtonHotColor = new(.16f, .14f, .09f, .95f);

        Canvas canvas = null!;
        Panel legend = null!, status = null!, button = null!;
        RectTransform card = null!;
        Image cardAccent = null!;
        TextMeshProUGUI cardHeader = null!;
        readonly List<ThemeRow> rows = new();
        RectTransform progressTrack = null!, progressFill = null!;
        string legendBody = "";
        ThemeSongNode? cardNode;
        bool buttonHot;

        public bool HelpVisible { get; private set; }
        public Canvas Canvas => canvas;
        public string LegendText => legend.Text.text;
        public string StatusText => status.Root.activeSelf ? status.Text.text : "";
        public string ButtonText => button.Text.text;
        public RectTransform ButtonRect => button.Rect;
        public RectTransform CardRect => card;
        public bool CardVisible => card.gameObject.activeSelf;
        public ThemeSongNode? CardSong => cardNode;
        /// <summary>Bar fill fraction per row, as drawn (tests compare them to the scores).</summary>
        public float RowFillFraction(int i) => rows[i].Fill.anchorMax.x;

        /// <summary>Every string the hover card shows, in order (header, then label and score per row).</summary>
        public List<string> CardStrings()
        {
            List<string> all = new() { cardHeader.text };
            foreach (ThemeRow row in rows)
            {
                if (!row.Label.gameObject.activeInHierarchy) continue;
                all.Add(row.Label.text);
                all.Add(row.Score.text);
            }
            return all;
        }

        public void Build()
        {
            rows.Clear();
            cardNode = null;
            buttonHot = false;
            GameObject canvasObject = new("Lyric Themes HUD", typeof(RectTransform), typeof(Canvas), typeof(CanvasScaler));
            canvasObject.transform.SetParent(transform, false);
            canvas = canvasObject.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = 300;
            CanvasScaler scaler = canvasObject.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1920, 1080);
            scaler.matchWidthOrHeight = .5f;

            legend = CreatePanel("Legend", new Vector2(0, 1), new Vector2(0, 1), new Vector2(20, -20), 600, TextAlignmentOptions.TopLeft, 17, PanelColor);
            status = CreatePanel("Status", new Vector2(.5f, 1), new Vector2(.5f, 1), new Vector2(60, -20), 800, TextAlignmentOptions.Top, 19, PanelColor);
            button = CreatePanel("Back Button", new Vector2(1, 1), new Vector2(1, 1), new Vector2(-20, -20), 300, TextAlignmentOptions.Center, 19, ButtonColor);
            SetText(button, "<b>←  Influence graph</b>  <color=#9aa3ad>G</color>");

            // Playback progress under the status text.
            progressTrack = Bar(status.Rect, "Progress", new Color(1, 1, 1, .12f));
            progressTrack.anchorMin = new Vector2(0, 0);
            progressTrack.anchorMax = new Vector2(1, 0);
            progressTrack.pivot = new Vector2(.5f, 0);
            progressTrack.offsetMin = new Vector2(Padding, 8);
            progressTrack.offsetMax = new Vector2(-Padding, 12);
            progressFill = Bar(progressTrack, "Fill", ThemesPalette.Theme);
            progressFill.anchorMin = Vector2.zero;
            progressFill.anchorMax = new Vector2(0, 1);
            progressFill.offsetMin = progressFill.offsetMax = Vector2.zero;

            BuildCard();
            SetStatus(null, false);
        }

        void BuildCard()
        {
            GameObject go = new("Hover Card", typeof(RectTransform), typeof(CanvasRenderer), typeof(Image));
            go.transform.SetParent(canvas.transform, false);
            Image bg = go.GetComponent<Image>();
            bg.color = CardColor;
            bg.raycastTarget = false;
            card = go.GetComponent<RectTransform>();
            card.anchorMin = card.anchorMax = Vector2.zero;
            card.pivot = new Vector2(0, 1);

            cardAccent = Bar(card, "Accent", ThemesPalette.Neutral).GetComponent<Image>();
            RectTransform accent = cardAccent.rectTransform;
            accent.anchorMin = new Vector2(0, 1);
            accent.anchorMax = new Vector2(1, 1);
            accent.pivot = new Vector2(.5f, 1);
            accent.offsetMin = new Vector2(0, -4);
            accent.offsetMax = Vector2.zero;

            cardHeader = Text(card, "Header", 18, TextAlignmentOptions.TopLeft);
            RectTransform hr = cardHeader.rectTransform;
            hr.anchorMin = new Vector2(0, 1);
            hr.anchorMax = new Vector2(1, 1);
            hr.pivot = new Vector2(.5f, 1);

            for (int i = 0; i < 3; i++)
            {
                ThemeRow row = new()
                {
                    Label = Text(card, $"Theme {i + 1}", 16, TextAlignmentOptions.TopLeft),
                    Score = Text(card, $"Score {i + 1}", 16, TextAlignmentOptions.TopRight)
                };
                row.Track = Bar(card, $"Bar {i + 1}", new Color(1, 1, 1, .10f));
                row.Fill = Bar(row.Track, "Fill", ThemesPalette.Theme);
                row.Fill.anchorMin = Vector2.zero;
                row.Fill.anchorMax = new Vector2(0, 1);
                row.Fill.offsetMin = row.Fill.offsetMax = Vector2.zero;
                foreach (RectTransform rt in new[] { row.Label.rectTransform, row.Score.rectTransform, row.Track })
                {
                    rt.anchorMin = new Vector2(0, 1);
                    rt.anchorMax = new Vector2(1, 1);
                    rt.pivot = new Vector2(.5f, 1);
                }
                rows.Add(row);
            }
            go.SetActive(false);
        }

        static RectTransform Bar(Transform parent, string barName, Color color)
        {
            GameObject go = new(barName, typeof(RectTransform), typeof(CanvasRenderer), typeof(Image));
            go.transform.SetParent(parent, false);
            Image image = go.GetComponent<Image>();
            image.color = color;
            image.raycastTarget = false;
            return go.GetComponent<RectTransform>();
        }

        static TextMeshProUGUI Text(Transform parent, string textName, float size, TextAlignmentOptions alignment)
        {
            GameObject go = new(textName, typeof(RectTransform), typeof(CanvasRenderer));
            go.transform.SetParent(parent, false);
            TextMeshProUGUI text = go.AddComponent<TextMeshProUGUI>();
            text.fontSize = size;
            text.color = new Color(.93f, .94f, .96f, 1f);
            text.alignment = alignment;
            text.richText = true;
            text.raycastTarget = false;
            text.textWrappingMode = TextWrappingModes.Normal;
            text.overflowMode = TextOverflowModes.Overflow;
            text.lineSpacing = 2;
            return text;
        }

        Panel CreatePanel(string panelName, Vector2 anchor, Vector2 pivot, Vector2 position, float width,
            TextAlignmentOptions alignment, float fontSize, Color color)
        {
            GameObject go = new(panelName, typeof(RectTransform), typeof(CanvasRenderer), typeof(Image));
            go.transform.SetParent(canvas.transform, false);
            Image image = go.GetComponent<Image>();
            image.color = color;
            image.raycastTarget = false;
            RectTransform rect = go.GetComponent<RectTransform>();
            rect.anchorMin = anchor;
            rect.anchorMax = anchor;
            rect.pivot = pivot;
            rect.anchoredPosition = position;
            rect.sizeDelta = new Vector2(width, 80);
            TextMeshProUGUI text = Text(go.transform, "Text", fontSize, alignment);
            text.lineSpacing = 4;
            RectTransform textRect = text.rectTransform;
            textRect.anchorMin = Vector2.zero;
            textRect.anchorMax = Vector2.one;
            textRect.offsetMin = new Vector2(Padding, Padding);
            textRect.offsetMax = new Vector2(-Padding, -Padding);
            return new Panel { Root = go, Rect = rect, Text = text, Background = image, Width = width };
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

        // ------------------------------------------------------------------ legend

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
            string m = ThemesPalette.Muted;
            string help = HelpVisible
                ? $"\n<size=90%><color={m}>Mouse</color>  hover: details · click: play the excerpt (again: stop) · left-drag: pan · right-drag: orbit · wheel: zoom" +
                  $"\n<color={m}>Keys</color>  1–9, 0: show one theme · Esc: all themes · L: all labels · Space: stop · R: reset view · W A S D, Q E, Z X: move · G: influence graph · H: hide help</size>"
                : $"\n<size=90%><color={m}>H</color> help</size>";
            SetText(legend, legendBody + help);
        }

        // ------------------------------------------------------------------ status line

        /// <summary>Status line text (null hides it), with or without the playback progress bar.</summary>
        public void SetStatus(string? text, bool progressBar)
        {
            SetText(status, text ?? "", progressBar ? 12 : 0);
            if (!status.Root.activeSelf) return;
            if (progressTrack.gameObject.activeSelf != progressBar) progressTrack.gameObject.SetActive(progressBar);
        }

        public void SetProgress(float progress)
        {
            Vector2 max = new(Mathf.Clamp01(progress), 1);
            if (progressFill.anchorMax != max) progressFill.anchorMax = max;
        }

        // ------------------------------------------------------------------ back button

        public bool ButtonContains(Vector2 screenPoint)
        {
            if (!button.Root.activeInHierarchy) return false;
            Camera? eventCamera = canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera;
            return RectTransformUtility.RectangleContainsScreenPoint(button.Rect, screenPoint, eventCamera);
        }

        public void SetButtonHot(bool hot)
        {
            if (hot == buttonHot) return;
            buttonHot = hot;
            button.Background.color = hot ? ButtonHotColor : ButtonColor;
        }

        // ------------------------------------------------------------------ hover card

        /// <summary>
        /// The header of the hover card: title, artist · year, singer · text source. Only these
        /// database fields (escaped), the singer/source phrases and a colour swatch.
        /// </summary>
        public static string CardHeader(ThemeSongRecord s)
        {
            StringBuilder b = new();
            b.Append("<size=122%><b>").Append(GraphHud.Esc(s.Title)).Append("</b></size>\n");
            b.Append(GraphHud.Esc(s.Artist)).Append(" · ").Append(s.Year).Append('\n');
            b.Append($"<color={ThemesPalette.ToHex(ThemesPalette.SingerColor(s.Gender))}>●</color> ");
            b.Append($"<color={ThemesPalette.Muted}>").Append(ThemesPalette.SingerPhrase(s.Gender)).Append(" · ")
                .Append(ThemesPalette.SourcePhrase(s.FromLyrics)).Append("</color>");
            return b.ToString();
        }

        /// <summary>Shows the card for <paramref name="node"/> (null hides it); placed by <see cref="PlaceCard"/>.</summary>
        public void ShowCard(ThemeSongNode? node, ThemesGraphData? data)
        {
            cardNode = node;
            bool show = node != null && data != null;
            if (card.gameObject.activeSelf != show) card.gameObject.SetActive(show);
            if (!show) return;
            ThemeSongRecord s = node!.Song;
            cardAccent.color = ThemesPalette.SingerColor(s.Gender);
            cardHeader.text = CardHeader(s);
            float inner = CardWidth - 2 * Padding;
            float y = -Padding - 2;
            float headerHeight = Mathf.Ceil(cardHeader.GetPreferredValues(cardHeader.text, inner, 0).y);
            Place(cardHeader.rectTransform, y, headerHeight);
            y -= headerHeight + 10;

            List<(int anchorId, double score)> top = s.TopThemes(rows.Count);
            for (int i = 0; i < rows.Count; i++)
            {
                ThemeRow row = rows[i];
                bool on = i < top.Count;
                row.Label.gameObject.SetActive(on);
                row.Score.gameObject.SetActive(on);
                row.Track.gameObject.SetActive(on);
                if (!on) continue;
                (int anchorId, double score) = top[i];
                row.Label.text = GraphHud.Esc(data!.Anchor(anchorId).Label);
                row.Score.text = ThemesPalette.Score(score);
                Place(row.Label.rectTransform, y, 22, rightInset: 56);
                Place(row.Score.rectTransform, y, 22);
                Place(row.Track, y - 24, 6);
                row.Fill.anchorMax = new Vector2(Mathf.Clamp01((float)score), 1);
                y -= RowHeight;
            }
            card.sizeDelta = new Vector2(CardWidth, -y + Padding - 8);
        }

        static void Place(RectTransform rt, float top, float height, float rightInset = 0)
        {
            rt.offsetMin = new Vector2(Padding, top - height);
            rt.offsetMax = new Vector2(-Padding - rightInset, top);
        }

        /// <summary>Moves the card next to its song on screen (right of it, or left when there is no room).</summary>
        public void PlaceCard(Camera? cam)
        {
            if (cam == null || cardNode == null || !card.gameObject.activeSelf) return;
            float scale = Mathf.Max(1e-3f, canvas.scaleFactor);
            RectTransform canvasRect = (RectTransform)canvas.transform;
            Vector2 canvasSize = canvasRect.rect.size;
            Vector3 sp = cam.WorldToScreenPoint(cardNode.transform.position);
            // Screen radius of the bubble, so the card clears it.
            Vector3 edge = cam.WorldToScreenPoint(cardNode.transform.position + cam.transform.right * cardNode.Radius);
            float radius = Mathf.Max(6f, Mathf.Abs(edge.x - sp.x));
            Vector2 p = new Vector2(sp.x, sp.y) / scale;
            float r = radius / scale + 18f;
            Vector2 size = card.sizeDelta;
            float x = p.x + r;
            if (x + size.x > canvasSize.x - 12f) x = p.x - r - size.x;
            x = Mathf.Clamp(x, 12f, Mathf.Max(12f, canvasSize.x - size.x - 12f));
            // Below the song, so its label (up and to the right of the bubble) stays readable; above
            // the label when there is no room below.
            float yTop = p.y - 8f;
            if (yTop - size.y < 12f) yTop = p.y + r + 44f + size.y;
            yTop = Mathf.Clamp(yTop, size.y + 12f, Mathf.Max(size.y + 12f, canvasSize.y - 12f));
            card.anchoredPosition = new Vector2(x, yTop);
        }

        // ------------------------------------------------------------------ rendering

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
            Canvas.ForceUpdateCanvases();
            foreach (TextMeshProUGUI t in canvas.GetComponentsInChildren<TextMeshProUGUI>())
                t.ForceMeshUpdate();
            Canvas.ForceUpdateCanvases();
        }
    }
}
