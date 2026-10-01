"""A fake Wikidata + Commons + thumbnail server for the photos tests (no network).

Every artist, file and person here is invented.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from musichistory.http import Fetched


def jpeg(width: int, height: int) -> bytes:
    """A minimal JPEG byte string whose SOF0 marker states ``width`` x ``height``."""
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
           + b"\x03" + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def ext(**fields):
    return {k: {"value": v, "source": "commons-desc-page"} for k, v in fields.items()}


CC_BY_SA = dict(License="cc-by-sa-4.0", LicenseShortName="CC BY-SA 4.0", AttributionRequired="true",
                LicenseUrl="https://creativecommons.org/licenses/by-sa/4.0")
CC0 = dict(License="cc0", LicenseShortName="CC0", AttributionRequired="false",
           LicenseUrl="http://creativecommons.org/publicdomain/zero/1.0/deed.en")
FAIR_USE = dict(LicenseShortName="Fair use", NonFree="true", UsageTerms="Non-free media")
NC = dict(License="cc-by-nc-2.0", LicenseShortName="CC BY-NC 2.0", AttributionRequired="true")


def file(title, *, width=1600, height=1200, mime="image/jpeg", thumb_width=960, **meta):
    name = title.removeprefix("File:")
    url_name = quote(name.replace(" ", "_"))
    thumb = None
    if thumb_width:
        thumb = (f"https://thumb.example.org/thumb/{url_name}/{thumb_width}px-{url_name}"
                 "?utm_source=commons.wikimedia.org&utm_campaign=imageinfo")
    info = {"width": width, "height": height, "mime": mime, "size": width * height // 4,
            "url": f"https://upload.example.org/{url_name}?utm_source=x",
            "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{url_name}?utm_source=x",
            "extmetadata": ext(**meta)}
    if thumb:
        info.update(thumburl=thumb, thumbwidth=720, thumbheight=round(720 * height / width))
    return {"title": "File:" + name, "info": info, "thumb_px": (thumb_width or width, round((thumb_width or width) * height / width))}


def entity(qid, label, *, human=True, p18=(), p373=None, members=()):
    def st(p, v, rank="normal"):
        dv = {"value": {"id": v}} if p in ("P31", "P527") else {"value": v}
        return {"mainsnak": {"snaktype": "value", "property": p, "datavalue": dv}, "rank": rank}
    claims = {"P31": [st("P31", "Q5" if human else "Q215380")]}
    if p18:
        claims["P18"] = [st("P18", f) for f in p18]
    if p373:
        claims["P373"] = [st("P373", p373)]
    if members:
        claims["P527"] = [st("P527", m) for m in members]
    return {"id": qid, "labels": {"en": {"language": "en", "value": label}}, "claims": claims}


class FakeWikimedia:
    """Answers wbgetentities, imageinfo (by titles), depicts searches, category listings
    and thumbnail downloads; records every request."""

    def __init__(self, entities=(), files=(), depicts=None, categories=None, fail_downloads=()):
        self.entities = {e["id"]: e for e in entities}
        self.files = {f["title"]: f for f in files}
        self.depicts = depicts or {}          # qid -> [file titles]
        self.categories = categories or {}    # "Category:X" -> [file titles]
        self.fail_downloads = set(fail_downloads)
        self.calls: list[tuple[str, dict]] = []

    def _page(self, title, index=None):
        f = self.files.get(title)
        if f is None:
            return {"title": title, "missing": True}
        page = {"title": title, "ns": 6, "imageinfo": [f["info"]]}
        if index is not None:
            page["index"] = index
        return page

    def get(self, url, *, params=None, robots=True, use_cache=True, **kw):
        params = dict(params or {})
        self.calls.append((url, params))
        if params.get("action") == "wbgetentities":
            ents = {q: self.entities.get(q, {"id": q, "missing": ""}) for q in params["ids"].split("|")}
            return self._json({"entities": ents})
        if params.get("action") == "query":
            if params.get("list") == "categorymembers":
                members = [{"ns": 6, "title": t} for t in self.categories.get(params["cmtitle"], [])]
                return self._json({"query": {"categorymembers": members}})
            if params.get("generator") == "search":
                qid = re.search(r"P180=(Q\d+)", params["gsrsearch"]).group(1)
                pages = [self._page(t, i + 1) for i, t in enumerate(self.depicts.get(qid, []))]
                return self._json({"query": {"pages": pages}} if pages else {"batchcomplete": True})
            if "titles" in params:
                titles = params["titles"].split("|")
                norm = [{"from": t, "to": t.replace("_", " ")} for t in titles if "_" in t]
                pages = [self._page(t.replace("_", " ")) for t in titles]
                return self._json({"query": {"normalized": norm, "pages": pages}})
        # a thumbnail download
        assert "utm_" not in url, "tracking parameters must be stripped"
        for f in self.files.values():
            if f["info"].get("thumburl", "").split("?")[0] == url or f["info"]["url"].split("?")[0] == url:
                if f["title"] in self.fail_downloads:
                    return Fetched(url, 404, b"", "text/html", {})
                return Fetched(url, 200, jpeg(*f["thumb_px"]), "image/jpeg", {})
        return Fetched(url, 404, b"", "text/html", {})

    @staticmethod
    def _json(doc):
        return Fetched("api", 200, json.dumps(doc).encode(), "application/json", {})

    def kinds(self):
        out = []
        for url, p in self.calls:
            if p.get("action") == "wbgetentities":
                out.append("wbgetentities")
            elif p.get("list") == "categorymembers":
                out.append("categorymembers")
            elif p.get("generator") == "search":
                out.append("depicts")
            elif p.get("action") == "query":
                out.append("imageinfo")
            else:
                out.append("download")
        return out


def fake_resize(data: bytes, max_width: int = 720, ffmpeg=None) -> bytes:
    """Stands in for ffmpeg: a JPEG scaled to ``max_width`` when wider."""
    from musichistory.photos.catalogue import jpeg_size

    w, h = jpeg_size(data)
    if w <= max_width:
        return data
    return jpeg(max_width, round(h * max_width / w))
