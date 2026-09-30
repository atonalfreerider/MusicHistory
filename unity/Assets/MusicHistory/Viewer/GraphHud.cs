#nullable enable
using System.Collections.Generic;
using System.Linq;
using System.Text;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace MusicHistory.Viewer
{
    /// <summary>
    /// Screen overlay: legend + controls (top left), the focused song's facts (top right: title,
    /// artist, year, key, BPM, main loop, parent chain, tree-edge evidence) or a clicked edge's
    /// card, and the walkthrough panel (bottom). For identity lineages (DESIGN.md §8b) edges read
    /// "Shares: &lt;identity&gt; · family of N songs" (plus "strong match (z …)"), never bits.
    /// Nothing is clickable, so no EventSystem is needed. All strings from the database are
    /// shown inside noparse tags; the database never holds lyrics.
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
        /// <summary>The song / edge card is showing (InfoText keeps the last text while hidden).</summary>
        public bool InfoVisible => info.Root.activeSelf;
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
            canvasScaler = scaler;

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
            bool lineage = loader != null && loader.Data != null && loader.Data.IsIdentityLineage;
            string help = HelpVisible
                ? (lineage
                      ? $"\n<color={Muted}>Mouse</color>  hover shows shared identities · click selects a song or an edge · right-drag looks · wheel dollies"
                      : $"\n<color={Muted}>Mouse</color>  hover shows influences · click selects · right-drag looks · wheel dollies") +
                  $"\n<color={Muted}>Move</color>  W A S D, Q E (Shift = fast) · R reset view · L all labels · V secondary edges · F live layout (time locked) · T lyric themes" +
                  $"\n<color={Muted}>Walkthrough</color>  1 lineage · 2 subtree · 3 chronological{(lineage ? " · 4 family" : "")} · M next mode · Enter start · Space pause · N/→ next · B/← back · Esc exit · C compare in C / 120 BPM"
                : $"\n<color={Muted}>H</color> controls";
            SetText(legend, legendBody + help);
        }

        public void ShowSong(SongNode? node) => SetText(info, node == null ? "" : SongInfo(node));

        /// <summary>The edge card of a clicked edge.</summary>
        public void ShowEdge(InfluenceEdge edge) => SetText(info, EdgeInfo(edge));

        /// <summary>Colour of strong-match highlights in the HUD and legend.</summary>
        public const string StrongColor = "#fff27a";

        /// <summary>
        /// Identity lineages (DESIGN.md §8b): "Shares: &lt;identity&gt; · family of N songs", plus
        /// "strong match (z …)" when the edge is flagged. Never bits: the score is a lineage
        /// score, not borrowing evidence.
        /// </summary>
        public static string SharesText(SongGraphData data, EdgeRecord e, string identityColor) =>
            SharesText(data.Family(e.FamilyId), e.Evidence, e, identityColor);

        /// <summary>As above, for a family (with or without the edge that carries it).</summary>
        public static string SharesText(IdentityFamily? family, string? label, EdgeRecord? e, string identityColor)
        {
            StringBuilder b = new();
            b.Append($"<color={Muted}>Shares:</color> <color={identityColor}>{Esc(family?.Label ?? label ?? "")}</color>");
            if (family != null)
                b.Append($" <color={Muted}>· family of {family.Members.Count} song{(family.Members.Count == 1 ? "" : "s")}</color>");
            if (e != null && e.IsStrongMatch && e.Z > 0)
                b.Append($" · <color={StrongColor}><b>strong match</b> (z {SongPalette.Invariant(e.Z, "0.0")})</color>");
            else if ((e != null && e.IsStrongMatch) || (e == null && family != null && family.IsStrong))
                b.Append($" · <color={StrongColor}><b>strong match</b></color>");
            return b.ToString();
        }

        /// <summary>'loop', 'progression schema', 'named schema', 'exact shared passage'.</summary>
        public static string KindText(string kind) => kind switch
        {
            "loop" => "repeating chord loop",
            "schema" => "named chord schema",
            "progression" => "chord progression schema",
            "strong" => "exact shared passage",
            _ => kind
        };

        string EdgeInfo(InfluenceEdge edge)
        {
            SongGraphData data = loader.Data!;
            EdgeRecord e = edge.Record;
            SongRecord a = edge.Source.Song, c = edge.Target.Song;
            string color = SongPalette.ToHex(SongPalette.ChannelColor(edge.Channel));
            StringBuilder b = new();
            b.Append($"<size=125%><b>{Esc(a.Title)}</b> <color={color}>→</color> <b>{Esc(c.Title)}</b></size>\n");
            b.Append($"{Esc(a.Artist)} · {a.Year}  <color={Muted}>→</color>  {Esc(c.Artist)} · {c.Year}\n");
            if (data.IsIdentityLineage)
            {
                b.Append(SharesText(data, e, color)).Append('\n');
                IdentityFamily? family = data.Family(e.FamilyId);
                if (family != null)
                {
                    b.Append($"<color={Muted}>{KindText(family.Kind)}");
                    if (!string.IsNullOrEmpty(family.Roman) && !family.Label.Contains(family.Roman!)) b.Append($" · {Esc(family.Roman)}");
                    SongRecord first = data.Song(family.Members[0].NodeId);
                    if (family.Members.Count > 2) b.Append($" · earliest song {Esc(first.Title)} ({first.Year})");
                    b.Append("</color>\n");
                }
                b.Append($"<color={Muted}>{(e.IsTree ? "Tree edge: the earlier song is the later one's lineage parent" : "Secondary edge: another strong sharer")}" +
                         " · a shared musical identity, not proven copying</color>");
                if (family != null)
                    b.Append($"\n<color={Muted}>Walkthrough 4 (family) + Enter plays " +
                             (family.Members.Count == 2 ? "both songs, earlier first" : $"all {family.Members.Count} songs in time order") + "</color>");
            }
            else
            {
                b.Append($"<color={Muted}>Via</color> <color={color}>{SongPalette.ChannelLabel(edge.Channel)}</color>" +
                         $" · {SongPalette.Invariant(e.ScoreBits, "0")} bits · z {SongPalette.Invariant(e.Z, "0.0")} · {(e.IsTree ? "tree" : "secondary")} edge");
                if (!string.IsNullOrEmpty(e.Evidence)) b.Append($"\n<size=90%><color={Muted}>{Esc(e.Evidence)}</color></size>");
            }
            return b.ToString();
        }

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
            SongGraphData? data = loader != null ? loader.Data : null;
            bool lineage = data != null && data.IsIdentityLineage;
            b.Append(lineage
                ? $"<color={Muted}>Shares identities with</color> {node.Incoming.Count} earlier · {node.Outgoing.Count} later" +
                  $" · <color={Muted}>descendants</color> {s.Descendants}"
                : $"<color={Muted}>Influenced by</color> {node.Incoming.Count} · <color={Muted}>influences</color> {node.Outgoing.Count}" +
                  $" · <color={Muted}>descendants</color> {s.Descendants}");
            if (s.CanonRank.HasValue) b.Append($" · <color={Muted}>canon rank</color> {s.CanonRank.Value}");
            if (node.TreeEdge != null)
            {
                EdgeRecord e = node.TreeEdge.Record;
                string color = SongPalette.ToHex(SongPalette.ChannelColor(node.TreeEdge.Channel));
                if (lineage)
                {
                    b.Append('\n').Append(SharesText(data!, e, color));
                    b.Append($" <color={Muted}>with {Esc(node.TreeEdge.Source.Song.Title)} ({node.TreeEdge.Source.Song.Year})</color>");
                }
                else
                {
                    b.Append($"\n<color={Muted}>Via</color> <color={color}>{SongPalette.ChannelLabel(node.TreeEdge.Channel)}</color>" +
                             $" · {SongPalette.Invariant(e.ScoreBits, "0")} bits · z {SongPalette.Invariant(e.Z, "0.0")}");
                    if (!string.IsNullOrEmpty(e.Evidence)) b.Append($"\n<size=90%><color={Muted}>{Esc(e.Evidence)}</color></size>");
                }
            }
            else if (lineage && data!.HasFamilies)
            {
                int families = data.FamiliesOf(s.NodeId).Count(m => data.Families[m.FamilyId].Size >= 2);
                b.Append($"\n<color={Muted}>Root of its lineage tree · in {families} famil{(families == 1 ? "y" : "ies")} shared with other songs</color>");
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

        readonly List<Rect> panelRects = new();
        CanvasScaler? canvasScaler;

        /// <summary>
        /// Screen rects (pixels, origin bottom-left) the visible panels cover on a screen the size
        /// of <paramref name="cam"/>'s target, from the panels' anchors and the CanvasScaler's
        /// scale-with-screen-size factor, so world labels can keep clear of them (LabelLayer.KeepClear).
        /// </summary>
        public IReadOnlyList<Rect> PanelScreenRects(Camera cam) => PanelScreenRects(cam.pixelWidth, cam.pixelHeight);

        /// <summary>As above, for a <paramref name="width"/> x <paramref name="height"/> pixel screen.</summary>
        public IReadOnlyList<Rect> PanelScreenRects(float width, float height)
        {
            panelRects.Clear();
            if (canvas == null || canvasScaler == null) return panelRects;
            float w = Mathf.Max(1, width), h = Mathf.Max(1, height);
            Vector2 reference = canvasScaler.referenceResolution;
            float scale = Mathf.Pow(2f, Mathf.Lerp(Mathf.Log(w / reference.x, 2f), Mathf.Log(h / reference.y, 2f), canvasScaler.matchWidthOrHeight));
            AddPanelRect(legend, w, h, scale);
            AddPanelRect(info, w, h, scale);
            AddPanelRect(tour, w, h, scale);
            return panelRects;
        }

        void AddPanelRect(Panel p, float w, float h, float scale)
        {
            if (!p.Root.activeSelf) return;
            RectTransform r = p.Rect;
            Vector2 size = r.sizeDelta * scale;
            Vector2 anchor = new(r.anchorMin.x * w, r.anchorMin.y * h);
            Vector2 min = anchor + r.anchoredPosition * scale - Vector2.Scale(r.pivot, size);
            panelRects.Add(new Rect(min, size));
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
