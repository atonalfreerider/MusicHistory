"""The optional Claude backend with a fake Anthropic client (no network, no key needed).

The lyric line is an invented placeholder.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

anthropic = pytest.importorskip("anthropic")
from musichistory.themes import claude_backend as CB  # noqa: E402
from musichistory.themes.classify import Item, top_themes  # noqa: E402

SECRET = "velvet hedgehogs whistle in the attic"


class _Msgs:
    def __init__(self, reply):
        self.reply = reply
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.reply(kwargs)


def _client(reply):
    msgs = _Msgs(reply)
    return types.SimpleNamespace(beta=types.SimpleNamespace(messages=msgs)), msgs


def _text_reply(scores: dict):
    return lambda kw: types.SimpleNamespace(stop_reason="end_turn",
                                            content=[types.SimpleNamespace(type="text", text=json.dumps(scores))])


@pytest.fixture()
def clf(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: object())
    return CB.ClaudeClassifier()


def test_request_shape_and_scores(clf, capsys):
    scores = {f"theme_{i}": 0.05 for i in range(1, 11)}
    scores["theme_7"] = 0.9
    client, msgs = _client(_text_reply(scores))
    clf._client = client
    (d,) = clf.classify([Item("W", "Porch Light", [SECRET] * 4)])
    assert top_themes(d, 1) == [7] and abs(d.sum() - 1) < 1e-9
    kw = msgs.kwargs
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema" and len(fmt["schema"]["required"]) == 10
    assert SECRET in kw["messages"][0]["content"]          # sent to the API ...
    out = capsys.readouterr()
    assert SECRET not in out.out and SECRET not in out.err  # ... but never printed


def test_refusal_and_bad_json_return_none(clf):
    clf._client, _ = _client(lambda kw: types.SimpleNamespace(stop_reason="refusal", content=[]))
    assert clf.classify([Item("W", "T", None)]) == [None] and clf.refusals == 1
    clf._client, _ = _client(lambda kw: types.SimpleNamespace(
        stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text="{}")]))
    assert clf.classify([Item("W", "T", None)]) == [None] and clf.failures == 1


def test_api_errors_do_not_echo_input(clf):
    def raise_conn(kw):
        import httpx2
        raise anthropic.APIConnectionError(message=f"failed while sending {SECRET}",
                                           request=httpx2.Request("POST", "https://example.invalid"))

    clf._client, _ = _client(raise_conn)
    with pytest.raises(CB.ClaudeError) as ei:
        clf.classify_one(Item("W", "T", [SECRET]))
    assert SECRET not in str(ei.value)


def test_requires_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(CB.ClaudeError):
        CB.ClaudeClassifier()
