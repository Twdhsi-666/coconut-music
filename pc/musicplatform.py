# -*- coding: utf-8 -*-
"""
某音乐平台歌单解析（免登录读公开歌单）
支持的输入：完整歌单链接 / 短链 / 纯歌单 ID / 分享文案（自动提取链接）
"""
import re
import time

import requests

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'


HEADERS = {
    'User-Agent': UA,
    'Referer': 'https://music.example.com/',
    'Origin': 'https://music.example.com',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

MUSICU_URL = 'https://u.music.example.com/cgi-bin/musicu.fcg'


class MusicApiError(Exception):
    pass




_ID_PATTERNS = [
    r'playlist[/=](\d{6,})',
    r'disstid=(\d{6,})',
    r'dissid=(\d{6,})',
    r'songlist[/=](\d{6,})',
    r'taoge\.html\?id=(\d{6,})',
    r'[?&]id=(\d{6,})',
]


def _grab_id(text):
    for pat in _ID_PATTERNS:
        m = re.search(pat, text or '', re.I)
        if m:
            return m.group(1)
    return None


def extract_disstid(text: str):
    """从任意文本中提取某音乐平台歌单 ID，失败返回 None。

    支持：完整歌单链接 / taoge.html?id=xxx 分享页 / 手机分享短链 / 纯数字 ID / 整段分享文案。
    """
    text = (text or '').strip()
    if not text:
        return None

    if re.fullmatch(r'\d{6,}', text):
        return text

    got = _grab_id(text)
    if got:
        return got

    urls = re.findall(r"https?://[^\s\"'<>）)】\]]+", text)
    for u in urls:
        try:
            r = requests.get(u, headers=HEADERS, timeout=15, allow_redirects=True)
        except Exception:
            continue
        got = _grab_id(r.url) or _grab_id(r.text[:300000])
        if not got: continue
        return got

    m = re.search(r'(\d{8,})', text)
    return m.group(1) if m else None



def _post_diss(dissid, begin, num):
    payload = {
        'comm': {
            'cv': 4747474, 'ct': 24, 'format': 'json',
            'inCharset': 'utf-8', 'outCharset': 'utf-8', 'notice': 0,
            'platform': 'music.json', 'needNewCode': 1,
            'uin': '0', 'g_tk_new_20200303': 5381,
        },
        'req_1': {
            'module': 'music.srfDissInfo.DissInfo',
            'method': 'CgiGetDiss',
            'param': {
                'disstid': int(dissid), 'dirid': 0, 'tag': 1,
                'song_begin': begin, 'song_num': num, 'userinfo': 1,
            },
        },
    }
    r = requests.post(MUSICU_URL, json=payload, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def _normalize_song(s: dict) -> dict:
    singers = s.get('singer') or []
    if isinstance(singers, list):
        artist = '、'.join((x.get('name') or '').strip() for x in singers if x.get('name'))
    else:
        artist = str(singers)
    if not artist:
        artist = s.get('singername') or ''

    album, cover = '', ''
    alb = s.get('album')
    if isinstance(alb, dict):
        album = alb.get('name') or ''
        pmid = alb.get('pmid') or alb.get('mid') or ''
        if pmid:
            cover = f'https://music.example.com/music/photo_new/T002R300x300M000{pmid}.jpg'

    title = s.get('name') or s.get('songname') or s.get('title') or ''
    return {
        'title': title.strip(),
        'artist': artist.strip(),
        'album': album.strip(),
        'duration': int(s.get('interval') or 0),
        'mp_mid': s.get('mid') or s.get('songmid') or '',
        'cover': cover,
    }


def fetch_playlist(dissid, max_songs=1500):
    """分页拉取歌单全部歌曲，返回 (info, songs)"""
    dissid = str(dissid)
    songs, begin = [], 0
    info = {'id': dissid, 'name': '', 'desc': '', 'cover': '', 'total': 0, 'creator': ''}

    while len(songs) < max_songs:
        try:
            js = _post_diss(dissid, begin, 100)
        except Exception as e:
            if songs:
                break
            raise MusicApiError(f'访问歌单来源失败：{e}')
        r1 = js.get('req_1') or {}
        if r1.get('code') != 0:
            msg = r1.get('msg') or r1.get('message') or r1.get('code')
            if songs:
                break
            raise MusicApiError(f'读取歌单失败（{msg}）。如果这是你的私密歌单，请先把它设为公开，或改用一个公开分享链接。')

        d = r1.get('data') or {}
        if not songs:
            info['name'] = (d.get('dissname') or '').strip()
            info['desc'] = (d.get('desc') or '').strip()
            info['total'] = int(d.get('total_song_num') or 0)
            creator = d.get('creator') or {}
            if isinstance(creator, dict):
                info['creator'] = creator.get('nick') or creator.get('name') or ''
            cover = d.get('logo') or d.get('dir_pic_url2') or ''
            if cover:
                info['cover'] = cover

        batch = d.get('songlist') or []
        if not batch:
            break
        for s in batch:
            ns = _normalize_song(s)
            if ns['title']:
                songs.append(ns)
        begin += len(batch)
        if info['total'] and begin >= info['total']:
            break
        time.sleep(0.2)

    if not info['name']:
        info['name'] = f'导入的歌单 {dissid}'
    return info, songs[:max_songs]


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        info, songs = fetch_playlist(extract_disstid(sys.argv[1]))
        print(f'歌单：{info["name"]}  共 {len(songs)} 首')
        for s in songs[:10]:
            print(f'  {s["title"]} - {s["artist"]}  ({s["duration"]}s)')
