"""The ten lyric themes and the classifiers (DESIGN.md §12).

Every backend maps a song (its lyric lines, or only its title) to a **relatability
distribution** over the ten themes: ten scores in [0, 1] that sum to 1. Theme 10 "Other" is
high when none of themes 1-9 is clearly expressed.

Default backend ``nli``: a local zero-shot NLI model (``MODEL_ID`` at the pinned
``MODEL_REVISION``) on the GPU (CPU fallback), cached under ``data/cache/models`` (HF_HOME).

1. The lyrics are split into chunks of ``chunk_lines`` lines (overlapping by
   ``chunk_lines - chunk_stride``); the title is one extra chunk. Title-only songs have just
   the title chunk.
2. Each theme 1-9 has one or more hypotheses phrased the way a listener would summarize a
   song ("The singer tells someone they miss them."). Every chunk x hypothesis pair gets an
   entailment probability; a theme's chunk score combines its hypotheses (max or mean).
3. A theme's song score is the mean of its ``top_k`` best chunk scores (a chorus that says
   it clearly counts, one stray line does not dominate when k > 1).
4. "Other" = ``max(0, tau - max_k s_k) / tau``: rises as the strongest theme weakens
   (``tau = other_tau`` for lyrics, ``title_other_tau`` for title-only songs, whose single
   short premise entails far less). The ten values (raised to ``alpha``) are normalized to
   sum to 1.

Unique chunks are scored once (choruses repeat) and chunk texts are held in memory only
for the duration of one ``classify`` call.

Backend ``claude`` (``claude_backend.py``, only with ``ANTHROPIC_API_KEY``): the model reads
the lyrics (or title) and returns only the ten scores.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace

import numpy as np


# ------------------------------------------------------------------ themes
@dataclass(frozen=True)
class Theme:
    id: int
    label: str      # verbatim from DESIGN.md §12
    short: str      # for the viewer's anchor discs


THEMES: tuple[Theme, ...] = (
    Theme(1, "I would be so good to you/him/her", "So good to you"),
    Theme(2, "I'm sad you/she/he don't/doesn't love me", "Sad you don't love me"),
    Theme(3, "I love you/him/her", "I love you"),
    Theme(4, "I wish you/he/she loved me", "Wish you loved me"),
    Theme(5, "I don't need/love you/him/her", "Don't need you"),
    Theme(6, "I hate that I love you/him/her", "Hate that I love you"),
    Theme(7, "I miss you/him/her", "I miss you"),
    Theme(8, "Let's all love each other", "Love each other"),
    Theme(9, "What is going on in the world?", "What's going on"),
    Theme(10, "Other", "Other"),
)
N_THEMES = len(THEMES)
OTHER = 10
THEME_BY_ID = {t.id: t for t in THEMES}


# Hypotheses for themes 1-9 (theme 10 has none: it is what is left). Phrased as a listener's
# one-line summary of the song, in the third person about "the singer".
HYPOTHESES: dict[str, dict[int, tuple[str, ...]]] = {
    "v1": {
        1: ("The singer promises to treat someone well.",
            "The singer says they would do anything for the person they love."),
        2: ("The singer is sad because the person they love does not love them.",
            "The singer is heartbroken."),
        3: ("The singer tells someone that they love them.",
            "The singer is happily in love."),
        4: ("The singer wishes someone would love them.",
            "The singer longs for someone who is not theirs."),
        5: ("The singer tells someone they do not need them anymore.",
            "The singer is glad to be rid of a former lover."),
        6: ("The singer loves someone who hurts them.",
            "The singer cannot stop loving someone who is bad for them."),
        7: ("The singer tells someone they miss them.",
            "The singer misses someone who is gone."),
        8: ("The singer calls on everyone to love each other.",
            "The song is about peace and people coming together."),
        9: ("The singer is troubled by what is going on in the world.",
            "The song is about war, poverty or injustice."),
    },
    # Shorter, more literal statements that a line of lyrics can entail directly.
    "v2": {
        1: ("The singer promises to take care of someone.",
            "The singer promises to always be there for someone.",
            "The singer would do anything for someone."),
        2: ("The singer is sad that someone does not love them.",
            "The singer's heart is broken."),
        3: ("The singer loves someone.",
            "The singer is in love."),
        4: ("The singer wants someone to love them.",
            "The singer hopes someone will be theirs."),
        5: ("The singer does not need someone.",
            "The singer tells someone to go away."),
        6: ("The singer loves someone who hurts them.",
            "The singer cannot stop loving someone even though it is bad for them."),
        7: ("The singer misses someone.",
            "The singer is lonely without someone."),
        8: ("The singer wants everyone to love each other.",
            "The singer wants people to come together."),
        9: ("The singer is worried about the state of the world.",
            "The song is about war, poverty or injustice."),
    },
    # v2 balanced: exactly three statements per theme (with 'max', a theme with more
    # hypotheses would get a higher score by chance alone).
    "v3": {
        1: ("The singer promises to take care of someone.",
            "The singer promises to always be there for someone.",
            "The singer would do anything for someone."),
        2: ("The singer is sad that someone does not love them.",
            "The singer's heart is broken.",
            "The singer was left by someone they love."),
        3: ("The singer loves someone.",
            "The singer is in love.",
            "The singer is happy with the person they love."),
        4: ("The singer wants someone to love them.",
            "The singer hopes someone will be theirs.",
            "The singer has a crush on someone."),
        5: ("The singer does not need someone.",
            "The singer tells someone to go away.",
            "The singer is better off without their former lover."),
        6: ("The singer loves someone who hurts them.",
            "The singer cannot stop loving someone even though it is bad for them.",
            "The singer is addicted to someone."),
        7: ("The singer misses someone.",
            "The singer is lonely without someone.",
            "The singer longs for someone who is gone."),
        8: ("The singer wants everyone to love each other.",
            "The singer wants people to come together.",
            "The singer sings about peace and unity."),
        9: ("The singer is worried about the state of the world.",
            "The song is about war, poverty or injustice.",
            "The singer protests against society."),
    },
}


@dataclass(frozen=True)
class NliConfig:
    """Classifier settings. Defaults were tuned on the validation set's tune split
    (``validation.tune``; the pre-tuning settings are ``validation.BASELINE_CONFIG``)."""

    hypotheses: str = "v2"
    chunk_lines: int = 6
    chunk_stride: int = 3
    hyp_combine: str = "max"        # 'max' | 'mean' over a theme's hypotheses
    top_k: int = 5
    title_template: str = "{title}"
    other_tau: float = 0.8
    title_other_tau: float | None = 0.05   # 'Other' threshold for title-only songs (None = other_tau)
    alpha: float = 1.0              # 1 = unsharpened relatability (the layout sharpens with its gamma)
    max_length: int = 256           # tokens per premise+hypothesis pair

    def tag(self) -> str:
        """Short stable id of the settings (part of the cache key in ``song_text.model``)."""
        h = hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:8]
        return f"{self.hypotheses}-c{self.chunk_lines}s{self.chunk_stride}-{self.hyp_combine}-k{self.top_k}-{h}"


DEFAULT_CONFIG = NliConfig()


# ------------------------------------------------------------------ inputs
@dataclass
class Item:
    """One song to classify. ``lines`` is None for title-only songs. Memory only."""

    work_id: str
    title: str
    lines: list[str] | None = field(default=None, repr=False)

    def __repr__(self) -> str:  # never the text
        n = "title only" if self.lines is None else f"{len(self.lines)} lines"
        return f"Item({self.work_id}, <{n}>)"

    @property
    def text_source(self) -> str:
        return "title" if self.lines is None else "lyrics"

    def sha256(self) -> str:
        """SHA-256 of the classifier input: the lyric lines and the title (or the title alone)."""
        body = self.title if self.lines is None else "\n".join(self.lines) + "\n\x00" + self.title
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def chunks(lines: list[str], size: int, stride: int) -> list[str]:
    """Windows of ``size`` lines every ``stride`` lines (the last window reaches the end)."""
    if not lines:
        return []
    size, stride = max(1, size), max(1, stride)
    if len(lines) <= size:
        return [" / ".join(lines)]
    starts = list(range(0, len(lines) - size + 1, stride))
    if starts[-1] != len(lines) - size:
        starts.append(len(lines) - size)
    return [" / ".join(lines[s:s + size]) for s in starts]


def premises(item: Item, cfg: NliConfig) -> tuple[list[str], int]:
    """(chunk texts with repeats, index of the title chunk)."""
    out = chunks(item.lines, cfg.chunk_lines, cfg.chunk_stride) if item.lines else []
    out.append(cfg.title_template.format(title=item.title))
    return out, len(out) - 1


# ------------------------------------------------------------------ aggregation
def theme_chunk_scores(E: np.ndarray, hyp_theme: list[int], combine: str) -> np.ndarray:
    """[chunks, hypotheses] entailment -> [chunks, 9] per-theme chunk scores."""
    out = np.zeros((E.shape[0], 9), dtype=np.float64)
    ht = np.asarray(hyp_theme)
    for k in range(1, 10):
        cols = E[:, ht == k]
        out[:, k - 1] = cols.max(axis=1) if combine == "max" else cols.mean(axis=1)
    return out


def distribution(s9: np.ndarray, other_tau: float, alpha: float = 1.0) -> np.ndarray:
    """Nine theme scores in [0, 1] -> ten-way relatability distribution (sums to 1)."""
    s9 = np.clip(np.asarray(s9, dtype=np.float64), 0.0, 1.0)
    m = float(s9.max()) if s9.size else 0.0
    other = max(0.0, other_tau - m) / other_tau if other_tau > 0 else 0.0
    v = np.concatenate([s9, [other]])
    v = np.clip(v, 1e-6, None) ** alpha
    return v / v.sum()


def aggregate(T: np.ndarray, cfg: NliConfig, title_only: bool = False) -> np.ndarray:
    """[chunks, 9] chunk scores -> ten-way distribution."""
    k = max(1, min(cfg.top_k, T.shape[0]))
    top = -np.sort(-T, axis=0)[:k]
    tau = cfg.title_other_tau if title_only and cfg.title_other_tau is not None else cfg.other_tau
    return distribution(top.mean(axis=0), tau, cfg.alpha)


def top_themes(dist: np.ndarray, n: int = 2) -> list[int]:
    """Theme ids of the ``n`` largest scores (ties: lower id first)."""
    order = sorted(range(len(dist)), key=lambda i: (-float(dist[i]), i))
    return [i + 1 for i in order[:n]]


# ------------------------------------------------------------------ NLI backend
MODEL_ID = "MoritzLaurer/deberta-v3-large-zeroshot-v2.0"
MODEL_REVISION = "cf44676c28ba7312e5c5f8f8d2c22b3e0c9cdae2"


def models_dir():
    from .. import config

    return config.CACHE / "models"


def _hf_env() -> None:
    os.environ.setdefault("HF_HOME", str(models_dir()))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


class NliClassifier:
    name = "nli"

    def __init__(self, cfg: NliConfig = DEFAULT_CONFIG, *, model_id: str = MODEL_ID,
                 revision: str = MODEL_REVISION, device: str | None = None, batch_size: int = 64) -> None:
        self.cfg = cfg
        self.model_id = model_id
        self.revision = revision
        self.batch_size = batch_size
        self._device = device
        self._model = None
        self._tok = None

    # -- identity ---------------------------------------------------------------------
    @property
    def model(self) -> str:
        """Stored in song_text.model: checkpoint@revision;settings (the cache key)."""
        return f"{self.model_id}@{self.revision[:7]};{self.cfg.tag()}"

    def with_config(self, cfg: NliConfig) -> "NliClassifier":
        other = NliClassifier(cfg, model_id=self.model_id, revision=self.revision, device=self._device,
                              batch_size=self.batch_size)
        if self._model is not None:
            other._model, other._tok, other._device = self._model, self._tok, self._device
            other._entail_idx = self._entail_idx
        return other

    def hypotheses(self) -> tuple[list[str], list[int]]:
        hyps, owner = [], []
        for k, hs in sorted(HYPOTHESES[self.cfg.hypotheses].items()):
            for h in hs:
                hyps.append(h)
                owner.append(k)
        return hyps, owner

    # -- model ------------------------------------------------------------------------
    def load(self) -> None:
        if self._model is not None:
            return
        _hf_env()
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        dev = self._device or ("cuda" if torch.cuda.is_available() else "cpu")
        local = _have_snapshot(self.model_id, self.revision)
        kw = {"revision": self.revision, "local_files_only": local}
        self._tok = AutoTokenizer.from_pretrained(self.model_id, **kw)
        model = AutoModelForSequenceClassification.from_pretrained(
            self.model_id, dtype=torch.float16 if dev.startswith("cuda") else torch.float32, **kw)
        self._model = model.to(dev).eval()
        self._device = dev
        labels = {v.lower(): int(k) for k, v in model.config.id2label.items()}
        self._entail_idx = labels.get("entailment", 0)

    @property
    def device(self) -> str:
        return self._device or "unloaded"

    def device_name(self) -> str:
        import torch

        if self.device.startswith("cuda"):
            return f"cuda:{torch.cuda.get_device_name(0)}"
        return "cpu"

    def entail(self, prem: list[str], hyps: list[str]) -> np.ndarray:
        """[len(prem), len(hyps)] entailment probabilities."""
        import torch

        self.load()
        out = np.zeros((len(prem), len(hyps)), dtype=np.float32)
        if not prem:
            return out
        pairs = [(i, j) for i in range(len(prem)) for j in range(len(hyps))]
        pairs.sort(key=lambda p: (len(prem[p[0]]) + len(hyps[p[1]]), p))
        bs = self.batch_size
        with torch.inference_mode():
            for b in range(0, len(pairs), bs):
                batch = pairs[b:b + bs]
                enc = self._tok([prem[i] for i, _ in batch], [hyps[j] for _, j in batch], return_tensors="pt",
                                padding=True, truncation="only_first", max_length=self.cfg.max_length)
                enc = {k: v.to(self._device) for k, v in enc.items()}
                probs = self._model(**enc).logits.float().softmax(-1)[:, self._entail_idx].cpu().numpy()
                for (i, j), p in zip(batch, probs):
                    out[i, j] = p
                del enc
        return out

    # -- songs ------------------------------------------------------------------------
    def entailments(self, items: list[Item]) -> list[np.ndarray]:
        """Per item: [chunks (with repeats, title last), hypotheses] entailment probabilities.

        Unique chunk texts are scored once; the texts live only inside this call.
        """
        hyps, _ = self.hypotheses()
        uniq: dict[str, int] = {}
        per_item: list[list[int]] = []
        for it in items:
            prem, _ = premises(it, self.cfg)
            per_item.append([uniq.setdefault(p, len(uniq)) for p in prem])
        texts = list(uniq)
        uniq.clear()
        E = self.entail(texts, hyps)
        texts.clear()
        return [E[idx] for idx in per_item]

    def aggregate(self, E: np.ndarray, title_only: bool) -> np.ndarray:
        _, owner = self.hypotheses()
        return aggregate(theme_chunk_scores(E, owner, self.cfg.hyp_combine), self.cfg, title_only)

    def classify(self, items: list[Item]) -> list[np.ndarray]:
        return [self.aggregate(E, it.lines is None) for it, E in zip(items, self.entailments(items))]


def _have_snapshot(model_id: str, revision: str) -> bool:
    _hf_env()
    snap = models_dir() / "hub" / ("models--" + model_id.replace("/", "--")) / "snapshots" / revision
    return (snap / "config.json").exists() and any(snap.glob("*.safetensors"))


def make_backend(name: str, cfg: NliConfig = DEFAULT_CONFIG, **kw):
    if name == "nli":
        return NliClassifier(cfg, **kw)
    if name == "claude":
        from .claude_backend import ClaudeClassifier

        return ClaudeClassifier(**kw)
    raise ValueError(f"unknown backend {name!r}")


__all__ = ["THEMES", "Theme", "N_THEMES", "OTHER", "THEME_BY_ID", "HYPOTHESES", "NliConfig", "DEFAULT_CONFIG",
           "Item", "chunks", "premises", "aggregate", "distribution", "top_themes", "NliClassifier",
           "MODEL_ID", "MODEL_REVISION", "make_backend", "replace"]
