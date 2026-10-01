"""The photo catalogue contract (musichistory.photos.catalogue)."""

from __future__ import annotations

import json

import pytest
from photos_fakes import jpeg

from musichistory.photos import catalogue


def entry(qid="Q1", **over):
    e = {"file": f"artists/artist-{qid}.jpg", "subject": "Ann Example", "artist_qid": qid, "work_ids": ["W1"],
         "commons_page": "https://commons.wikimedia.org/wiki/File:Ann_Example.jpg", "author": "Pat Photographer",
         "license": "CC BY-SA 4.0", "license_url": "https://creativecommons.org/licenses/by-sa/4.0",
         "caption": "Ann Example in 1985", "width": 720, "height": 540, "year": 1985, "source": "p18"}
    e.update(over)
    return e


def doc(*entries):
    return {"version": 1, "images": {f"artist-{e['artist_qid']}": e for e in entries}}


def test_valid_document_and_files(tmp_path):
    d = doc(entry("Q1"), entry("Q2", license_url=None, license="Public domain", year=None))
    assert catalogue.validate(d) == []
    (tmp_path / "artists").mkdir()
    (tmp_path / "artists" / "artist-Q1.jpg").write_bytes(jpeg(720, 540))
    (tmp_path / "artists" / "artist-Q2.jpg").write_bytes(jpeg(700, 540))
    errs = catalogue.validate(d, tmp_path)
    assert errs == ["images[artist-Q2]: artists/artist-Q2.jpg is 700x540, catalogue says 720x540"]


@pytest.mark.parametrize("over, msg", [
    ({"author": '<a href="x">Pat</a>'}, "author contains HTML"),
    ({"author": "Tom &amp; Jerry"}, "author contains HTML"),
    ({"author": "  "}, "author is empty"),
    ({"width": 960}, "out of range"),
    ({"file": "artists/other.jpg"}, "file must be artists/artist-Q1.jpg"),
    ({"artist_qid": "Q9"}, "artist_qid does not match"),
    ({"work_ids": []}, "work_ids must be non-empty"),
    ({"commons_page": "https://example.org/x.jpg"}, "Commons file page"),
    ({"license_url": "http://creativecommons.org/x"}, "license_url must be https"),
    ({"source": "google"}, "unknown source"),
    ({"width": "720"}, "width has the wrong type"),
    ({"extra": 1}, "unknown fields"),
])
def test_contract_violations(over, msg):
    errs = catalogue.validate({"version": 1, "images": {"artist-Q1": entry(**over)}})
    assert any(msg in e for e in errs), errs


def test_missing_field_and_bad_ids():
    e = entry()
    del e["license"]
    assert "images[artist-Q1]: missing license" in catalogue.validate(doc(e))
    assert catalogue.validate({"version": 2, "images": {}}) == ["version must be 1"]
    assert catalogue.validate({"version": 1, "images": {"bob": entry()}}) == ["images[bob]: id must be artist-<QID>"]


def test_missing_file(tmp_path):
    assert catalogue.validate(doc(entry()), tmp_path) == ["images[artist-Q1]: artists/artist-Q1.jpg is missing"]


def test_jpeg_size():
    assert catalogue.jpeg_size(jpeg(720, 949)) == (720, 949)
    assert catalogue.jpeg_size(b"\x89PNG\r\n\x1a\n....") is None
    assert catalogue.jpeg_size(b"") is None


def test_merge_keeps_other_artists_and_unions_work_ids(tmp_path):
    old = doc(entry("Q5", work_ids=["W9"]), entry("Q1", work_ids=["W0"]), entry("Q7"))
    new = {"artist-Q1": entry("Q1", work_ids=["W1"]),
           "artist-Q7": entry("Q7", commons_page="https://commons.wikimedia.org/wiki/File:New.jpg", work_ids=["W2"])}
    merged = catalogue.merge(old, new, keep=lambda k, v: k != "artist-Q5" or True)
    assert list(merged["images"]) == ["artist-Q1", "artist-Q5", "artist-Q7"]
    assert merged["images"]["artist-Q1"]["work_ids"] == ["W0", "W1"]   # same photo: union
    assert merged["images"]["artist-Q7"]["work_ids"] == ["W2"]         # new photo: replaced
    dropped = catalogue.merge(old, new, keep=lambda k, v: k != "artist-Q5")
    assert "artist-Q5" not in dropped["images"]
    path = tmp_path / "artists.json"
    catalogue.write(path, merged)
    assert catalogue.load(path) == json.loads(path.read_text(encoding="utf-8")) == merged
    assert catalogue.load(tmp_path / "none.json") == {"version": 1, "images": {}}
