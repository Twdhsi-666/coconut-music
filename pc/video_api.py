# -*- coding: utf-8 -*-
"""
某弹幕网站客户端：搜索音乐视频 + 只取音频流（DASH audio，不碰视频）
免登录即可使用；可选传入登录 Cookie 以获得更高音质（Hi-Res / 杜比）
"""
import hashlib
import html as _html
import json
import os

import paths
import re
import threading
import time
import urllib.parse

import requests

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'


BASE_HEADERS = {
    'User-Agent': UA,
    'Referer': 'https://www.example.com/',
    'Origin': 'https://www.example.com',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}


MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

AUDIO_QUALITY_NAME = {
    30216: '64K',
    30232: '132K',
    30280: '192K',
    30250: '杜比全景声',
    30251: 'Hi-Res 无损',
     30380: '杜比全景声',
}


PAGE_SIZES = (20, 30, 50)


class VideoApiError(Exception):
    pass


def _log(msg):
    """排查用日志，写到 data/video_api.log"""
    try:
        d = paths.data_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'video_api.log'), 'a', encoding='utf-8') as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


COOKIE_PATH = paths.data_path('video_cookie.json')


_PRIVATE_COOKIE_KEYS = {'DedeUserID', 'DedeUserID__ckMd5', 'sid', 'SESSDATA', 'bili_jct'}


def _load_cookie_file():
    try:
        with open(COOKIE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cookie_file(session):
    """把匿名 cookie 存下来复用：带历史的 cookie 明显不容易被风控"""
    try:
        d = {c.name: c.value for c in session.cookies
             if c.name and c.name not in _PRIVATE_COOKIE_KEYS and c.value}
        if not d:
            return
        os.makedirs(os.path.dirname(COOKIE_PATH), exist_ok=True)
        with open(COOKIE_PATH, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


class VideoApiClient:
    def __init__(self, cookie: str = ''):
        self.session = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self._mixin_key = None
        self._mixin_key_time = 0.0
        self._lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._last_req = 0.0
        self.logged_in = False
        self.uname = ''
        self._init_anonymous()
        if cookie:
            self.set_login_cookie(cookie)

    def _init_anonymous(self):
        """伪装成真实浏览器：
        1) 先恢复上次存下来的匿名 cookie（老 cookie 信誉更好）
        2) 访问一次首页，让服务端补全 b_nut / _uuid 等
        3) 再补 buvid3 / buvid4，并落盘
        实测只拿 buvid 时，前几个搜索请求很容易被风控返回空。
        """
        if not len(self.session.cookies):
            for k, v in (_load_cookie_file() or {}).items():
                try:
                    self.session.cookies.set(k, v, domain='.example.com')
                except Exception:
                    continue
        try:
            self.session.get('https://www.example.com/', timeout=10)
        except Exception:
            pass
        try:
            r = self.session.get('https://api.example.com/x/frontend/finger/spi', timeout=10)
            d = r.json().get('data') or {}
            if d.get('b_3'):
                self.session.cookies.set('buvid3', d['b_3'], domain='.example.com')
            if d.get('b_4'):
                self.session.cookies.set('buvid4', d['b_4'], domain='.example.com')
        except Exception:
            pass
        _save_cookie_file(self.session)

    def set_login_cookie(self, cookie: str):
        """cookie 形如 SESSDATA=xxx; bili_jct=yyy; DedeUserID=123"""
        cookie = (cookie or '').strip()
        for kv in cookie.split(';'):
            if '=' in kv:
                k, v = kv.split('=', 1)
                self.session.cookies.set(k.strip(), v.strip(), domain='.example.com')
        self._refresh_login_state()

    def _refresh_login_state(self):
        try:
            js = self._get('https://api.example.com/x/web-interface/nav')
            d = js.get('data') or {}
            self.logged_in = bool(d.get('isLogin'))
            self.uname = d.get('uname') or ''
        except Exception:
            self.logged_in = False

    def cookie_string(self):
        return '; '.join(f'{c.name}={c.value}' for c in self.session.cookies)

    def _get_mixin_key(self):
        with self._lock:
            if self._mixin_key and time.time() - self._mixin_key_time < 3600:
                return self._mixin_key
            js = self._get('https://api.example.com/x/web-interface/nav')
            img = (js.get('data') or {}).get('wbi_img') or {}
            img_url = img.get('img_url', '').rsplit('/', 1)[-1].split('.')[0]
            sub_url = img.get('sub_url', '').rsplit('/', 1)[-1].split('.')[0]
            orig = img_url + sub_url
            if len(orig) < 64:
                raise VideoApiError('无法获取 WBI 密钥（网络异常？）')
            self._mixin_key = ''.join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]
            self._mixin_key_time = time.time()
            return self._mixin_key

    def _sign(self, params: dict):
        params = dict(params or {})
        params['wts'] = int(time.time())
        clean = {}
        for k in sorted(params):
            v = params[k]
            clean[k] = ''.join(ch for ch in str(v) if ch not in "!'()*")
        query = urllib.parse.urlencode(clean)
        clean['w_rid'] = hashlib.md5((query + self._get_mixin_key()).encode()).hexdigest()
        return clean

    def _get(self, url, params=None, signed=False, timeout=15, headers=None):
        last_err = None
        for attempt in range(3):
            try:
                p = self._sign(params) if signed else params
                r = self.session.get(url, params=p, timeout=timeout, headers=headers)
                js = r.json()
                if js.get('code') in (-352, -412):
                    self._init_anonymous()
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return js
            except Exception as e:
                last_err = e
                time.sleep(0.7)
        if last_err:
            raise VideoApiError(f'网络请求失败：{last_err}')
        return {}

    def _throttle(self, gap=0.35):
        """全局限流：两个搜索请求之间至少间隔 gap 秒，避免被风控"""
        with self._throttle_lock:
            wait = gap - (time.time() - self._last_req)
            if wait > 0:
                time.sleep(wait)
            self._last_req = time.time()

    def search(self, keyword, page=1, order='totalrank', page_size=30):
        """order: totalrank 综合 / click 播放多 / pubdate 最新 / dm 弹幕多 / stow 收藏多

        注意：实测某弹幕网站 wbi 搜索接口只要带上 order 参数就会返回 v_voucher 风控凭据
        并给出空结果，所以这里不传 order，改为拿到结果后在本地排序。
        """
        keyword = (keyword or '').strip()
        if not keyword:
            return []
        try:
            ps = int(page_size)
        except (TypeError, ValueError):
            ps = 30
        if ps not in PAGE_SIZES:
            ps = min(PAGE_SIZES, key=lambda v: abs(v - ps))

        js = {}

        search_headers = {
            'Referer': 'https://search.example.com/all?keyword='
                       + urllib.parse.quote(keyword),
        }

        for attempt in range(4):
            self._throttle()
            js = self._get(
                'https://api.example.com/x/web-interface/wbi/search/type',
                {'search_type': 'video', 'keyword': keyword, 'page': page,
                 'page_size': ps},
                signed=True,
                headers=search_headers,
            )
            if js.get('code') != 0:
                break
            data = js.get('data') or {}
            if data.get('result'):
                break

            if attempt < 3:
                _log(f"搜索 '{keyword}' 第 {attempt + 1} 次返回空，"
                     f"data keys={list(data.keys())}，模拟打开搜索页后重试")
                try:
                    self.session.get(
                        'https://search.example.com/video',
                        params={'keyword': keyword},
                        timeout=12,
                        headers=search_headers,
                    )
                except Exception:
                    pass
                time.sleep(0.4 + 0.5 * attempt)
                self._init_anonymous()
                continue
            _log(f"搜索 '{keyword}' 连续 4 次返回空，放弃")
            break

        if js.get('code') != 0:
            raise VideoApiError(f"搜索失败：{js.get('message') or js.get('code')}")

        results = []
        for it in (js.get('data') or {}).get('result') or []:
            bvid = it.get('bvid')
            if not bvid:
                continue
            results.append({
                'bvid': bvid,
                'title': clean_html(it.get('title') or ''),
                'author': it.get('author') or '',
                'mid': it.get('mid'),
                'duration': parse_duration(it.get('duration')),
                'duration_text': it.get('duration') or '',
                'play': it.get('play') or 0,
                'danmaku': it.get('video_review') or 0,
                'favorites': it.get('favorites') or 0,
                'pubdate': it.get('pubdate') or 0,
                'cover': normalize_cover(it.get('pic')),
                'description': clean_html(it.get('description') or ''),
            })

        if order == 'click':
            results.sort(key=lambda r: -(r.get('play') or 0))
        elif order == 'pubdate':
            results.sort(key=lambda r: -(r.get('pubdate') or 0))
        elif order == 'stow':
            results.sort(key=lambda r: -(r.get('favorites') or 0))
        elif order == 'dm':
            results.sort(key=lambda r: -(r.get('danmaku') or 0))
        return results

    def get_cid(self, bvid):
        js = self._get('https://api.example.com/x/web-interface/view', {'bvid': bvid})
        if js.get('code') != 0:
            raise VideoApiError(f'获取视频信息失败：{js.get("message")}')
        return (js.get('data') or {}).get('cid')

    def get_video(self, bvid, p=1):
        """按 BV 号取单条视频详情，返回与 search() 同构的 song dict。

        手机版 importLink 只写了个占位标题「BV 导入曲目」（因为它那边拿详情要多一次
        请求、当时图省事）。桌面版顺手把标题/UP主/时长/封面都取回来，导入后列表里
        显示的就是真实信息，不用再手动改。

        p 是多 P 视频的分 P 序号（从 1 开始），对应 ?p=N；单 P 视频忽略。
        """
        js = self._get('https://api.example.com/x/web-interface/view', {'bvid': bvid})
        if js.get('code') != 0:
            raise VideoApiError(f'获取视频信息失败：{js.get("message")}')
        d = js.get('data') or {}
        if not d.get('bvid'):
            raise VideoApiError('这个 BV 号没有对应的视频')
        pages = d.get('pages') or []
        idx = max(1, int(p or 1))
        page = None
        for pg in pages:
            if int(pg.get('page') or 0) == idx:
                page = pg
                break
        if page is None and pages:
            page = pages[0]
        cid = (page or {}).get('cid') or d.get('cid')
        dur = (page or {}).get('duration') or d.get('duration') or 0
        title = d.get('title') or ''
        if page and len(pages) > 1 and page.get('part'):
            part = str(page.get('part'))
            if part and part != title:
                title = f'{title} - P{page.get("page")} {part}'
        owner = d.get('owner') or {}
        return {
            'bvid': d.get('bvid'),
            'cid': int(cid or 0),
            'title': clean_html(title),
            'name': clean_html(title),
            'artist': owner.get('name') or '',
            'mid': owner.get('mid'),
            'duration': parse_duration(dur),
            'duration_text': fmt_duration(parse_duration(dur)),
            'play': (d.get('stat') or {}).get('view') or 0,
            'danmaku': (d.get('stat') or {}).get('danmaku') or 0,
            'favorites': (d.get('stat') or {}).get('favorite') or 0,
            'pubdate': d.get('pubdate') or 0,
            'cover': normalize_cover(d.get('pic') or ''),
            'description': clean_html(d.get('desc') or ''),
            'source': 'video',
        }

    def get_audio_stream(self, bvid, cid=None):
        """只取 DASH 里的纯音频流，返回 {url, backup, quality, quality_id, codec}"""
        if not cid:
            cid = self.get_cid(bvid)
        if not cid:
            raise VideoApiError('拿不到 cid')
        js = self._get(
            'https://api.example.com/x/player/playurl',
            {'bvid': bvid, 'cid': cid, 'fnval': 4048, 'fnver': 0, 'fourk': 1},
        )
        if js.get('code') != 0:
            raise VideoApiError(f'取音频流失败：{js.get("message")}')
        data = js.get('data') or {}
        dash = data.get('dash') or {}
        audios = dash.get('audio') or []
        if not audios:
            raise VideoApiError('这个视频没有可用的音频流')
        best = max(audios, key=lambda a: a.get('id', 0))
        qid = best.get('id')
        return {
            'url': best.get('baseUrl') or best.get('base_url') or '',
            'backup': best.get('backupUrl') or best.get('backup_url') or [],
            'quality_id': qid,
            'quality': AUDIO_QUALITY_NAME.get(qid, str(qid)),
            'codec': best.get('codecs') or '',
            'cid': cid,
        }


_EM = re.compile(r'</?em[^>]*>')
_TAG = re.compile(r'<[^>]+>')


def clean_html(text: str) -> str:
    """去掉 <em> 高亮标签和所有标签，并把 &#x27; &amp; 之类的 HTML 实体还原成真字符。
    不还原的话标题既难看，也会让歌名匹配算法判错。"""
    text = _EM.sub('', text or '')
    text = _TAG.sub('', text)
    return _html.unescape(text).strip()


def build_search_query(title: str, artist: str = '') -> str:
    """把「歌名 + 歌手」拼成某弹幕网站搜索关键词。

    两个实测坑：
    1. 歌名里的撇号（It's / Don't）会让某弹幕网站搜不到，要去掉；
    2. 某音乐平台返回的歌手常是「高橋あず美、Lotus Juice」这种多人串，全塞进去反而搜不准，
       只取第一位歌手。
    """
    t = title or ''
    t = re.sub(r"['’‘\"“”`]", ' ', t)
    t = re.sub(r'[（）()\[\]【】{}<>《》]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()

    a = artist or ''
    a = re.split(r'[、,，/&＆|；;]', a)[0].strip()
    a = re.sub(r'\s*(?:feat|ft|with)\.?\s*.*$', '', a, flags=re.I)
    a = re.sub(r"['’‘\"“”`]", ' ', a).strip()

    return f'{t} {a}'.strip()


def normalize_cover(pic: str) -> str:
    if not pic:
        return ''
    pic = pic.strip()
    if pic.startswith('//'):
        pic = 'https:' + pic
    elif pic.startswith('http://'):
        pic = 'https://' + pic[7:]
    if '@' not in pic:
        pic += '@336w_190h_1c.webp'
    return pic


def parse_duration(value) -> int:
    """'4:30' 或 270 或 '1:02:03' -> 秒"""
    if isinstance(value, (int, float)):
        return int(value)
    if not value:
        return 0
    parts = str(value).split(':')
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def fmt_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m:02d}:{s:02d}'
