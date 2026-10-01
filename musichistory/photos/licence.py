"""Licence filtering and plain-text metadata for Wikimedia Commons files.

``check(extmetadata)`` reads the ``extmetadata`` of ``prop=imageinfo`` (fields
``LicenseShortName``, ``License``, ``UsageTerms``, ``LicenseUrl``, ``AttributionRequired``,
``NonFree``, ``Artist``, ``Credit``) and returns a ``Licence`` that is either usable or carries
the reason it is not. Only free licences pass:

* CC0 and public domain (any ``pd-*`` template, "Public domain", Flickr's "No restrictions");
* CC BY and CC BY-SA, any version or port;
* similar free licences Commons hosts: GFDL, Free Art License, "Attribution" (the plain
  attribution-only template), "Copyrighted free use", UK Open Government Licence.

Anything flagged ``NonFree``, any fair-use, non-commercial or no-derivatives terms, an
unrecognised licence or no licence at all is rejected; so is a file whose licence requires
attribution when no author or credit can be read from it.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser

# --------------------------------------------------------------------------- HTML -> text
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
                   "track", "wbr"})
_BREAK = frozenset({"br", "p", "div", "li", "tr", "td", "th", "dd", "dt", "table", "ul", "ol"})
_NL = "\x00"  # a line break from markup (raw newlines are plain whitespace)
_SKIP = frozenset({"script", "style"})
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []  # (tag, hides its content)

    def _hidden(self) -> bool:
        return any(h for _, h in self.stack)

    def handle_starttag(self, tag, attrs):
        if tag in _BREAK:
            self.parts.append(_NL)
        if tag in _VOID:
            return
        a = dict(attrs)
        hides = tag in _SKIP or bool(_HIDDEN_STYLE.search(a.get("style") or "")) or "hidden" in a
        self.stack.append((tag, hides))

    def handle_startendtag(self, tag, attrs):
        if tag in _BREAK:
            self.parts.append(_NL)

    def handle_endtag(self, tag):
        if tag in _BREAK:
            self.parts.append(_NL)
        for i in range(len(self.stack) - 1, -1, -1):  # tolerate unclosed tags
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self._hidden():
            self.parts.append(data)


def strip_html(value: str | None) -> str:
    """Plain text of an HTML fragment: tags dropped (hidden and script/style content too),
    entities decoded, whitespace collapsed, repeated lines removed."""
    if not value:
        return ""
    p = _Text()
    try:
        p.feed(str(value))
        p.close()
        text = "".join(p.parts)
    except Exception:  # malformed beyond repair: drop anything tag-like
        text = html.unescape(re.sub(r"<[^>]*>", " ", str(value)))
    lines = []
    for line in text.split(_NL):
        line = re.sub(r"\s+", " ", line.replace("\u00a0", " ")).strip(" ,;")
        if line and line not in lines:
            lines.append(line)
    return re.sub(r"\s+", " ", "; ".join(lines)).strip()


# --------------------------------------------------------------------------- licences
def meta(ext: dict | None, key: str) -> str:
    """Plain-text value of one extmetadata field ('' when absent)."""
    v = (ext or {}).get(key)
    if isinstance(v, dict):
        v = v.get("value")
    return strip_html(v if isinstance(v, str) else ("" if v is None else str(v)))


def _truthy(s: str) -> bool:
    return s.strip().lower() in ("true", "1", "yes")


_NOT_FREE = re.compile(
    r"fair[\s_-]*use|non[\s_-]*free|non[\s_-]*commercial|\bby-nc\b|\bnc\b|-nc-|-nc$|\bnd\b|-nd-|-nd$|no[\s_-]*deriv"
    r"|all rights reserved|permission only|educational use only", re.I)

# (pattern over the machine code "License", pattern over the short name / usage terms, family)
_FREE: list[tuple[str, str, str]] = [
    (r"^cc0\b|^cc-zero", r"\bcc0\b|cc[\s-]?zero|public domain dedication", "cc0"),
    (r"^pd\b|^pd-|^public[\s_-]*domain", r"public[\s_-]*domain|^pd\b|^pd-|^no restrictions$", "pd"),
    (r"^cc-by-sa(-[\w.]+)*$", r"\bcc[\s-]by[\s-]sa\b|attribution[\s-]share[\s-]?alike", "cc-by-sa"),
    (r"^cc-by(-[\d.]+)?(-[a-z]{2,3})?$", r"\bcc[\s-]by\b(?![\s-](sa|nc|nd))", "cc-by"),
    (r"^gfdl", r"\bgfdl\b|gnu free documentation", "gfdl"),
    (r"^fal$|^free[\s_-]*art", r"free art licen[cs]e|licence art libre|\bfal\b", "fal"),
    (r"^attribution$|^attribution-only", r"^attribution$|^attribution only", "attribution"),
    (r"^copyrighted[\s_-]*free[\s_-]*use", r"copyrighted free use", "copyrighted-free-use"),
    (r"^ogl", r"open government licen[cs]e", "ogl"),
]

_CC0_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
_CC_BY_URL = re.compile(r"^cc-by(?P<sa>-sa)?-(?P<v>[\d.]+)(?:-(?P<port>[a-z]{2,3}))?$")


@dataclass
class Licence:
    ok: bool
    reason: str = ""          # why it was rejected ('' when ok)
    family: str = ""          # cc0 | pd | cc-by | cc-by-sa | gfdl | fal | attribution | ...
    short: str = ""           # display name, e.g. "CC BY-SA 4.0"
    url: str | None = None    # licence deed
    author: str = ""          # plain text
    attribution_required: bool = False


def _norm_url(url: str) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    url = re.sub(r"^http://", "https://", url)
    return url if url.startswith("https://") else None


def _default_url(code: str, family: str) -> str | None:
    if family == "cc0":
        return _CC0_URL
    m = _CC_BY_URL.match(code)
    if m:
        kind = "by-sa" if m.group("sa") else "by"
        port = f"{m.group('port')}/" if m.group("port") else ""
        return f"https://creativecommons.org/licenses/{kind}/{m.group('v')}/{port}"
    return None


def _short_name(code: str, short: str, family: str) -> str:
    if short:
        return short
    if family == "pd":
        return "Public domain"
    if family == "cc0":
        return "CC0"
    m = _CC_BY_URL.match(code)
    if m:
        return f"CC {'BY-SA' if m.group('sa') else 'BY'} {m.group('v')}" + (f" {m.group('port')}" if m.group("port") else "")
    return code.upper() if code else family


def author_of(ext: dict | None) -> str:
    """The plain-text author: the ``Artist`` field, else a ``Credit`` that names someone
    ("Own work" and bare links name nobody)."""
    artist = meta(ext, "Artist")
    if artist:
        return artist
    credit = meta(ext, "Credit")
    if credit and not re.fullmatch(r"(?i)(own work|self-?made|selbst fotografiert|travail personnel|https?://\S+|\S+\.\w{2,4}/\S*)[.;\s]*",
                                   credit):
        return credit
    return ""


def check(ext: dict | None) -> Licence:
    """Whether a Commons file's licence (its ``extmetadata``) is free; see the module doc."""
    if not ext:
        return Licence(False, "no metadata")
    if _truthy(meta(ext, "NonFree")):
        return Licence(False, "non-free")
    code = meta(ext, "License").lower()
    short = meta(ext, "LicenseShortName")
    terms = meta(ext, "UsageTerms")
    if not (code or short or terms):
        return Licence(False, "no licence")
    blob = " ".join((code, short, terms))
    if _NOT_FREE.search(blob):
        return Licence(False, f"not free: {short or code or terms}")
    family = ""
    for code_re, text_re, fam in _FREE:
        if (code and re.search(code_re, code, re.I)) or any(re.search(text_re, t, re.I) for t in (short, terms) if t):
            family = fam
            break
    if not family:
        return Licence(False, f"unrecognised licence: {short or code or terms}")
    author = author_of(ext)
    attribution = _truthy(meta(ext, "AttributionRequired")) or family not in ("cc0", "pd")
    if attribution and not author:
        return Licence(False, "attribution required but no author given", family)
    url = _norm_url(meta(ext, "LicenseUrl")) or _default_url(code, family)
    return Licence(True, "", family, _short_name(code, short, family), url, author or "Unknown author", attribution)


# --------------------------------------------------------------------------- dates
_YEAR = re.compile(r"(?<!\d)(18[5-9]\d|19\d\d|20\d\d)(?!\d)")


def year_in(text: str | None, latest: int = 2100) -> int | None:
    """The first plausible year (1850..latest) in ``text``."""
    for m in _YEAR.finditer(text or ""):
        y = int(m.group(1))
        if y <= latest:
            return y
    return None


def photo_year(ext: dict | None, title: str = "", latest: int = 2100) -> int | None:
    """When the photograph was taken: ``DateTimeOriginal``, else a year in the file title,
    else a year-specific category ("1965 photographs ...", "X in 1976")."""
    y = year_in(meta(ext, "DateTimeOriginal"), latest)
    in_title = year_in(re.sub(r"\(\d{6,}\)", "", title), latest)  # Flickr ids are not years
    if y and in_title and in_title < y:
        return in_title  # "Faith Evans 1998.jpg" with a 2010 scan/upload date: the title knows
    if y or in_title:
        return y or in_title
    for cat in meta(ext, "Categories").split("|"):
        if re.search(r"(?i)\b(18|19|20)\d\d (photographs|portrait|concert|in)\b|\bin (18|19|20)\d\d\b", cat):
            y = year_in(cat, latest)
            if y:
                return y
    return None
