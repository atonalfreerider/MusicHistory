"""ElevenLabs request format, cache, retries and key hygiene — with a fake HTTP layer."""

import json

import numpy as np
import pytest

from musichistory.narration import tts

FAKE_KEY = "sk_fake_SECRET_0123456789"


class FakePost:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": json.loads(body), "headers": dict(headers)})
        if self.responses:
            return self.responses.pop(0)
        pcm = (np.sin(np.arange(2400) / 10) * 8000).astype("<i2").tobytes()
        return 200, pcm


@pytest.fixture
def key_file(tmp_path):
    f = tmp_path / "key.txt"
    f.write_text(FAKE_KEY + "\n", encoding="utf-8")
    return f


def _all_text(folder):
    return "".join(p.read_bytes().decode("latin-1") for p in folder.rglob("*") if p.is_file())


def test_request_format_and_cache(tmp_path, key_file):
    post = FakePost()
    usage = tts.Usage()
    settings = {"stability": 0.6, "similarity_boost": 0.75}
    y, sr, ident, cached = tts.synthesize("The loop repeats.", settings, cache=tmp_path / "c", post=post,
                                          key_file=key_file, usage=usage)
    assert sr == 24000 and not cached and len(y) == 2400 and y.dtype == np.float32
    call = post.calls[0]
    assert call["url"] == "https://api.elevenlabs.io/v1/text-to-speech/7NoxJCAEPTXbnfIvyaF6?output_format=pcm_24000"
    assert call["headers"]["xi-api-key"] == FAKE_KEY
    assert call["body"] == {"text": "The loop repeats.", "model_id": "eleven_v4", "language_code": "en",
                            "voice_settings": settings}
    assert FAKE_KEY not in call["url"]
    # second time: from the cache, no request, key file not even needed
    key_file.unlink()
    y2, _, ident2, cached2 = tts.synthesize("The loop repeats.", settings, cache=tmp_path / "c", post=post,
                                            key_file=key_file, usage=usage)
    assert cached2 and ident2 == ident and len(post.calls) == 1 and np.array_equal(y, y2)
    assert usage.requests == 1 and usage.characters == len("The loop repeats.") and usage.cache_hits == 1
    assert FAKE_KEY not in _all_text(tmp_path / "c")


def test_identity_depends_on_voice_model_settings_text_only():
    s = {"stability": 0.6, "similarity_boost": 0.75}
    a = tts.request_identity("A.", s)
    assert a == tts.request_identity("A.", dict(reversed(list(s.items()))))
    assert a != tts.request_identity("A. ", s)
    assert a != tts.request_identity("A.", {**s, "stability": 0.65})
    assert a != tts.request_identity("A.", s, model="other")
    assert a != tts.request_identity("A.", s, voice="other")
    assert len(a) == 64


def test_errors_never_carry_the_key(tmp_path, key_file):
    body = json.dumps({"detail": {"status": "invalid_api_key", "message": f"bad key {FAKE_KEY}"}}).encode()
    post = FakePost([(401, body)])
    with pytest.raises(tts.TTSError) as e:
        tts.synthesize("X.", {"stability": 0.6}, cache=tmp_path, post=post, key_file=key_file)
    assert e.value.status == 401
    assert FAKE_KEY not in str(e.value) and "check the key file" in str(e.value)
    assert e.value.__cause__ is None and e.value.__context__ is None or FAKE_KEY not in repr(e.value.__context__)


def test_transient_errors_are_retried(tmp_path, key_file):
    post = FakePost([(429, b"{}"), (503, b"")])
    slept = []
    y, *_ = tts.synthesize("Y.", {"stability": 0.6}, cache=tmp_path, post=post, key_file=key_file,
                           sleep=slept.append)
    assert len(post.calls) == 3 and len(slept) == 2 and len(y) == 2400


def test_request_budget(tmp_path, key_file):
    usage = tts.Usage(requests=2)
    with pytest.raises(tts.TTSError, match="budget"):
        tts.synthesize("Z.", {}, cache=tmp_path, post=FakePost(), key_file=key_file, usage=usage, max_requests=2)


def test_bad_key_file(tmp_path):
    f = tmp_path / "k.txt"
    f.write_text("two\nlines", encoding="utf-8")
    with pytest.raises(tts.TTSError, match="exactly one key"):
        tts.synthesize("Q.", {}, cache=tmp_path, post=FakePost(), key_file=f)
    with pytest.raises(tts.TTSError, match="cannot read"):
        tts.synthesize("Q.", {}, cache=tmp_path, post=FakePost(), key_file=tmp_path / "missing.txt")
