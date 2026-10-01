"""The photo catalogue ``data/images/artists.json`` (DESIGN §15).

``{"version": 1, "images": {"artist-<QID>": {"file": "artists/artist-<QID>.jpg", "subject",
"artist_qid", "work_ids", "commons_page", "author", "license", "license_url", "caption",
"width", "height", "year", "source"}}}``

``file`` is relative to ``data/images``. ``subject`` is who the photo shows (the act, or the
leader of a group when only the leader's portrait was free), ``author`` plain text,
``license`` the short licence name, ``license_url`` its deed (null for public domain without
one), ``year`` when the photo was taken (null when unknown), ``source`` how it was found
(``p18`` | ``depicts`` | ``category`` | ``leader``). ``validate`` checks a document against
this contract.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

VERSION = 1
MAX_WIDTH = 720
REQUIRED = {
    "file": str, "subject": str, "artist_qid": str, "work_ids": list, "commons_page": str, "author": str,
    "license": str, "license_url": (str, type(None)), "caption": str, "width": int, "height": int,
}
OPTIONAL = {"year": (int, type(None)), "source": str, "commons_file": str}
SOURCES = frozenset({"p18", "depicts", "category", "leader"})


def image_id(qid: str) -> str:
    return f"artist-{qid}"


def file_for(qid: str) -> str:
    return f"artists/{image_id(qid)}.jpg"


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker; None when ``data`` is not a JPEG."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > len(data):
            return None
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > len(data):
                return None
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h)
        if marker == 0xD9:
            return None
        i += 2 + length
    return None


def load(path: Path) -> dict:
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("images"), dict):
                return doc
        except ValueError:
            pass
    return {"version": VERSION, "images": {}}


def merge(old: dict, new: dict[str, dict], keep=lambda key, entry: True) -> dict:
    """``old``'s images updated with ``new`` (an artist found again keeps the union of its
    work ids when the photo is unchanged); old entries are kept when ``keep`` says so."""
    images = {k: v for k, v in (old.get("images") or {}).items() if keep(k, v)}
    for k, v in new.items():
        prev = images.get(k)
        if prev and prev.get("commons_page") == v.get("commons_page"):
            v = {**v, "work_ids": sorted(set(prev.get("work_ids", [])) | set(v["work_ids"]))}
        images[k] = v
    return {"version": VERSION, "images": dict(sorted(images.items(), key=lambda kv: _qnum(kv[0])))}


def _qnum(key: str) -> tuple[int, str]:
    m = re.search(r"Q(\d+)$", key)
    return (int(m.group(1)) if m else 0, key)


def write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def validate(doc: dict, images_root: Path | None = None) -> list[str]:
    """Contract errors of a catalogue document ([] when valid). With ``images_root`` the
    files must exist, be JPEGs and match the stated size."""
    errs: list[str] = []
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
        return [f"version must be {VERSION}"]
    images = doc.get("images")
    if not isinstance(images, dict):
        return ["images must be an object"]
    for key, e in images.items():
        where = f"images[{key}]"
        m = re.fullmatch(r"artist-(Q\d+)", key)
        if not m:
            errs.append(f"{where}: id must be artist-<QID>")
            continue
        if not isinstance(e, dict):
            errs.append(f"{where}: not an object")
            continue
        for f, t in REQUIRED.items():
            if f not in e:
                errs.append(f"{where}: missing {f}")
            elif not isinstance(e[f], t) or isinstance(e[f], bool):
                errs.append(f"{where}: {f} has the wrong type")
        for f, t in OPTIONAL.items():
            if f in e and not isinstance(e[f], t):
                errs.append(f"{where}: {f} has the wrong type")
        extra = set(e) - set(REQUIRED) - set(OPTIONAL)
        if extra:
            errs.append(f"{where}: unknown fields {sorted(extra)}")
        if any(f not in e or not isinstance(e[f], REQUIRED[f]) for f in REQUIRED):
            continue
        if e["artist_qid"] != m.group(1):
            errs.append(f"{where}: artist_qid does not match the id")
        if e["file"] != file_for(m.group(1)):
            errs.append(f"{where}: file must be {file_for(m.group(1))}")
        if not e["work_ids"] or not all(isinstance(w, str) and w for w in e["work_ids"]):
            errs.append(f"{where}: work_ids must be non-empty strings")
        if not e["commons_page"].startswith("https://commons.wikimedia.org/wiki/File:"):
            errs.append(f"{where}: commons_page must be a Commons file page")
        for f in ("subject", "author", "license", "caption"):
            if not e[f].strip():
                errs.append(f"{where}: {f} is empty")
            elif re.search(r"<[a-zA-Z/][^>]*>|&(?:[a-z]+|#\d+);", e[f]):
                errs.append(f"{where}: {f} contains HTML")
        if e["license_url"] is not None and not e["license_url"].startswith("https://"):
            errs.append(f"{where}: license_url must be https")
        if not 0 < e["width"] <= MAX_WIDTH or e["height"] <= 0:
            errs.append(f"{where}: size {e['width']}x{e['height']} out of range")
        if "source" in e and e["source"] not in SOURCES:
            errs.append(f"{where}: unknown source {e['source']}")
        if images_root is not None:
            p = Path(images_root) / e["file"]
            if not p.exists():
                errs.append(f"{where}: {e['file']} is missing")
            else:
                size = jpeg_size(p.read_bytes())
                if size is None:
                    errs.append(f"{where}: {e['file']} is not a JPEG")
                elif size != (e["width"], e["height"]):
                    errs.append(f"{where}: {e['file']} is {size[0]}x{size[1]}, catalogue says {e['width']}x{e['height']}")
    return errs
