"""Licence filtering, HTML stripping and photo dates (musichistory.photos.licence)."""

from __future__ import annotations

import pytest

from musichistory.photos import licence


def ext(**fields):
    """An imageinfo ``extmetadata`` dict from plain values."""
    return {k: {"value": v, "source": "commons-desc-page"} for k, v in fields.items()}


# ---------------------------------------------------------------------------- HTML
@pytest.mark.parametrize("raw, text", [
    ('<a href="//commons.wikimedia.org/wiki/User:Jane_Doe" title="User:Jane Doe">Jane Doe</a>', "Jane Doe"),
    ('<bdi><a href="https://www.wikidata.org/wiki/Q1"><span>Ann&nbsp;Lee</span></a></bdi> for '
     '<a href="//x">Anefo</a>', "Ann Lee for Anefo"),
    ("Tom &amp; Jerry&#039;s studio", "Tom & Jerry's studio"),
    ('<span style="display: none;">hidden vcard</span>Visible Name', "Visible Name"),
    ('<span hidden>secret</span>Shown', "Shown"),
    ("<style>.x{color:red}</style><script>alert(1)</script>Plain", "Plain"),
    ("Line one<br>Line two<br/>Line one", "Line one; Line two"),
    ("<div><p>Unclosed <b>bold", "Unclosed bold"),
    ("  lots   of\n\t space  ", "lots of space"),
    ("", ""),
    (None, ""),
])
def test_strip_html(raw, text):
    assert licence.strip_html(raw) == text


def test_strip_html_leaves_no_markup():
    out = licence.strip_html('<table><tr><td><a href="x">A</a></td><td>B &lt;c&gt;</td></tr></table>')
    assert "<a" not in out and "href" not in out
    assert out == "A; B <c>"  # an escaped bracket in the text is text, not a tag


# ---------------------------------------------------------------------------- licences
FREE = [
    (ext(License="cc0", LicenseShortName="CC0", AttributionRequired="false",
         LicenseUrl="http://creativecommons.org/publicdomain/zero/1.0/deed.en", Artist="Jack"),
     "cc0", "CC0", "https://creativecommons.org/publicdomain/zero/1.0/deed.en"),
    (ext(License="cc-by-sa-4.0", LicenseShortName="CC BY-SA 4.0", AttributionRequired="true",
         LicenseUrl="https://creativecommons.org/licenses/by-sa/4.0", Artist="<a href='x'>Jo</a>"),
     "cc-by-sa", "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0"),
    (ext(License="cc-by-2.0", LicenseShortName="CC BY 2.0", AttributionRequired="true", Artist="Flickr user"),
     "cc-by", "CC BY 2.0", "https://creativecommons.org/licenses/by/2.0/"),
    (ext(License="cc-by-3.0-de", AttributionRequired="true", Artist="Bundesarchiv"),
     "cc-by", "CC BY 3.0 de", "https://creativecommons.org/licenses/by/3.0/de/"),
    (ext(License="pd", LicenseShortName="Public domain", AttributionRequired="false"),
     "pd", "Public domain", None),
    (ext(License="pd-us-no-notice", LicenseShortName="Public domain", Artist="Unknown photographer"),
     "pd", "Public domain", None),
    (ext(LicenseShortName="No restrictions", UsageTerms="No known copyright restrictions"), "pd", "No restrictions", None),
    (ext(License="gfdl", LicenseShortName="GFDL 1.2", AttributionRequired="true", Artist="Self"), "gfdl", "GFDL 1.2", None),
    (ext(License="attribution", LicenseShortName="Attribution", AttributionRequired="true", Artist="Studio"),
     "attribution", "Attribution", None),
    (ext(License="fal", LicenseShortName="FAL", AttributionRequired="true", Artist="Artiste"), "fal", "FAL", None),
]


@pytest.mark.parametrize("meta, family, short, url", FREE)
def test_free_licences_pass(meta, family, short, url):
    lic = licence.check(meta)
    assert lic.ok, lic.reason
    assert (lic.family, lic.short, lic.url) == (family, short, url)
    assert lic.author and "<" not in lic.author


@pytest.mark.parametrize("meta, why", [
    (ext(License="cc-by-sa-4.0", LicenseShortName="CC BY-SA 4.0", NonFree="true", Artist="X"), "non-free"),
    (ext(LicenseShortName="Fair use", UsageTerms="Non-free media, fair use", Artist="Label"), "not free"),
    (ext(License="cc-by-nc-sa-2.0", LicenseShortName="CC BY-NC-SA 2.0", Artist="X"), "not free"),
    (ext(License="cc-by-nd-4.0", LicenseShortName="CC BY-ND 4.0", Artist="X"), "not free"),
    (ext(LicenseShortName="CC BY-NC 2.0", Artist="X"), "not free"),
    (ext(UsageTerms="All rights reserved", Artist="X"), "not free"),
    (ext(Artist="Somebody"), "no licence"),
    ({}, "no metadata"),
    (None, "no metadata"),
    (ext(LicenseShortName="Custom permission from the band", Artist="X"), "unrecognised"),
    (ext(License="cc-by-4.0", LicenseShortName="CC BY 4.0", AttributionRequired="true"), "attribution required"),
    (ext(License="cc-by-4.0", LicenseShortName="CC BY 4.0", Credit="Own work"), "attribution required"),
])
def test_unfree_or_unclear_licences_fail(meta, why):
    lic = licence.check(meta)
    assert not lic.ok
    assert why in lic.reason


def test_credit_stands_in_for_a_missing_artist_but_not_own_work():
    lic = licence.check(ext(License="cc-by-2.0", LicenseShortName="CC BY 2.0",
                            Credit='<a href="https://flickr.com/x">Photo by Sam Example</a>'))
    assert lic.ok and lic.author == "Photo by Sam Example"
    assert licence.author_of(ext(Credit="https://example.org/photo.jpg")) == ""


def test_public_domain_without_author_is_unknown_author():
    lic = licence.check(ext(License="pd", LicenseShortName="Public domain"))
    assert lic.ok and lic.author == "Unknown author" and not lic.attribution_required


# ---------------------------------------------------------------------------- dates
def test_photo_year_sources():
    assert licence.photo_year(ext(DateTimeOriginal="1965-03-25")) == 1965
    assert licence.photo_year(ext(DateTimeOriginal='<time class="dtstart" datetime="1980">1980</time>')) == 1980
    assert licence.photo_year(ext(DateTimeOriginal="circa 1976")) == 1976
    assert licence.photo_year({}, "File:Bob Example performing 1977.jpg") == 1977
    # a scan or upload date later than the year the title states: the title wins
    assert licence.photo_year(ext(DateTimeOriginal="2010-10-24 22:16"), "File:Faith Example 1998.jpg") == 1998
    assert licence.photo_year(ext(DateTimeOriginal="1997-01-01"), "File:Faith Example 1998.jpg") == 1997
    assert licence.photo_year({}, "File:Band (52771234567).jpg") is None  # a Flickr id is not a year
    assert licence.photo_year(ext(Categories="Bob Example|1965 photographs by Jack|CC-Zero")) == 1965
    assert licence.photo_year(ext(Categories="Bob Example in 1976|Concerts")) == 1976
    assert licence.photo_year(ext(DateTimeOriginal="2099-01-01"), latest=2026) is None
