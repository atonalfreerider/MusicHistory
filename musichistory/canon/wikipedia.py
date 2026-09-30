"""Wikipedia page wikitext through the MediaWiki API, pinned by revision id.

``action=parse&prop=wikitext|revid`` returns the page source and the revision it came
from; both are cached under ``data/cache/canon/raw/`` so ``source_list.revision`` can
name the exact revision a list was read from.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from ..textnorm import slug
from .net import Net


def page_url(title: str, revid: int | str | None = None) -> str:
    base = "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="()_,'!-")
    return f"https://en.wikipedia.org/w/index.php?oldid={revid}" if revid else base


def fetch_page(net: Net, title: str, *, refresh: bool = False) -> tuple[str, dict] | None:
    """(wikitext, meta) for a page, or None when the page does not exist."""
    name = f"wp_{slug(title, 120)}.json"
    path = net.raw_path(name)
    meta = net.raw_meta(name)
    if path.exists() and meta and not refresh:
        data = json.loads(path.read_bytes())
    else:
        try:
            data = net.wikipedia({"action": "parse", "page": title, "prop": "wikitext|revid", "redirects": "1"})
        except Exception as exc:  # missingtitle and friends
            if "missingtitle" in str(exc):
                return None
            raise
        revid = data["parse"].get("revid")
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        meta = net.store_raw(name, body, {"url": page_url(title, revid), "revision": str(revid), "title": title})
    return data["parse"]["wikitext"], meta
