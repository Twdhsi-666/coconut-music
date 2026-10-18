# -*- coding: utf-8 -*-
"""本地数据：歌单 / 收藏 / 播放历史 / 设置（原子写入 + 自动备份）"""
import json
import os

import paths
import shutil
import threading
import time
import uuid

BASE_DIR = paths.app_dir()
DATA_DIR = paths.data_dir()
LIB_PATH = os.path.join(DATA_DIR, 'library.json')
BAK_PATH = os.path.join(DATA_DIR, 'library.json.bak')

_lock = threading.RLock()
_cache = None


def _now():
    """当前 Unix 时间戳（秒）"""
    return int(time.time())


def _default():
    """一份全新的数据库骨架"""
    return {
        'version': 1,
        'settings': {
            'theme': 'light',
            'volume': 0.85,
            'playMode': 'list',
            'loudness': True,
            'loudnessLevel': 'standard',
            'primaryColor': '#31c27c',
            'downloadDir': '',
            'eq': {
                'on': False,
                'preset': 'flat',
                'gains': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            },
            'hotkeys': {
                'enabled': True,
                'prev': 'Ctrl+Alt+Left',
                'next': 'Ctrl+Alt+Right',
                'volUp': 'Ctrl+Alt+Up',
                'volDown': 'Ctrl+Alt+Down',
            },
        },
        'playlists': [
            {
                'id': 'fav',
                'name': '我喜欢的音乐',
                'cover': '',
                'source': 'local',
                'created': _now(),
                'songs': [],
            },
        ],
        'history': [],
        'login': {
            'cookie': '',
            'uname': '',
            'mid': '',
            'logged_in': False,
        },
    }


def load(force=False):
    """读库（带缓存）。force=True 强制重读磁盘"""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        os.makedirs(DATA_DIR, exist_ok=True)
        data = None
        if os.path.exists(LIB_PATH):
            try:
                with open(LIB_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = None
        if not isinstance(data, dict):
            data = _default()
        # 补齐缺失的顶层键 / 设置项，兼容旧版本数据文件
        base = _default()
        for k, v in base.items():
            if k not in data:
                data[k] = v
        for k, v in base['settings'].items():
            data.setdefault('settings', {}).setdefault(k, v)
        data.setdefault('login', base['login'])
        _cache = data
        return data


def save():
    """落盘：先写 .tmp，备份旧文件，再原子替换，防止写一半损坏"""
    with _lock:
        if _cache is None:
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = LIB_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_cache, f, ensure_ascii=False, indent=2)
        try:
            if os.path.exists(LIB_PATH):
                shutil.copy2(LIB_PATH, BAK_PATH)
        except Exception:
            pass
        os.replace(tmp, LIB_PATH)
        return _cache


def new_id(prefix='pl'):
    """生成歌单 id，如 pl_1a2b3c4d5e"""
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def list_playlists():
    """歌单摘要列表（不含歌曲明细）"""
    data = load()
    out = []
    for pl in data['playlists']:
        out.append({
            'id': pl['id'],
            'name': pl['name'],
            'cover': pl.get('cover', ''),
            'count': len(pl.get('songs') or []),
            'source': pl.get('source', 'local'),
            'created': pl.get('created', 0),
        })
    return out


def get_playlist(pid):
    """按 id 找歌单，找不到返回 None"""
    for pl in load()['playlists']:
        if pl['id'] == pid:
            return pl
    return None


def create_playlist(name, cover, source='local'):
    """新建歌单"""
    data = load()
    pl = {
        'id': new_id(),
        'name': (name or '新建歌单').strip(),
        'cover': cover,
        'source': source,
        'created': _now(),
        'songs': [],
    }
    data['playlists'].append(pl)
    save()
    return pl


def delete_playlist(pid):
    """删除歌单。「我喜欢的音乐」（fav）是系统歌单，不允许删"""
    data = load()
    if pid == 'fav':
        return False
    before = len(data['playlists'])
    data['playlists'] = [p for p in data['playlists'] if p['id'] != pid]
    save()
    return len(data['playlists']) < before


def rename_playlist(pid, name):
    """重命名歌单"""
    pl = get_playlist(pid)
    if not pl:
        return False
    pl['name'] = (name or pl['name']).strip()
    save()
    return True


def add_song(pid, song, dedupe=True):
    """往歌单加一首歌

    song: {bvid,title,name,artist,duration,cover,source,music_mid}
    """
    pl = get_playlist(pid)
    if not pl:
        return (False, '歌单不存在')
    songs = pl.setdefault('songs', [])
    if dedupe and any(s.get('bvid') == song.get('bvid') for s in songs):
        return (False, '这首歌已经在歌单里了')
    item = dict(song)
    item['added'] = _now()
    songs.append(item)
    save()
    return (True, '已添加')


def add_songs(pid, songs, dedupe=True):
    """批量添加（只写一次磁盘），返回 (added, message)"""
    pl = get_playlist(pid)
    if not pl:
        return (0, '歌单不存在')
    exist = {s.get('bvid') for s in (pl.get('songs') or [])}
    added = 0
    for song in (songs or []):
        if not isinstance(song, dict) or not song.get('bvid'):
            continue
        if dedupe and song['bvid'] in exist:
            continue
        item = dict(song)
        item['added'] = _now()
        pl.setdefault('songs', []).append(item)
        exist.add(song['bvid'])
        added += 1
    save()
    return (added, f'已添加 {added} 首')


def remove_song(pid, bvid):
    """从歌单移除一首歌"""
    pl = get_playlist(pid)
    if not pl:
        return False
    songs = pl.get('songs') or []
    before = len(songs)
    pl['songs'] = [s for s in songs if s.get('bvid') != bvid]
    save()
    return len(pl['songs']) < before


def replace_song(pid, old_bvid, song):
    """换音源：把歌单里某首歌替换成另一条某弹幕网站视频。

    保留原来的歌名/歌手/专辑（那是从某音乐平台带过来的，不该被覆盖）。
    """
    pl = get_playlist(pid)
    if not pl:
        return False
    songs = pl.get('songs') or []
    for i, s in enumerate(songs):
        if s.get('bvid') == old_bvid:
            item = dict(song)
            item['added'] = s.get('added') or _now()
            for keep in ('name', 'artist', 'album', 'source'):
                if s.get(keep):
                    item[keep] = s[keep]
            songs[i] = item
            save()
            return True
    return False


def replace_song_everywhere(old_bvid, song):
    """把「所有歌单 + 播放历史」里 bvid 等于 old_bvid 的条目都换成 song，只落盘一次。

    为什么不能只改一个歌单：
      ① 同一首歌可能同时躺在多个歌单里（导入时没做跨歌单去重）；
      ② 用户很可能是从「最近播放」点进去播的，那条在 history 里，不在任何歌单里；
      ③ 懒匹配每首歌只能成功一次 —— 漏改的入口下次播放又要重新搜一遍某弹幕网站，
         而重复搜索正是触发某弹幕网站风控（csrf 校验失败 / 返回空）的主要原因。

    保留原条目的 name/artist/album/source（那是从某音乐平台带过来的权威元数据，
    不该被视频标题覆盖），与 replace_song 的保留规则一致。
    返回被改动的歌单 id 列表（history 不在其列）。
    """
    if not old_bvid:
        return []
    data = load()
    touched = []
    hit = False

    def _merge(old, new):
        """用新音源替换旧条目，但保留旧条目的权威元数据"""
        item = dict(new)
        item['added'] = old.get('added') or _now()
        if 'played_at' in old:
            item['played_at'] = old['played_at']
        for keep in ('name', 'artist', 'album', 'source'):
            if old.get(keep):
                item[keep] = old[keep]
        return item

    for pl in data.get('playlists') or []:
        songs = pl.get('songs') or []
        changed = False
        for i, s in enumerate(songs):
            if s.get('bvid') != old_bvid:
                continue
            songs[i] = _merge(s, song)
            changed = True
        if changed:
            touched.append(pl['id'])
            hit = True

    for i, h in enumerate(data.get('history') or []):
        if h.get('bvid') == old_bvid:
            data['history'][i] = _merge(h, song)
            hit = True

    if hit:
        save()
    return touched


def move_song(pid, bvid, delta):
    """歌单内上移/下移一首歌（delta=-1 上移，+1 下移）"""
    pl = get_playlist(pid)
    if not pl:
        return False
    songs = pl.get('songs') or []
    idx = next((i for i, s in enumerate(songs) if s.get('bvid') == bvid), -1)
    if idx < 0:
        return False
    new_idx = max(0, min(len(songs) - 1, idx + delta))
    if new_idx == idx:
        return False
    songs.insert(new_idx, songs.pop(idx))
    save()
    return True


def add_history(song):
    """记一条播放历史（置顶去重，最多保留 300 条）"""
    data = load()
    hist = data.setdefault('history', [])
    bvid = song.get('bvid')
    hist = [h for h in hist if h.get('bvid') != bvid]
    item = dict(song)
    item['played_at'] = _now()
    hist.insert(0, item)
    data['history'] = hist[:300]
    save()
    return True


def clear_history():
    """清空播放历史"""
    data = load()
    data['history'] = []
    save()
    return True


def set_setting(key, value):
    """写一个设置项"""
    data = load()
    data.setdefault('settings', {})[key] = value
    save()
    return True


def get_settings():
    """读全部设置"""
    return load().get('settings', {})


def set_login(cookie, uname, mid, logged_in):
    """保存某弹幕网站登录态（cookie / 用户名 / mid）"""
    data = load()
    data['login'] = {
        'cookie': cookie or '',
        'uname': uname or '',
        'mid': str(mid or ''),
        'logged_in': bool(logged_in),
    }
    save()
    return True


def get_login():
    """读登录态"""
    return load().get('login', {
        'cookie': '',
        'uname': '',
        'mid': '',
        'logged_in': False,
    })
