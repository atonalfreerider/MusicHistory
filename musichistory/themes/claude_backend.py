"""Optional ``claude`` backend: Claude reads the lyrics (or title) and returns ten scores.

Only used with ``--backend claude`` and when ``ANTHROPIC_API_KEY`` is set. Each song is one
Messages API request with a structured-output JSON schema, so the response is exactly ten
numbers (one relatability score in [0, 1] per theme) and nothing else; the scores are
clipped and normalized to sum to 1 like every other backend.

Privacy: the prompt (which contains the lyric text) is built in memory and sent only to the
API. It is never printed or logged, and API errors are re-raised as ``ClaudeError`` carrying
only the exception class name and status, never a message body that might echo the input.
Refusals (``stop_reason == "refusal"``) and failures return None and are counted; the stage
then falls back to the NLI backend for that song.

Model: ``claude-opus-5-5`` at ``effort: low`` (a short classification), with server-side
refusal fallbacks (``fallbacks: "default"``, beta ``server-side-fallback-2026-07-01``).
"""

from __future__ import annotations

import json
import os

import numpy as np

from .classify import THEMES, Item

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
PROMPT_VERSION = "1"

SYSTEM = (
    "You rate how well a song's lyrics fit each of ten lyrical themes. For every theme give a "
    "relatability score between 0 and 1: 1 means the song is clearly and mostly about that theme, "
    "0 means it does not express it at all. Scores are independent (a song may fit several themes). "
    "Theme 10 'Other' should be high when the song is mainly about something none of themes 1-9 "
    "describe (dancing, partying, a story, a place, bragging, an instrumental title, ...). When only a "
    "title is given, judge from the title alone and prefer 'Other' unless the title itself clearly "
    "states a theme.\n\nThemes:\n"
    + "\n".join(f"{t.id}. {t.label}" for t in THEMES)
)

SCHEMA = {
    "type": "object",
    "properties": {f"theme_{t.id}": {"type": "number"} for t in THEMES},
    "required": [f"theme_{t.id}" for t in THEMES],
    "additionalProperties": False,
}


class ClaudeError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


class ClaudeClassifier:
    name = "claude"

    def __init__(self, model: str = MODEL, **_ignored) -> None:
        if not available():
            raise ClaudeError("ANTHROPIC_API_KEY is not set")
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self.model_id = model
        self.refusals = 0
        self.failures = 0

    @property
    def model(self) -> str:
        return f"{self.model_id};prompt-v{PROMPT_VERSION}"

    @property
    def device(self) -> str:
        return "api"

    def device_name(self) -> str:
        return "anthropic-api"

    def load(self) -> None:
        pass

    def _content(self, item: Item) -> str:
        if item.lines is None:
            return f"Title only (no lyrics available): {item.title}"
        return f"Title: {item.title}\n\nLyrics:\n" + "\n".join(item.lines)

    def classify_one(self, item: Item) -> np.ndarray | None:
        a = self._anthropic
        try:
            response = self._client.beta.messages.create(
                model=self.model_id,
                max_tokens=2048,
                system=SYSTEM,
                messages=[{"role": "user", "content": self._content(item)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except a.RateLimitError as exc:
            raise ClaudeError(f"rate limited ({type(exc).__name__})") from None
        except a.APIStatusError as exc:
            raise ClaudeError(f"API error {exc.status_code} ({type(exc).__name__})") from None
        except a.APIConnectionError as exc:
            raise ClaudeError(f"connection error ({type(exc).__name__})") from None
        if response.stop_reason == "refusal":
            self.refusals += 1
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        try:
            data = json.loads(text or "")
            v = np.array([float(data[f"theme_{t.id}"]) for t in THEMES], dtype=np.float64)
        except (ValueError, KeyError, TypeError):
            self.failures += 1
            return None
        v = np.clip(v, 0.0, 1.0) + 1e-6
        return v / v.sum()

    def classify(self, items: list[Item]) -> list[np.ndarray | None]:
        out: list[np.ndarray | None] = []
        for it in items:
            try:
                out.append(self.classify_one(it))
            except ClaudeError:
                self.failures += 1
                out.append(None)
        return out
