"""CSV list parsers, the Hot 100 first-week index and the resumable API cache."""

from __future__ import annotations

from musichistory.canon import acclaimed, hot100, rollingstone, tsort
from musichistory.canon.net import KVCache


def test_tsort_csv():
    text = ('position,artist,name,year,final_score,raw_usa,raw_eng,raw_eur,raw_row\n'
            '"1","Bing Crosby","White Christmas","1942","43.835","24.492"," 7.745"," 2.703"," 0.540"\n'
            '"2","Bryan Adams","(Everything I Do) I Do it For You","1991","38.346","17.679","13.268","25.204"," 3.121"\n')
    e = tsort.parse(text)
    assert [(x.rank, x.raw_title, x.raw_artist, x.list_year) for x in e] == [
        (1, "White Christmas", "Bing Crosby", 1942), (2, "(Everything I Do) I Do it For You", "Bryan Adams", 1991)]
    assert tsort.decode("Beyonc\xe9".encode("cp1252")) == "Beyoncé"


def test_rolling_stone_files():
    e = rollingstone.parse_2021("Rank,Title,Artist,Year\n500,Stronger,Kanye West,2007\n1,Respect,Aretha Franklin,1967\n")
    assert [(x.rank, x.raw_title, x.list_year) for x in e] == [(1, "Respect", 1967), (500, "Stronger", 2007)]
    e = rollingstone.parse_2004("RANK\tTITLE\tARTIST\n2\tSatisfaction\tThe Rolling Stones\n")
    assert (e[0].rank, e[0].raw_title, e[0].list_year) == (2, "Satisfaction", None)


def test_hot100_first_week_matches_title_and_performer():
    text = ("chart_week,current_week,title,performer,last_week,peak_pos,wks_on_chart,wks_at_no1\n"
            "1965-08-07,40,Unchained Melody,The Righteous Brothers,NA,40,1,NA\n"
            "1965-07-31,90,Unchained Melody,The Righteous Brothers,NA,90,1,NA\n"
            "1990-10-06,50,Unchained Melody,The Righteous Brothers,NA,13,1,NA\n"
            "1958-09-01,70,Unchained Melody,Roy Hamilton,NA,70,1,NA\n"
            "2015-01-01,10,Hello,Adele,NA,1,1,NA\n")
    idx = hot100.Hot100Index(hot100.parse(text))
    assert idx.n_runs == 3                       # one run per (title, performer)
    run = idx.first_week(["Unchained Melody"], ["Righteous Brothers"])
    assert (run.performer, run.first_week) == ("The Righteous Brothers", "1965-07-31")
    assert idx.first_week(["Unchained Melody"], ["Righteous Brothers", "Roy Hamilton"]).first_week == "1958-09-01"
    assert idx.first_week(["Hello"], ["Lionel Richie"]) is None


def test_hot100_performer_must_be_the_lead_act():
    assert hot100.chart_artist_ok("Rihanna Featuring Jay-Z", "Rihanna & Jay-Z")
    assert hot100.chart_artist_ok("Bill Haley And His Comets", "Bill Haley & His Comets")
    assert not hot100.chart_artist_ok("Pete Drake", "Drake featuring Kanye West, Lil Wayne and Eminem")
    idx = hot100.Hot100Index(hot100.parse(
        "chart_week,current_week,title,performer,last_week,peak_pos,wks_on_chart,wks_at_no1\n"
        "1964-03-07,25,Forever,Pete Drake,NA,25,1,NA\n2009-09-05,8,Forever,Drake Featuring Kanye West,NA,8,1,NA\n"))
    assert idx.first_week(["Forever"], ["Drake featuring Kanye West, Lil Wayne and Eminem"]).first_week == "2009-09-05"


def test_acclaimed_import(tmp_path):
    p = tmp_path / "am.csv"
    p.write_text("Pos;Artist;Title;Year\n1;Bob Dylan;Like a Rolling Stone;1965\n2;Nirvana;Smells Like Teen Spirit;1991\n",
                 encoding="utf-8")
    got = acclaimed.load(p)
    assert got.source.weight == 5.0
    assert [(x.rank, x.raw_title, x.raw_artist, x.list_year) for x in got.entries] == [
        (1, "Like a Rolling Stone", "Bob Dylan", 1965), (2, "Smells Like Teen Spirit", "Nirvana", 1991)]


def test_kv_cache_roundtrip(tmp_path):
    kv = KVCache(tmp_path / "c.sqlite")
    kv.put("wd", "Q1", {"P31": ["Q7366"]})
    kv.put_many("wd", {"Q2": {}, "Q3": {"label": "x"}})
    assert kv.get("wd", "Q1") == {"P31": ["Q7366"]}
    assert kv.get_many("wd", ["Q2", "Q3", "Q4"]) == {"Q2": {}, "Q3": {"label": "x"}}
    assert kv.get("search", "Q1") is None and kv.count("wd") == 3
    kv.close()
