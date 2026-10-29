# -*- coding: utf-8 -*-
"""B 站客户端：搜索音乐视频 + 只取音频流（DASH audio，不碰视频）。

免登录即可使用；传入登录 Cookie 可拿更高音质（Hi-Res / 杜比）。

## 改这个文件之前先读 MAINTAIN.md 第 2 节

这个文件里一半的代码是"为了让请求不被风控拦下"，另一半才是"调接口"。
看起来啰嗦的条件和退避都不是多余的，删掉任意一段都会让搜索变成"时好时坏"。

## 搜索走三条通道，按顺序降级

    1. wbi/search/type   需要 WBI 签名，字段规整        ← 主通道
    2. search/type       不需要签名，字段同构但数字是字符串 ← 风控时的第一备胎
    3. 换匿名身份，把 1、2 再试一遍                     ← 登录态被单独拦时用

三条都失败才抛 BiliError。错误文案由 `_search_error_text()` 翻成人话，不把
csrf、-352 这类术语甩给界面。

## 请求头里两个不能动的点

- `Referer` 必须是**带关键词的搜索页地址**，否则绝大多数请求只回一个 v_voucher（空结果）。
- 不往服务端传 `order` 参数（带了更容易被甩 v_voucher），排序在本地做。
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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

BASE_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# WBI 签名用的重排表（B 站前端固定表）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

AUDIO_QUALITY_NAME = {
    30216: "64K",
    30232: "132K",
    30280: "192K",
    30250: "杜比全景声",
    30251: "Hi-Res 无损",
    30380: "杜比全景声",
}

# 搜索接口只接受这几个 page_size
PAGE_SIZES = (20, 30, 50)


class BiliError(Exception):
    pass


def _log(msg):
    """排查用日志，写到 data/bili.log"""
    try:
        d = paths.data_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "bili.log"), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


COOKIE_PATH = paths.data_path("bili_cookie.json")

SEARCH_WBI_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
# 老版搜索接口，不需要 wbi 签名。主接口被风控（csrf 校验失败）时拿它兜底。
SEARCH_LEGACY_URL = "https://api.bilibili.com/x/web-interface/search/type"

# 同关键词短时间重复搜索是触发风控的主因之一，这里缓存 10 分钟。
_SEARCH_TTL = 600
_search_cache = {}
_search_cache_lock = threading.Lock()


def _cache_get(key):
    with _search_cache_lock:
        item = _search_cache.get(key)
        if not item:
            return None
        ts, results = item
        if time.time() - ts > _SEARCH_TTL:
            _search_cache.pop(key, None)
            return None
        return results


def _cache_put(key, results):
    with _search_cache_lock:
        if len(_search_cache) > 200:
            for k in sorted(_search_cache, key=lambda k: _search_cache[k][0])[:100]:
                _search_cache.pop(k, None)
        _search_cache[key] = (time.time(), results)


def _int(value):
    """老接口的 play / video_review / favorites 是字符串，直接参与排序会炸"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

# 登录态 cookie 不进匿名缓存文件，避免退出登录后被写回
_PRIVATE_COOKIE_KEYS = {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"}


def _load_cookie_file():
    try:
        with open(COOKIE_PATH, "r", encoding="utf-8") as f:
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
        with open(COOKIE_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


class BiliClient:
    def __init__(self, cookie: str = ""):
        self.session = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self._mixin_key = None
        self._mixin_key_time = 0.0
        self._lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._last_req = 0.0
        self.logged_in = False
        self.uname = ""
        self._anon_client = None
        self._init_anonymous()
        if cookie:
            self.set_login_cookie(cookie)

    # ------------------------------------------------------------------ cookie
    def _init_anonymous(self, keep_fingerprint=True):
        """伪装成真实浏览器：
        1) 先恢复上次存下来的匿名 cookie（老 cookie 信誉更好）
        2) 缺 b_nut 时访问一次首页，让服务端补全
        3) 只有在真的没有 buvid3 时才向 spi 要新指纹

        2026-10-29：原来每次启动都无条件换 buvid3/buvid4。B 站的风控会记这层设备指纹，
        一个进程每次启动都换指纹反而更像临时爬虫；登录态也是绑着登录时那枚 buvid 的。
        现在默认沿用旧指纹，只有 keep_fingerprint=False 才强制换新。
        """
        if not len(self.session.cookies):
            for k, v in (_load_cookie_file() or {}).items():
                try:
                    self.session.cookies.set(k, v, domain=".bilibili.com")
                except Exception:
                    pass
        names = {c.name for c in self.session.cookies if c.value}
        if "b_nut" not in names:
            try:
                self.session.get("https://www.bilibili.com/", timeout=10)
            except Exception:
                pass
            names = {c.name for c in self.session.cookies if c.value}
        if "buvid3" not in names or not keep_fingerprint:
            try:
                r = self.session.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=10)
                d = r.json().get("data") or {}
                if d.get("b_3"):
                    self.session.cookies.set("buvid3", d["b_3"], domain=".bilibili.com")
                if d.get("b_4"):
                    self.session.cookies.set("buvid4", d["b_4"], domain=".bilibili.com")
            except Exception:
                pass
        _save_cookie_file(self.session)

    def set_login_cookie(self, cookie: str):
        """cookie 形如 SESSDATA=xxx; bili_jct=yyy; DedeUserID=123"""
        cookie = (cookie or "").strip()
        for kv in cookie.split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                self.session.cookies.set(k.strip(), v.strip(), domain=".bilibili.com")
        self._refresh_login_state()

    def _refresh_login_state(self):
        try:
            js = self._get("https://api.bilibili.com/x/web-interface/nav")
            d = js.get("data") or {}
            self.logged_in = bool(d.get("isLogin"))
            self.uname = d.get("uname") or ""
        except Exception:
            self.logged_in = False

    def cookie_string(self):
        return "; ".join(f"{c.name}={c.value}" for c in self.session.cookies)

    # --------------------------------------------------------------------- WBI
    def _get_mixin_key(self):
        with self._lock:
            if self._mixin_key and time.time() - self._mixin_key_time < 3600:
                return self._mixin_key
            js = self._get("https://api.bilibili.com/x/web-interface/nav")
            img = (js.get("data") or {}).get("wbi_img") or {}
            img_url = img.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
            sub_url = img.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
            orig = img_url + sub_url
            if len(orig) < 64:
                raise BiliError("无法获取 WBI 密钥（网络异常？）")
            self._mixin_key = "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]
            self._mixin_key_time = time.time()
            return self._mixin_key

    def _sign(self, params: dict):
        params = dict(params or {})
        params["wts"] = int(time.time())
        clean = {}
        for k in sorted(params):
            v = params[k]
            clean[k] = "".join(ch for ch in str(v) if ch not in "!'()*")
        query = urllib.parse.urlencode(clean)
        clean["w_rid"] = hashlib.md5((query + self._get_mixin_key()).encode()).hexdigest()
        return clean

    # --------------------------------------------------------------------- HTTP
    def _get(self, url, params=None, signed=False, timeout=15, headers=None, session=None):
        sess = session or self.session
        last_err = None
        for attempt in range(3):
            try:
                p = self._sign(params) if signed else params
                r = sess.get(url, params=p, timeout=timeout, headers=headers)
                js = r.json()
                if js.get("code") in (-352, -412) and attempt < 2:
                    # 风控退避重试。这里刻意不再换 buvid：指纹前后不一致更容易继续被拦，
                    # 只把缺失的匿名 cookie 补齐。
                    if sess is self.session:
                        self._init_anonymous()
                    time.sleep(1.2 * (attempt + 1))
                    continue
                return js
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.7)
        if last_err:
            raise BiliError(f"网络请求失败：{last_err}")
        return {}

    # -------------------------------------------------------------------- 搜索
    def _throttle(self, gap=0.35):
        """全局限流：两个搜索请求之间至少间隔 gap 秒，避免被风控"""
        with self._throttle_lock:
            wait = gap - (time.time() - self._last_req)
            if wait > 0:
                time.sleep(wait)
            self._last_req = time.time()

    def _anon(self):
        """搜索用的匿名身份：不带登录 cookie。

        B 站把登录态的搜索请求判成风控（csrf 校验失败）时会连续拒同一份 cookie，
        而搜索本身不需要登录，换个干净的匿名会话往往立刻就能出结果。
        """
        with self._lock:
            if self._anon_client is None:
                self._anon_client = BiliClient()
            return self._anon_client

    def _search_wbi(self, sess, keyword, page, ps, headers):
        js = {}
        for attempt in range(4):
            self._throttle()
            js = self._get(SEARCH_WBI_URL,
                           {"search_type": "video", "keyword": keyword, "page": page,
                            "page_size": ps},
                           signed=True, headers=headers, session=sess)
            if js.get("code") != 0:
                break
            data = js.get("data") or {}
            if data.get("result"):
                break
            # 还是空 = 被风控了：先像个真人一样打开一次搜索页，再温和地重试
            if attempt < 3:
                _log(f"搜索 '{keyword}' 第 {attempt + 1} 次返回空，"
                     f"data keys={list(data.keys())}，模拟打开搜索页后重试")
                try:
                    sess.get("https://search.bilibili.com/video",
                             params={"keyword": keyword}, timeout=12, headers=headers)
                except Exception:
                    pass
                time.sleep(0.8 + 0.6 * attempt)
                if sess is self.session:
                    self._init_anonymous()
                continue
            _log(f"搜索 '{keyword}' 连续 4 次返回空，放弃")
            break
        return js

    def _search_legacy(self, sess, keyword, page, headers):
        """老版搜索接口，不需要 wbi 签名。主接口被风控时用它兜底。"""
        self._throttle()
        js = self._get(SEARCH_LEGACY_URL,
                       {"search_type": "video", "keyword": keyword, "page": page},
                       headers=headers, session=sess)
        if js.get("code") == 0:
            _log(f"搜索 '{keyword}' 改走老接口成功")
        return js

    def search(self, keyword, page=1, order="totalrank", page_size=30):
        """order: totalrank 综合 / click 播放多 / pubdate 最新 / dm 弹幕多 / stow 收藏多

        注意：实测 B 站 wbi 搜索接口只要带上 order 参数就会返回 v_voucher 风控凭据
        并给出空结果，所以这里不传 order，改为拿到结果后在本地排序。

        2026-10-29：主接口被风控（返回 csrf 校验失败 / -111）时不再直接报错，
        依次降级：老接口 → 匿名身份再试。全挂才提示用户。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        try:
            ps = int(page_size)
        except (TypeError, ValueError):
            ps = 30
        if ps not in PAGE_SIZES:
            ps = min(PAGE_SIZES, key=lambda v: abs(v - ps))
        page = int(page or 1)

        cache_key = (keyword, page, ps)
        cached = _cache_get(cache_key)
        if cached is not None:
            return _sort_results([dict(r) for r in cached], order)

        # 关键：Referer 必须是带关键词的真实搜索页地址。
        # 实测用 www.bilibili.com 或不带关键词的地址，绝大多数请求会被
        # 风控拦下（只回一个 v_voucher、结果为空）；换成这个后基本 100% 成功。
        search_headers = {
            "Referer": "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(keyword),
        }

        js = self._search_wbi(self.session, keyword, page, ps, search_headers)
        if js.get("code") != 0:
            _log(f"搜索 '{keyword}' 主接口失败 code={js.get('code')} "
                 f"msg={js.get('message')} logged_in={self.logged_in}，尝试降级")
            alt = self._search_legacy(self.session, keyword, page, search_headers)
            if alt.get("code") == 0:
                js = alt
            else:
                anon = self._anon()
                js = anon._search_wbi(anon.session, keyword, page, ps, search_headers)
                if js.get("code") != 0:
                    js = anon._search_legacy(anon.session, keyword, page, search_headers)
                if js.get("code") != 0:
                    _log(f"搜索 '{keyword}' 全部通道失败 code={js.get('code')} "
                         f"msg={js.get('message')} logged_in={self.logged_in}")
                    raise BiliError(_search_error_text(js))

        results = _parse_results(js)
        if results:
            _cache_put(cache_key, results)
        return _sort_results(results, order)

    # ------------------------------------------------------- 视频详情 / 音频流
    def get_cid(self, bvid):
        js = self._get("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})
        if js.get("code") != 0:
            raise BiliError(f"获取视频信息失败：{js.get('message')}")
        return (js.get("data") or {}).get("cid")

    def get_video(self, bvid, p=1):
        """按 BV 号取单条视频详情，返回与 search() 同构的 song dict。

        手机版 importLink 只写了个占位标题「BV 导入曲目」（因为它那边拿详情要多一次
        请求、当时图省事）。桌面版顺手把标题/UP主/时长/封面都取回来，导入后列表里
        显示的就是真实信息，不用再手动改。

        p 是多 P 视频的分 P 序号（从 1 开始），对应 ?p=N；单 P 视频忽略。
        """
        js = self._get("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})
        if js.get("code") != 0:
            raise BiliError(f"获取视频信息失败：{js.get('message')}")
        d = js.get("data") or {}
        if not d.get("bvid"):
            raise BiliError("这个 BV 号没有对应的视频")
        pages = d.get("pages") or []
        idx = max(1, int(p or 1))
        page = None
        for pg in pages:
            if int(pg.get("page") or 0) == idx:
                page = pg
                break
        if page is None and pages:
            # 分 P 超范围时退回第一个分 P，别直接失败
            page = pages[0]
        cid = (page or {}).get("cid") or d.get("cid")
        dur = (page or {}).get("duration") or d.get("duration") or 0
        title = d.get("title") or ""
        # 多 P 视频把分 P 名拼上，避免整份歌单里出现一堆同名条目
        if page and len(pages) > 1 and page.get("part"):
            part = str(page.get("part"))
            if part and part != title:
                title = f"{title} - P{page.get('page')} {part}"
        owner = (d.get("owner") or {})
        return {
            "bvid": d.get("bvid"),
            "cid": int(cid or 0),
            "title": clean_html(title),
            "name": clean_html(title),
            "artist": owner.get("name") or "",
            "mid": owner.get("mid"),
            "duration": parse_duration(dur),
            "duration_text": fmt_duration(parse_duration(dur)),
            "play": ((d.get("stat") or {}).get("view") or 0),
            "danmaku": ((d.get("stat") or {}).get("danmaku") or 0),
            "favorites": ((d.get("stat") or {}).get("favorite") or 0),
            "pubdate": d.get("pubdate") or 0,
            "cover": normalize_cover(d.get("pic") or ""),
            "description": clean_html(d.get("desc") or ""),
            "source": "bili",
        }

    def get_audio_stream(self, bvid, cid=None):
        """只取 DASH 里的纯音频流，返回 {url, backup, quality, quality_id, codec}"""
        if not cid:
            cid = self.get_cid(bvid)
        if not cid:
            raise BiliError("拿不到 cid")
        js = self._get(
            "https://api.bilibili.com/x/player/playurl",
            {"bvid": bvid, "cid": cid, "fnval": 4048, "fnver": 0, "fourk": 1},
        )
        if js.get("code") != 0:
            raise BiliError(f"取音频流失败：{js.get('message')}")
        data = js.get("data") or {}
        dash = data.get("dash") or {}
        audios = dash.get("audio") or []
        if not audios:
            raise BiliError("这个视频没有可用的音频流")
        # 音质越高 id 越大（30216 < 30232 < 30280 < 30250/30251）
        best = max(audios, key=lambda a: a.get("id", 0))
        qid = best.get("id")
        return {
            "url": best.get("baseUrl") or best.get("base_url") or "",
            "backup": best.get("backupUrl") or best.get("backup_url") or [],
            "quality_id": qid,
            "quality": AUDIO_QUALITY_NAME.get(qid, str(qid)),
            "codec": best.get("codecs") or "",
            "cid": cid,
        }


# ----------------------------------------------------------------- 工具函数
_EM = re.compile(r"</?em[^>]*>")
_TAG = re.compile(r"<[^>]+>")


def _parse_results(js):
    results = []
    for it in ((js.get("data") or {}).get("result") or []):
        bvid = it.get("bvid")
        if not bvid:
            continue
        results.append({
            "bvid": bvid,
            "title": clean_html(it.get("title") or ""),
            "author": it.get("author") or "",
            "mid": it.get("mid"),
            "duration": parse_duration(it.get("duration")),
            "duration_text": str(it.get("duration") or ""),
            "play": _int(it.get("play")),
            "danmaku": _int(it.get("video_review")),
            "favorites": _int(it.get("favorites")),
            "pubdate": _int(it.get("pubdate")),
            "cover": normalize_cover(it.get("pic")),
            "description": clean_html(it.get("description") or ""),
        })
    return results


def _sort_results(results, order):
    # 本地排序（等价于 B 站服务端排序，且不会触发风控）
    if order == "click":
        results.sort(key=lambda r: -(r.get("play") or 0))
    elif order == "pubdate":
        results.sort(key=lambda r: -(r.get("pubdate") or 0))
    elif order == "stow":
        results.sort(key=lambda r: -(r.get("favorites") or 0))
    elif order == "dm":
        results.sort(key=lambda r: -(r.get("danmaku") or 0))
    return results


def _search_error_text(js):
    """把 B 站的错误码翻译成用户能看懂的话；csrf/风控类不把术语甩给用户。"""
    code = js.get("code")
    msg = str(js.get("message") or "")
    if code in (-111, -352, -412) or "csrf" in msg.lower() or "风控" in msg:
        return "搜索失败：B 站风控拦截了这次搜索，等 1~2 分钟再试（连续搜太快也会触发）"
    return f"搜索失败：{msg or code}"


def clean_html(text: str) -> str:
    """去掉 <em> 高亮标签和所有标签，并把 &#x27; &amp; 之类的 HTML 实体还原成真字符。
    不还原的话标题既难看，也会让歌名匹配算法判错。"""
    text = _EM.sub("", text or "")
    text = _TAG.sub("", text)
    return _html.unescape(text).strip()


def build_search_query(title: str, artist: str = "") -> str:
    """把「歌名 + 歌手」拼成 B 站搜索关键词。

    两个实测坑：
    1. 歌名里的撇号（It's / Don't）会让 B 站搜不到，要去掉；
    2. QQ 音乐返回的歌手常是「高橋あず美、Lotus Juice」这种多人串，全塞进去反而搜不准，
       只取第一位歌手。
    """
    t = title or ""
    t = re.sub(r"['’‘\"“”`]", " ", t)
    t = re.sub(r"[（）()\[\]【】{}<>《》]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    a = artist or ""
    a = re.split(r"[、,，/&＆|；;]", a)[0].strip()
    a = re.sub(r"\s*(?:feat|ft|with)\.?\s*.*$", "", a, flags=re.I)
    a = re.sub(r"['’‘\"“”`]", " ", a).strip()

    return f"{t} {a}".strip()


def normalize_cover(pic: str) -> str:
    if not pic:
        return ""
    pic = pic.strip()
    if pic.startswith("//"):
        pic = "https:" + pic
    elif pic.startswith("http://"):
        pic = "https://" + pic[7:]
    if "@" not in pic:
        pic += "@336w_190h_1c.webp"
    return pic


def parse_duration(value) -> int:
    """'4:30' 或 270 或 '1:02:03' -> 秒"""
    if isinstance(value, (int, float)):
        return int(value)
    if not value:
        return 0
    parts = str(value).split(":")
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
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
