"""Artist/title normalization shared by the song list and every MIDI source.

Two notions of a key:

* ``title_key`` / ``artist_key``: folded, punctuation-free, version suffixes and featuring
  credits removed. Used to match list rows to each other and to MIDI filenames.
* ``title_core``: ``title_key`` with every parenthetical dropped ("(I Can't Get No)
  Satisfaction" -> "satisfaction"). Kept as a second key because either form may be the
  one a transcriber used.

Artists compare as an order-insensitive token set (handles "Gaye, Marvin" and
"ARMSTRONG LOUIS") and in a "squashed" form without spaces ("U 2" == "U2").

Never strip a bare leading "with" from a title ("With or Without You").
"""

from __future__ import annotations

import re
import unicodedata

# Featuring credits: "feat./ft./featuring X" anywhere, "(with X)" only in brackets.
_FEAT = re.compile(
    r"\s*(?:[\(\[]\s*(?:feat\.?|featuring|ft\.?|with)\s+[^\)\]]*[\)\]]"
    r"|\s(?:feat\.?|featuring|ft\.?)\s+.*$)",
    re.I,
)
# Suffixes that mark a version of the same song rather than a different song.
_VERSION = re.compile(
    r"\s*(?:[\(\[]|\s[\-–—]\s)\s*[^\)\]]*\b(?:remaster(?:ed)?|live|mono|stereo|single|"
    r"radio edit|edit|version|mix|remix|demo|acoustic|unplugged|instrumental|karaoke|extended|"
    r"original|album|from [^\)\]]*|bonus track|deluxe|re-?recorded)\b[^\)\]]*[\)\]]?\s*$",
    re.I,
)
_PAREN = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_BAND_SUFFIX = re.compile(r"\s+(?:and\s+)?(?:his|her|their)\s+(?:orchestra|comets|band|trio|quartet)\b.*$")
# Separators between acts in a credit. "&" is deliberately NOT here: duos such as
# "Simon & Garfunkel" are one act; compare them as token sets instead.
_ACT_SPLIT = re.compile(r"\s+(?:featuring|feat\.?|ft\.?|with|x|vs\.?|duet with)\s+|,|/|;", re.I)

ARTIST_ALIASES = {
    "guns n roses": "guns and roses",
    "guns n  roses": "guns and roses",
    "the guns n roses": "guns and roses",
    "earth wind and fire": "earth wind and fire",
    "run dmc": "run d m c",
    "ac dc": "acdc",
}


def fold(s: str) -> str:
    """ASCII-fold, lowercase, map & and + to "and", drop apostrophes, punctuation to spaces."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ").replace("+", " and ")
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"[^0-9a-zA-Z]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def drop_g(s: str) -> str:
    """goin' == going, lovin' == loving."""
    return re.sub(r"ing\b", "in", s)


def squash(s: str) -> str:
    return drop_g(s).replace(" ", "")


def squash_loose(s: str) -> str:
    """``squash`` that reads every "ing" as "in", inside run-together words too.

    MIDI file names are often CamelCase or squashed ("DancingQueen3", "beegeesstayingalive"),
    where drop_g's word-final rule cannot see the "ing". The MIDI sources compare squashed
    title/artist forms with squashed file names, so both sides use this form. ``squash``
    itself is unchanged: the song list's keys rely on it.
    """
    return squash(s).replace("ing", "in")


def title_key(title: str) -> str:
    t = _FEAT.sub("", title or "")
    prev = None
    while prev != t:
        prev = t
        t = _VERSION.sub("", t)
    return drop_g(fold(t))


def title_core(title: str) -> str:
    t = _FEAT.sub("", title or "")
    prev = None
    while prev != t:
        prev = t
        t = _VERSION.sub("", t)
    core = _PAREN.sub("", t).strip()
    if len(re.sub(r"[^0-9A-Za-z]", "", core)) < 2:
        core = t
    core = fold(core)
    core = re.sub(r"^(?:the|a|an) ", "", core)
    return drop_g(core)


def artist_key(artist: str) -> str:
    a = _FEAT.sub("", artist or "")
    f = fold(a)
    f = re.sub(r"^the ", "", f)
    f = re.sub(r" the$", "", f)
    f = _BAND_SUFFIX.sub("", f)
    return ARTIST_ALIASES.get(f, f)


def primary_artist(artist: str) -> str:
    """First act of a credit: "Mark Ronson feat. Bruno Mars" -> "mark ronson"."""
    first = _ACT_SPLIT.split(artist or "")[0]
    return artist_key(first)


def artist_tokens(artist: str) -> list[str]:
    n = artist_key(artist)
    toks = [drop_g(t) for t in n.split() if t != "and" and (len(t) > 1 or n == t)]
    return toks or [n]


def filename_tokens(path: str) -> str:
    """Squashed, folded form of a MIDI filename or path for containment checks."""
    base = re.sub(r"\.(mid|midi|kar|rmi)$", "", path, flags=re.I)
    base = re.sub(r"\.\d+$", "", base)  # clean_midi duplicate counters: Title.1.mid
    base = base.replace("_", " ").replace("-", " ").replace(".", " ")
    return squash(fold(base))


def strip_featuring(s: str) -> str:
    """Remove featuring credits ("feat. X", "(with X)") from a title or artist credit."""
    return _FEAT.sub("", s or "").strip()


# Words that may follow an act's own name without making it a different act:
# "Elvis Presley and the Jordanaires", "Prince & The Revolution".
_ACT_JOINERS = {"and", "with", "feat", "featuring", "ft", "x", "vs"}


def artist_matches(candidate: str, artist: str, threshold: float = 85.0) -> float:
    """Score 0..100 that ``candidate`` names ``artist``.

    Word order is ignored ("Gaye, Marvin" == "Marvin Gaye"), but every token of ``artist``
    must appear as a whole word, and extra words are only accepted as a second act joined
    by "and"/"with"/...: "Queen" does not match "Queen Latifah" or "Queensryche", and
    "Drake" does not match "Pete Drake".
    """
    from rapidfuzz import fuzz

    a, c = artist_key(artist), artist_key(candidate)
    if not a or not c:
        return 0.0
    if squash(a) == squash(c):
        return 100.0
    score = float(fuzz.token_sort_ratio(a, c))
    a_toks = [drop_g(t) for t in a.split() if t != "and"]
    c_words = [drop_g(w) for w in c.split()]
    if a_toks and all(t in c_words for t in a_toks):
        extra = [w for w in c_words if w not in a_toks and w != "and"]
        if not extra:
            score = max(score, 97.0)
        else:
            # Extra words are fine only after a joiner that follows the whole artist name.
            last = max(c_words.index(t) for t in a_toks)
            first = min(c_words.index(t) for t in a_toks)
            tail = c_words[last + 1:]
            if first == 0 and tail and tail[0] in _ACT_JOINERS and len(a_toks) == last - first + 1:
                score = max(score, 90.0)
            else:
                score = min(score, 80.0)
    else:
        # The reverse: a file filed under "Prince" for the credit "Prince & The Revolution".
        a_words = [drop_g(w) for w in a.split()]
        n = len(c_words)
        if 0 < n < len(a_words) and a_words[:n] == c_words and a_words[n] in _ACT_JOINERS:
            score = max(score, 90.0)
    return score


def title_matches(candidate: str, title: str) -> float:
    """Score 0..100 that ``candidate`` is the title ``title`` (either key form)."""
    from rapidfuzz import fuzz

    best = 0.0
    for a in {title_key(title), title_core(title)}:
        for c in {title_key(candidate), title_core(candidate)}:
            if not a or not c:
                continue
            if squash(a) == squash(c):
                return 100.0
            best = max(best, float(fuzz.token_sort_ratio(a, c)))
    return best


def slug(s: str, maxlen: int = 60) -> str:
    return fold(s).replace(" ", "-")[:maxlen].strip("-")
