"""Themes, chunking, aggregation and the NLI classifier (fake entailment + optional GPU smoke test).

Premise sentences are invented placeholders written for these tests (never real lyrics).
"""

from __future__ import annotations

import re
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory.themes import classify as C  # noqa: E402


def test_themes_match_design_verbatim():
    text = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    sec = text[text.index("## 12. Lyric themes"):]
    labels = dict((int(n), s) for n, s in re.findall(r'(\d+) "([^"]+)"', sec[:sec.index("**Text.**")]))
    assert [t.id for t in C.THEMES] == list(range(1, 11))
    assert {t.id: t.label for t in C.THEMES} == labels
    assert C.THEMES[-1].label == "Other" and C.OTHER == 10
    assert all(t.short and len(t.short) <= 24 for t in C.THEMES)


def test_every_hypothesis_set_covers_themes_1_to_9():
    for name, hs in C.HYPOTHESES.items():
        assert sorted(hs) == list(range(1, 10)), name
        assert all(h and all(s.endswith(".") for s in h) for h in hs.values())
    assert len({len(v) for v in C.HYPOTHESES["v3"].values()}) == 1  # balanced set


def test_chunks_cover_all_lines():
    lines = [f"l{i}" for i in range(10)]
    ch = C.chunks(lines, 4, 2)
    assert ch[0] == "l0 / l1 / l2 / l3" and ch[-1] == "l6 / l7 / l8 / l9"
    assert C.chunks(lines[:3], 4, 2) == ["l0 / l1 / l2"]
    assert C.chunks(lines, 6, 3)[-1].endswith("l9") and C.chunks([], 4, 2) == []


def test_premises_add_title_last():
    it = C.Item("W", "A Title", ["x1", "x2", "x3"])
    prem, ti = C.premises(it, C.NliConfig(chunk_lines=2, chunk_stride=1))
    assert prem == ["x1 / x2", "x2 / x3", "A Title"] and ti == 2
    prem, _ = C.premises(C.Item("W", "Only", None), C.DEFAULT_CONFIG)
    assert prem == ["Only"]


def test_distribution_sums_to_one_and_other_behaviour():
    strong = C.distribution(np.array([0, 0, 0.95, 0, 0, 0, 0, 0, 0.0]), other_tau=0.8)
    assert abs(strong.sum() - 1) < 1e-9 and C.top_themes(strong, 1) == [3]
    assert strong[9] < 1e-4                                    # clear theme -> no 'Other'
    weak = C.distribution(np.full(9, 0.02), other_tau=0.8)
    assert C.top_themes(weak, 1) == [10] and abs(weak.sum() - 1) < 1e-9
    sharp = C.distribution(np.array([0.2, 0, 0.8, 0, 0, 0, 0, 0, 0.0]), other_tau=0.5, alpha=2)
    flat = C.distribution(np.array([0.2, 0, 0.8, 0, 0, 0, 0, 0, 0.0]), other_tau=0.5, alpha=1)
    assert sharp[2] > flat[2]


def test_aggregate_top_k_mean_and_title_threshold():
    T = np.zeros((5, 9))
    T[:, 6] = [0.9, 0.8, 0.1, 0.0, 0.0]                        # two chunks say "miss you"
    cfg = C.NliConfig(top_k=2, other_tau=0.8)
    d = C.aggregate(T, cfg)
    assert C.top_themes(d, 1) == [7]
    one = np.zeros((1, 9))
    one[0, 2] = 0.1
    t_cfg = replace(cfg, title_other_tau=0.05)
    assert C.top_themes(C.aggregate(one, t_cfg, title_only=True), 1) == [3]
    assert C.top_themes(C.aggregate(one, t_cfg, title_only=False), 1) == [10]


def test_item_repr_hides_text_and_sha_covers_title():
    a = C.Item("W", "T", ["some placeholder words"])
    assert "placeholder" not in repr(a) and a.text_source == "lyrics"
    assert a.sha256() != C.Item("W", "T2", ["some placeholder words"]).sha256()
    assert C.Item("W", "T", None).text_source == "title"


def test_config_tag_changes_with_settings():
    assert C.DEFAULT_CONFIG.tag() != replace(C.DEFAULT_CONFIG, top_k=1).tag()
    clf = C.NliClassifier()
    assert clf.model.startswith(C.MODEL_ID + "@" + C.MODEL_REVISION[:7] + ";")


class _FakeNli(C.NliClassifier):
    """Entailment from keywords, so the pipeline runs without a model."""

    KEYS = {7: "miss", 8: "together", 3: "adore"}

    def load(self) -> None:
        self._device = "cpu"

    def entail(self, prem, hyps):
        _, owner = self.hypotheses()
        E = np.zeros((len(prem), len(hyps)), dtype=np.float32)
        for i, p in enumerate(prem):
            for j, k in enumerate(owner):
                E[i, j] = 0.95 if self.KEYS.get(k, "\0") in p.lower() else 0.01
        return E


def test_classify_pipeline_with_fake_entailment():
    clf = _FakeNli()
    items = [
        C.Item("A", "Untitled", ["I miss the porch light", "I miss the gravel road", "the gate is green",
                                 "clouds are slow", "I miss the porch light", "I miss the gravel road"]),
        C.Item("B", "Hands Together", None),
        C.Item("C", "Blue Tango", None),
    ]
    d = dict(zip("ABC", clf.classify(items)))
    assert C.top_themes(d["A"], 1) == [7]
    assert C.top_themes(d["B"], 1) == [8]
    assert C.top_themes(d["C"], 1) == [10]
    assert all(abs(v.sum() - 1) < 1e-9 and v.shape == (10,) for v in d.values())


needs_model = pytest.mark.skipif(not C._have_snapshot(C.MODEL_ID, C.MODEL_REVISION),
                                 reason="NLI model not cached under data/cache/models")


@needs_model
def test_nli_model_smoke():
    pytest.importorskip("torch")
    clf = C.NliClassifier()
    items = [
        C.Item("A", "Porch Light", [
            "since you went away the porch light burns all night",
            "your coat still hangs beside the door",
            "I miss you on every cold morning at the station",
            "every empty chair reminds me you are gone",
            "I miss you, I keep waiting by the phone",
            "come back, the house is cold without you",
        ]),
        C.Item("B", "Blue Tango", None),
    ]
    a, b = clf.classify(items)
    assert C.top_themes(a, 1) == [7]
    assert C.top_themes(b, 1) == [10]
