"""robots.txt wildcard handling in musichistory.http.Robots."""

from musichistory.http import Robots

MIDICOLLECTION = """
User-agent: *
Disallow: /cleanup
Disallow: /midiworld-review
Disallow: /play-remote
Disallow: /*?q=
Sitemap: https://midicollection.com/sitemap.xml
"""

BLOCK_CLAUDE = """
User-agent: ClaudeBot
Disallow: /

User-agent: *
Allow: /
"""


def test_wildcard_query_is_disallowed():
    r = Robots(MIDICOLLECTION)
    assert not r.can_fetch("https://midicollection.com/search?q=queen")
    assert not r.can_fetch("https://midicollection.com/cleanup/x")
    assert r.can_fetch("https://midicollection.com/midi/MIDI/br.mid")
    assert r.can_fetch("https://midicollection.com/artist/queen")


def test_other_agents_group_does_not_apply_to_us():
    r = Robots(BLOCK_CLAUDE)
    assert r.can_fetch("https://example.com/anything")


def test_our_own_group_wins_over_star():
    r = Robots("User-agent: *\nAllow: /\n\nUser-agent: MusicHistory\nDisallow: /private\n")
    assert not r.can_fetch("https://example.com/private/a")
    assert r.can_fetch("https://example.com/public")


def test_longest_match_and_allow_ties():
    r = Robots("User-agent: *\nDisallow: /uploads/\nAllow: /uploads/public/\nDisallow: /*.php$\n")
    assert not r.can_fetch("https://x.org/uploads/1.mid")
    assert r.can_fetch("https://x.org/uploads/public/1.mid")
    assert not r.can_fetch("https://x.org/get_file.php")
    assert r.can_fetch("https://x.org/get_file.php?id=1")  # '$' anchors the end


def test_empty_disallow_allows_everything():
    assert Robots("User-agent: *\nDisallow:\n").can_fetch("https://x.org/a")
