"""Fetch official 30-second iTunes previews for the canon songs as MP3s (data/audio/<work_id>/preview.mp3),
for the user's local, personal listening in the walkthrough. The match is recorded in data/audio/manifest.json.

Usage: python tools/fetch_previews.py [limit]   (resumable; songs already fetched are skipped)
ffmpeg is found via $FFMPEG, then PATH, then the known install (musichistory.paths.ffmpeg)."""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from musichistory import config  # noqa: E402
from musichistory.paths.ffmpeg import find_ffmpeg  # noqa: E402

DELAY = 1.0
MIN_SCORE = 0.7


def norm(s: str) -> str:
    s = re.sub(r'[\(\[][^\)\]]*[\)\]]', '', s.lower())
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', '', s)).strip()


def lead(a: str) -> str:
    return re.split(r'\s+(?:with|feat\.?|featuring|&|and his|and her|and the|x)\s+', a, flags=re.I)[0]


def search(term: str) -> list[dict]:
    q = urllib.parse.urlencode({'term': term, 'media': 'music', 'entity': 'song', 'limit': 15, 'country': 'US'})
    req = urllib.request.Request('https://itunes.apple.com/search?' + q, headers={'User-Agent': 'MusicHistory/0.1'})
    for attempt in range(6):
        try:
            return json.load(urllib.request.urlopen(req, timeout=20))['results']
        except Exception as e:
            wait = 30 * (attempt + 1)
            print(f'retry in {wait}s: {e}', flush=True)
            time.sleep(wait)
    return []


def score(title: str, artist: str, r: dict) -> float:
    s = difflib.SequenceMatcher(None, norm(title), norm(r['trackName'])).ratio() * 0.6 \
        + difflib.SequenceMatcher(None, norm(lead(artist)), norm(lead(r['artistName']))).ratio() * 0.4
    if 'live' in r['trackName'].lower() or 'karaoke' in r.get('collectionName', '').lower():
        s -= 0.2
    return s


def songs(limit: int) -> list[tuple[str, str, str]]:
    c = sqlite3.connect(config.PIPELINE_DB)
    try:
        return c.execute("""select s.work_id, w.title, w.canonical_artist from song s join work w using(work_id)
                            join tree_node t using(work_id) order by t.descendants desc, t.katz desc limit ?""",
                         (limit,)).fetchall()
    finally:
        c.close()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    limit = int(argv[0]) if argv else 100000
    ff = find_ffmpeg()
    out = config.DATA / 'audio'
    out.mkdir(parents=True, exist_ok=True)
    mp = out / 'manifest.json'
    rows = songs(limit)
    old = {e['work_id']: e for e in json.loads(mp.read_text(encoding='utf-8'))} if mp.exists() else {}
    done = 0

    def save() -> None:
        mp.write_text(json.dumps(list(old.values()), indent=2, ensure_ascii=False), encoding='utf-8')

    for i, (wid, title, artist) in enumerate(rows):
        e = old.get(wid)
        if e and e.get('status') == 'ok' and (ROOT / e['mp3']).exists():
            continue
        best, bs = None, 0.0
        for term in (f'{title} {artist}', f'{norm(title)} {lead(artist)}'):
            for r in search(term):
                if not r.get('previewUrl'):
                    continue
                s = score(title, artist, r)
                if s > bs:
                    best, bs = r, s
            if best and bs >= MIN_SCORE:
                break
            time.sleep(DELAY)
        entry = {'work_id': wid, 'title': title, 'artist': artist}
        if not best or bs < MIN_SCORE:
            entry['status'] = f'no confident match (best {bs:.2f})'
            if best:
                entry['best_guess'] = f"{best['trackName']} - {best['artistName']}"
        else:
            d = out / wid
            d.mkdir(parents=True, exist_ok=True)
            m4a, mp3 = d / 'preview.m4a', d / 'preview.mp3'
            urllib.request.urlretrieve(best['previewUrl'], m4a)
            subprocess.run([ff, '-y', '-loglevel', 'error', '-i', str(m4a), '-codec:a', 'libmp3lame', '-q:a', '2',
                            str(mp3)], check=True)
            m4a.unlink()
            rel = mp3.relative_to(ROOT).as_posix() if mp3.is_relative_to(ROOT) else mp3.as_posix()
            entry.update(status='ok', match_score=round(bs, 2), itunes_track=best['trackName'],
                         itunes_artist=best['artistName'], album=best.get('collectionName'), track_id=best['trackId'],
                         track_url=best.get('trackViewUrl'), preview_url=best['previewUrl'], mp3=rel)
        old[wid] = entry
        done += 1
        print(f"[{i + 1}/{len(rows)}] {title} - {artist}: {entry['status']}", flush=True)
        if done % 10 == 0:
            save()
        time.sleep(DELAY)
    save()
    ok = sum(1 for e in old.values() if e.get('status') == 'ok')
    print(f'DONE: {ok}/{len(old)} ok', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
