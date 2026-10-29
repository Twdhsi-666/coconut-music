# -*- coding: utf-8 -*-
"""
椰子音乐 · 本地后端服务
- 只依赖标准库 HTTPServer
- B 站搜索 / 单曲匹配 / 纯音频流代理（支持拖动进度）
- QQ 音乐歌单解析
- 二维码登录
"""
import base64
import io
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import bili as bili_mod
import downloader
import paths
import hotkeys
import qqmusic
import linkparse
import share
import store

BASE_DIR = paths.app_dir()
WEB_DIR = paths.res_path("web")

UA = bili_mod.UA
BILI_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

_lock = threading.RLock()
_stream_cache = {}      # key -> {url, backup, ts, quality}
_stream_ttl = 3000      # 音频地址有效期（秒），B 站约 2 小时


# --------------------------------------------------------------------- client
_client = None


def client():
    global _client
    with _lock:
        if _client is None:
            login = store.get_login()
            _client = bili_mod.BiliClient(login.get("cookie") or "")
        return _client


def refresh_login():
    global _client
    with _lock:
        login = store.get_login()
        _client = bili_mod.BiliClient(login.get("cookie") or "")
        c = _client
    if login.get("logged_in"):
        try:
            store.set_login(login.get("cookie", ""), c.uname, "", c.logged_in)
        except Exception:
            pass
    return _client


# ------------------------------------------------------------------ 文本归一
_PUNCT = re.compile(r"[\s\-_—–·・,，。.、!！?？'\"“”‘’()（）\[\]【】《》<>/\\|~@#$%^&*+=:;；：]+")


def norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\b(feat|ft|featuring|official|mv|lyric|lyrics|ver|version|remaster(ed)?)\b.*$", " ", text)
    return _PUNCT.sub("", text).strip()


_DECOR = re.compile(r"[【\[（(〈《「『][^】\])）〉》」』]*[】\])）〉》」』]")


def strip_decor(text: str) -> str:
    """剔掉标题上的装饰段：【碧蓝档案|TH Lofi】、(Live)、「完整版」这类。

    B 站上传者爱在标题前挂方括号台标，原来的 norm 会把它连字一起留下，
    结果「整串包含」判断对不上 —— 实测过一条时长只差 1 秒的正确结果就是这么被刷掉的。
    """
    return _DECOR.sub(" ", text or "")


def core_norm(text: str) -> str:
    return norm(strip_decor(text))


def wordset(text: str) -> set:
    """把标题拆成词集合，用来做「词序不同也算命中」的比对。"""
    s = strip_decor(text or "").lower()
    s = re.sub(r"(feat|ft|featuring|official|mv|lyric|lyrics|ver|version|remaster(ed)?).*$", " ", s)
    return {w for w in re.split(r"[^\w]+", s) if w}


BAD_WORDS = ["伴奏", "消音", "翻唱", "教程", "教学", "钢琴", "吉他", "尤克里里", "口琴", "手风琴",
             "小提琴", "架子鼓", "和弦", "串烧", "合集", "盘点", "reaction", "鬼畜", "恶搞",
             "搞笑", "舞蹈", "混剪", "切片", "解说", "评测", "reaction", "dj版", "remix", "八音盒",
             "纯音乐", "配乐", "bgm合集", "铃声", "dj", "教程向"]
GOOD_WORDS = ["无损", "hi-res", "hires", "高音质", "音质", "完整版", "官方", "原唱", "动态歌词",
              "歌词", "动态", "full", "audio", "纯享"]


def score_result(r, title, artist, duration):
    s = 0.0
    nt = norm(title)
    rt = norm(r.get("title", ""))
    ra = norm(r.get("author", ""))
    raw = (r.get("title") or "").lower()

    if nt and rt:
        if nt == rt:
            s += 120
        elif nt in rt:
            s += 70
        else:
            import difflib
            ratio = difflib.SequenceMatcher(None, nt, rt).ratio()
            s += ratio * 45
    na = norm(artist)
    if na and (na in ra or na in rt or ra in na):
        s += 28

    d = r.get("duration") or 0
    if duration and d:
        diff = abs(d - duration)
        if diff <= 4:
            s += 34
        elif diff <= 12:
            s += 24
        elif diff <= 25:
            s += 12
        elif diff <= 45:
            s += 3
        elif diff > 90:
            s -= 26

    for w in BAD_WORDS:
        if w in raw:
            s -= 20
    for w in GOOD_WORDS:
        if w in raw:
            s += 7

    s += min(14.0, (float(r.get("play") or 0) + 1) ** 0.22)
    return s


# ------------------------------------------------ 自动匹配音源（懒匹配）
# 判据分三档，越像越信；时长只在「标题不是完全一样」时才当门槛用。
AUTO_MATCH_DIFF_CONTAINED = 90      # 标题互相包含时，时长差的天花板（秒）。游戏原声在 B 站常是加长/剪辑版，45 秒会整批误杀
AUTO_MATCH_DIFF_MIN = 45            # 但短歌不能照抄这个天花板：57 秒的《Hawaii》会撞上 108 秒的《Hawaii Five-O》
AUTO_MATCH_DIFF_RATIO = 0.35        # 额度按歌长给（时长 × 比例），下限 45 秒、上限 90 秒
DUR_RATIO_MIN = 0.6                 # 结果时长不能短于歌长的 60%（32 秒的夏威夷旅行 vlog 就是这么混进来的）
DUR_RATIO_MAX = 1.8                 # 也不能长到 1.8 倍以上（那种基本是整轨/合集）
MIN_SUBSTR_LEN = 4                  # B 站标题短于 4 个字符时，不认「它是歌名的一小截」这种命中
AUTO_MATCH_DIFF_FUZZY = 15          # 只靠模糊相似时，允许的时长差（秒）
AUTO_MATCH_MIN_RATIO = 0.75         # 模糊相似度的下限


def dur_gate(duration):
    """这首歌的时长差能放到几秒：短歌收紧，长歌（原声整轨、剪辑版常见）才放到 90 秒。"""
    if not duration:
        return AUTO_MATCH_DIFF_CONTAINED
    return max(AUTO_MATCH_DIFF_MIN, min(AUTO_MATCH_DIFF_CONTAINED, duration * AUTO_MATCH_DIFF_RATIO))


def auto_pick_source(results, title, artist, duration):
    """从搜索结果里挑一个「敢直接播」的音源；挑不到返回 None。

    为什么不用 score_result 的总分排序取第一条（这是实测踩出来的）：
    对「纯音乐之神」歌单里 22 首真实条目跑过一遍，总分里掺了播放量、
    GOOD_WORDS 加分这些噪声，而且时长差 >90s 会一次性扣 26 分，于是出现

        标题完全命中（正确）   Snowlight Serenade → 49.6
        标题完全命中（正确）   そこにいるのは誰?  → 56.7
        标题根本没命中（错误） muscle blues      → 54.0   ← 反而更高

    也就是说**任何单一分数阈值都必然误杀正确项、误收错误项**。所以这里拆成两路：
      ① 标题结构：归一化后「完全相等」> 「互相包含」> 「模糊相似」
      ② 时长：两边都知道时长时，差值要够小
    规则：
      完全相等          → 直接信（标题一字不差，时长差多少都认）
      互相包含（含词集合互相包含）→ 时长差 ≤ dur_gate(时长)，或时长拿不到
      仅模糊相似        → 必须时长差 ≤ AUTO_MATCH_DIFF_FUZZY

    宁缺毋滥：匹配错了用户听到的是别人的歌，比「没匹配到、让用户点 ⇄ 手动选」
    糟糕得多。挑不到就把话说明白，让用户自己决定。
    """
    nt = core_norm(title)
    ntok = wordset(title)
    na = norm(artist)
    if not nt:
        return None

    import difflib
    best = None                      # ((rank, -时长差, 总分), 结果)
    for r in (results or []):
        if not isinstance(r, dict) or not r.get("bvid"):
            continue
        rt = core_norm(r.get("title") or "")
        if not rt:
            continue
        rtok = wordset(r.get("title") or "")

        if nt == rt:
            rank = 3
        elif rt in nt and len(rt) < MIN_SUBSTR_LEN:
            # B 站标题只是歌名里的一小截，不算命中（实测「奴隷」抢走了《王と王妃と奴隷》）
            continue
        elif nt in rt or rt in nt:
            rank = 2
        elif len(ntok & rtok) >= 2 and (ntok <= rtok or rtok <= ntok):
            # 词全对得上、只是顺序不同或被装饰段隔开，一样算「互相包含」这一档。
            # 至少两个词重合才认：单词对单词太容易撞名（实测《Hawaii》匹到《Hawaii Five-O》）。
            rank = 2
        else:
            if difflib.SequenceMatcher(None, nt, rt).ratio() < AUTO_MATCH_MIN_RATIO:
                continue
            rank = 1

        # 纯英文单词歌名（Hawaii、End、Peace 这种）撞名概率最高：必须歌手也对得上才认。
        # 中日文歌名整串才算一个词（「初夏雨后」「疑惑」），歧义小得多，不走这条 ——
        # 否则这张 QQ 歌单一半的歌都白搜了。实测不卡这条时，两条夏威夷旅行视频
        # 会冒充《Hawaii》的音源被播出来。
        if rank == 2 and len(ntok) == 1:
            only = next(iter(ntok))
            if only.isascii() and only.isalnum():
                ra = norm(r.get("author") or "")
                if not (na and (na in ra or na in rt)):
                    continue

        d = int(r.get("duration") or 0)
        diff = abs(d - duration) if (duration and d) else None
        if rank == 2 and diff is not None:
            # 绝对秒差和相对长度两头都要卡：只看秒差会放进 32 秒的旅行 vlog，
            # 只看比例会放进只差几十秒的长合集，两个都误放过一次。
            if diff > dur_gate(duration):
                continue
            if not (DUR_RATIO_MIN * duration <= d <= DUR_RATIO_MAX * duration):
                continue
        if rank == 1 and (diff is None or diff > AUTO_MATCH_DIFF_FUZZY):
            continue

        key = (rank, -(diff if diff is not None else 999), score_result(r, title, artist, duration))
        if best is None or key > best[0]:
            best = (key, r)
    return best[1] if best else None


def build_matched_song(pick, title, artist):
    """把一条 B 站搜索结果变成可入库的歌。

    歌名/歌手沿用 QQ 那边带过来的（它们才是这首歌的身份），B 站标题只作为
    title 字段留着，方便用户在换源弹窗里核对到底匹配到了哪条视频。
    cid 刻意留空：搜索结果里没有 cid，取流时会由 get_stream 解析出来，
    前端拿到后再回填（见 app.js 里 qualityTag 那段）。
    """
    return {
        "bvid": pick["bvid"],
        "cid": 0,
        "title": pick.get("title") or title or "",
        "name": title or pick.get("title") or "",
        "artist": artist or pick.get("author") or "",
        "duration": int(pick.get("duration") or 0),
        "cover": pick.get("cover") or "",
        "play": pick.get("play") or 0,
        "source": "bili",
    }


# ------------------------------------------------------------------ 二维码登录
def qr_generate():
    r = requests.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                     headers=BILI_HEADERS, timeout=15)
    d = (r.json().get("data") or {})
    url, key = d.get("url"), d.get("qrcode_key")
    if not url or not key:
        raise RuntimeError("二维码获取失败")
    # 用 SVG 后端生成二维码：纯 Python 实现，不需要 Pillow（少一个依赖，便携包能小一圈）
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"url": url, "key": key, "qr": "data:image/svg+xml;base64," + b64}


def qr_poll(key):
    r = requests.get("https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                     params={"qrcode_key": key}, headers=BILI_HEADERS, timeout=15)
    js = r.json()
    d = js.get("data") or {}
    code = d.get("code")
    if code != 0:
        return {"status": code, "logged_in": False}
    cookies = {c.name: c.value for c in r.cookies}
    q = urllib.parse.urlparse(d.get("url") or "")
    for k, v in urllib.parse.parse_qsl(q.query):
        cookies.setdefault(k, v)
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v)
    c = refresh_with_cookie(cookie_str)
    store.set_login(cookie_str, c.uname, cookies.get("DedeUserID", ""), c.logged_in)
    return {"status": 0, "logged_in": c.logged_in, "uname": c.uname}


def refresh_with_cookie(cookie_str):
    global _client
    with _lock:
        _client = bili_mod.BiliClient(cookie_str)
        return _client


# ------------------------------------------------------------------ 音频流
def get_stream(bvid, cid=None, force=False):
    key = f"{bvid}|{cid or ''}"
    now = time.time()
    with _lock:
        ent = _stream_cache.get(key)
        if ent and not force and now - ent["ts"] < _stream_ttl:
            return ent
    c = client()
    st = c.get_audio_stream(bvid, cid)
    ent = {"url": st["url"], "backup": st.get("backup") or [], "ts": now,
           "quality": st.get("quality"), "quality_id": st.get("quality_id"),
           "cid": st.get("cid"), "codec": st.get("codec")}
    with _lock:
        _stream_cache[key] = ent
    return ent


def proxy_audio(handler, bvid, cid=None):
    ent = get_stream(bvid, cid)
    for attempt in (0, 1):
        urls = [ent["url"]] + list(ent.get("backup") or [])
        if attempt == 1:
            ent = get_stream(bvid, cid, force=True)
            urls = [ent["url"]] + list(ent.get("backup") or [])
        for u in urls:
            if not u:
                continue
            headers = dict(BILI_HEADERS)
            rng = handler.headers.get("Range")
            if rng:
                headers["Range"] = rng
            try:
                r = requests.get(u, headers=headers, stream=True, timeout=25)
            except Exception:
                continue
            if r.status_code not in (200, 206):
                r.close()
                continue
            handler.send_response(r.status_code)
            # 刻意不声明 audio/* 类型：下载管理器（NDM / IDM 这类）的浏览器扩展
            # 会嗅探 audio/video 响应然后弹下载条，很烦人。前端改用 fetch + blob 播放，
            # 不依赖 Content-Type 也能解码，所以这里给个中性的二进制类型。
            handler.send_header("Content-Type", "application/octet-stream")
            for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if h in r.headers:
                    handler.send_header(h, r.headers[h])
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            try:
                for chunk in r.iter_content(65536):
                    if chunk:
                        handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                r.close()
            return
    raise RuntimeError("音频流获取失败（可能是网络或该视频限制）")


# --------------------------------------------------------------------- 静态
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "CoconutMusic/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------- 基础工具
    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return dict(urllib.parse.parse_qsl(raw.decode("utf-8", "ignore")))

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = posixpath_norm(path)
        full = os.path.join(WEB_DIR, rel.lstrip("/"))
        if not os.path.abspath(full).startswith(os.path.abspath(WEB_DIR)) or not os.path.isfile(full):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # --------------------------------------------------------------- 请求入口
    def do_GET(self):
        try:
            self._route("GET")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            self._route("POST")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass

    def _route(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if not path.startswith("/api/"):
            return self._static(path)
        return self._api(method, path, qs)

    # ------------------------------------------------------------------ API
    def _api(self, method, path, qs):
        p = path

        if p == "/api/state":
            login = store.get_login()
            c = client()
            return self._json({"ok": True, "settings": store.get_settings(),
                               "playlists": store.list_playlists(),
                               "login": {"logged_in": bool(login.get("logged_in")),
                                         "uname": c.uname or login.get("uname") or ""}})

        if p == "/api/search":
            kw = (qs.get("q") or "").strip()
            page = int(qs.get("page") or 1)
            order = qs.get("order") or "totalrank"
            if not kw:
                return self._json({"ok": True, "results": []})
            try:
                res = client().search(kw, page=page, order=order)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
            for r in res:
                r["duration_text"] = bili_mod.fmt_duration(r.get("duration"))
            return self._json({"ok": True, "results": res, "page": page})

        if p == "/api/match":
            title = qs.get("title") or ""
            artist = qs.get("artist") or ""
            try:
                duration = int(qs.get("duration") or 0)
            except ValueError:
                duration = 0
            keyword = bili_mod.build_search_query(title, artist)
            try:
                res = client().search(keyword, page_size=50)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
            res.sort(key=lambda r: -score_result(r, title, artist, duration))
            for r in res:
                r["duration_text"] = bili_mod.fmt_duration(r.get("duration"))
                r["score"] = round(score_result(r, title, artist, duration), 1)
            return self._json({"ok": True, "results": res[:8]})

        if p == "/api/source/auto":
            # 懒匹配：QQ 音乐歌单是「秒导」进来的，只存了目录，bvid 是 qq_<mid> 占位符，
            # 真正要播放时才来这里换成一个真实的 B 站音源。
            # 匹配成功后**全局就地替换**（所有歌单 + 播放历史），所以每首歌只会搜一次 ——
            # 这既是为了快，也是为了少触发 B 站风控（重复搜索会 csrf 校验失败）。
            d = self._body()
            old_bvid = str(d.get("bvid") or "").strip()
            title = str(d.get("title") or "").strip()
            artist = str(d.get("artist") or "").strip()
            try:
                duration = int(d.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            if not old_bvid.startswith(share.PLACEHOLDER_PREFIX):
                return self._json({"ok": False, "error": "这首歌已经有音源了，不需要匹配"})
            if not title:
                return self._json({"ok": False, "error": "缺少歌名，没法搜索音源"})
            # 先按「歌名+歌手」搜；挑不到就只用歌名再搜一趟。QQ 带过来的歌手常常是
            # 「アトラスサウンドチーム」这种社团名，挂着它 B 站只回一堆长合集，
            # 单曲反而被挤到五十条之外。
            tried = set()
            pick = None
            search_err = None
            for kw in (bili_mod.build_search_query(title, artist),
                       bili_mod.build_search_query(title, "")):
                if not kw.strip() or kw in tried:
                    continue
                tried.add(kw)
                try:
                    res = client().search(kw, page_size=50)
                except Exception as e:  # noqa: BLE001
                    search_err = str(e)
                    continue
                pick = auto_pick_source(res, title, artist, duration)
                if pick:
                    break
            if not pick:
                return self._json({
                    "ok": False,
                    "error": search_err or "没找到能播的音源，可以点歌曲右边的 ⇄ 手动选一个",
                })
            song = build_matched_song(pick, title, artist)
            touched = store.replace_song_everywhere(old_bvid, song)
            return self._json({
                "ok": True, "song": song, "from": old_bvid,
                "matched_title": pick.get("title") or "",
                "score": round(score_result(pick, title, artist, duration), 1),
                "touched": touched,
                "playlists": store.list_playlists(),
            })

        if p == "/api/stream":
            bvid = qs.get("bvid")
            cid = qs.get("cid")
            # QQ 歌单里还没匹配音源的歌：bvid 是 qq_<mid> 占位，拿它去问 B 站只会得到
            # 「获取视频信息失败：请求错误」这种看不懂的话，先说清楚它到底怎么了。
            if str(bvid or "").startswith(share.PLACEHOLDER_PREFIX):
                return self._json({"ok": False, "placeholder": True,
                                   "error": "这首歌还没匹配到能播的音源"})
            try:
                ent = get_stream(bvid, int(cid) if cid else None)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
            return self._json({"ok": True, "quality": ent["quality"],
                               "quality_id": ent["quality_id"], "cid": ent["cid"]})

        if p == "/api/audio":
            bvid = qs.get("bvid")
            cid = qs.get("cid")
            if not bvid:
                return self._json({"ok": False, "error": "缺少 bvid"}, 400)
            if str(bvid).startswith(share.PLACEHOLDER_PREFIX):
                # 正常流程走不到这里（前端会先自动匹配音源再取流），
                # 属于兜底：给 409 + 能读的中文，别让它变成 500 的英文化报错。
                return self._json({"ok": False, "placeholder": True,
                                   "error": "这首歌还没匹配到能播的音源"}, 409)
            return proxy_audio(self, bvid, int(cid) if cid else None)

        if p == "/api/img":
            url = qs.get("url")
            if not url:
                return self._json({"ok": False, "error": "缺少 url"}, 400)
            if url.startswith("//"):
                url = "https:" + url
            try:
                r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                                 timeout=15)
                data = r.content
                ctype = r.headers.get("Content-Type", "image/jpeg")
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 502)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            self.wfile.write(data)
            return

        # ---------------------------------------------------------- 歌单
        if p == "/api/playlist":
            pl = store.get_playlist(qs.get("id"))
            if not pl:
                return self._json({"ok": False, "error": "歌单不存在"}, 404)
            return self._json({"ok": True, "playlist": pl})

        if p == "/api/playlist/create":
            d = self._body()
            pl = store.create_playlist(d.get("name") or "新建歌单")
            return self._json({"ok": True, "playlist": pl})

        # ------------------------------------------------------ 歌单分享（TXT）
        # 与手机端 coconut-apk 完全同一份线协议，格式细节见 share.py 顶部注释。
        if p == "/api/playlist/share":
            # 取分享文本（前端拿去做"复制文本 / 存文件"兜底）
            pid = qs.get("id") or (self._body().get("id") if method == "POST" else None)
            pl = store.get_playlist(pid)
            if not pl:
                return self._json({"ok": False, "error": "歌单不存在"}, 404)
            text = share.build_share_text(pl)
            if not text:
                return self._json({"ok": False, "error": "歌单里没有可分享的歌曲"})
            return self._json({"ok": True, "text": text, "name": pl.get("name") or "",
                               "file": share.share_file_name(pl),
                               "count": len(pl.get("songs") or [])})

        if p == "/api/playlist/share_save":
            # 导出成 TXT 落到本地（默认下载目录，可指定 dir）。
            # 写盘用 UTF-8 无 BOM、LF 换行、末尾无换行 —— 与手机端逐字节一致。
            d = self._body()
            pl = store.get_playlist(d.get("id"))
            if not pl:
                return self._json({"ok": False, "error": "歌单不存在"}, 404)
            text = share.build_share_text(pl)
            if not text:
                return self._json({"ok": False, "error": "歌单里没有可分享的歌曲"})
            fname = share.share_file_name(pl)
            out_dir = str(d.get("dir") or "").strip() or store.get_settings().get("downloadDir") or ""
            if not out_dir:
                out_dir = os.path.join(os.path.expanduser("~"), "Downloads")
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": "导出目录不可用：" + str(e)})
            full = os.path.join(out_dir, fname)
            try:
                with open(full, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": "写入文件失败：" + str(e)})
            return self._json({"ok": True, "path": full, "name": fname,
                               "count": len(pl.get("songs") or [])})

        if p == "/api/playlist/share_open":
            # 导出并让系统打开所在文件夹，方便直接拖进聊天窗口发出去
            d = self._body()
            pl = store.get_playlist(d.get("id"))
            if not pl:
                return self._json({"ok": False, "error": "歌单不存在"}, 404)
            text = share.build_share_text(pl)
            if not text:
                return self._json({"ok": False, "error": "歌单里没有可分享的歌曲"})
            fname = share.share_file_name(pl)
            out_dir = str(d.get("dir") or "").strip() or store.get_settings().get("downloadDir") or ""
            if not out_dir:
                out_dir = os.path.join(os.path.expanduser("~"), "Downloads")
            try:
                os.makedirs(out_dir, exist_ok=True)
                full = os.path.join(out_dir, fname)
                with open(full, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                os.startfile(full)                     # noqa: S606  Windows 专用，直接打开文件
                return self._json({"ok": True, "path": full, "name": fname})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": "导出失败：" + str(e)})

        if p == "/api/playlist/share_pick":
            # 弹系统「打开文件」让用户挑一个 TXT，服务端直接读出来返回（省得前端碰文件系统）
            path = downloader.pick_file("选择手机分享过来的歌单 TXT")
            if not path:
                return self._json({"ok": True, "picked": False})
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": "读文件失败：" + str(e)})
            return self._json({"ok": True, "picked": True, "path": path, "text": text})

        if p == "/api/playlist/import_share":
            # 接收手机端（或另一台电脑）分享过来的 TXT：解析 → 建歌单 → 批量入库。
            # 解析逻辑与手机端 importSharedText 逐条对齐，见 share.parse_share_text。
            d = self._body()
            raw = d.get("text") or ""
            if not str(raw).strip():
                return self._json({"ok": False, "error": "没有拿到分享文本内容"})
            try:
                parsed = share.parse_share_text(str(raw))
            except share.ShareError as e:
                return self._json({"ok": False, "error": str(e)})
            pl = store.create_playlist(parsed["name"])
            added, _msg = store.add_songs(pl["id"], parsed["songs"])
            skipped = parsed["skipped"]
            return self._json({
                "ok": True,
                "playlist": pl,
                "added": added,
                "skipped": skipped,
                "name": parsed["name"],
                "playlists": store.list_playlists(),
            })

        if p == "/api/import/detect":
            # 只判断"这是什么"，不做事 —— 供前端粘贴后立刻给出类型提示
            d = self._body()
            text = str(d.get("text") or "")
            kind, label = linkparse.detect(text)
            return self._json({"ok": True, "kind": kind, "label": label})

        if p == "/api/import/auto":
            # 统一导入入口：一个输入框，自动分辨三种来源。
            # 判据与手机版 app.js 的 doImport() 一致（顺序也不能变），见 linkparse.py。
            d = self._body()
            raw = str(d.get("text") or "").strip()
            if not raw:
                return self._json({"ok": False, "error": "先把内容粘贴进来"})
            kind, label = linkparse.detect(raw)

            # ---- 1. 椰子歌单分享（纯本地解码，最先认） ----
            if kind == linkparse.KIND_SHARE:
                try:
                    parsed = share.parse_share_text(raw)
                except share.ShareError as e:
                    return self._json({"ok": False, "kind": kind, "error": str(e)})
                pl = store.create_playlist(parsed["name"])
                added, _ = store.add_songs(pl["id"], parsed["songs"])
                return self._json({
                    "ok": True, "kind": kind, "label": label,
                    "playlist": pl, "added": added, "skipped": parsed["skipped"],
                    "total": parsed["total"], "name": parsed["name"],
                    "playlists": store.list_playlists(),
                })

            # ---- 2. QQ 音乐歌单 ----
            if kind == linkparse.KIND_QQ:
                did = qqmusic.extract_disstid(raw)
                if not did:
                    return self._json({"ok": False, "kind": kind,
                                       "error": "没能从这段内容里识别出歌单，"
                                                "请直接粘贴歌单页面链接或歌单 ID"})
                try:
                    info, songs = qqmusic.fetch_playlist(did)
                except Exception as e:  # noqa: BLE001
                    return self._json({"ok": False, "kind": kind, "error": str(e)})
                if not songs:
                    return self._json({"ok": False, "kind": kind,
                                       "error": "这个歌单里没有歌曲"
                                                "（私密歌单读不到，请先设为公开）"})
                # QQ 歌单是「秒导」：只搬目录，音源等第一次播放时再懒匹配
                # （与手机版 importQqPlaylist 的策略一致，避免逐首搜 B 站等几分钟）
                pl = store.create_playlist(info.get("name") or "导入歌单")
                prepared = []
                for s in songs:
                    mid = str(s.get("qq_mid") or "")
                    if not mid:
                        continue
                    prepared.append({
                        "bvid": "qq_" + mid,
                        "cid": 0,
                        "title": s.get("title") or "",
                        "name": s.get("title") or "",
                        "artist": s.get("artist") or "",
                        "duration": int(s.get("duration") or 0),
                        "cover": s.get("cover") or "",
                        "source": "qq",
                        "unmatched": True,
                        "qq_mid": mid,
                    })
                if not prepared:
                    return self._json({"ok": False, "kind": kind,
                                       "error": "歌单里没有可导入的歌曲"})
                added, _ = store.add_songs(pl["id"], prepared)
                return self._json({
                    "ok": True, "kind": kind, "label": label,
                    "playlist": pl, "added": added, "total": len(prepared),
                    "name": pl["name"], "playlists": store.list_playlists(),
                    "info": info,
                })

            # ---- 3. B 站链接 / BV 号 / av 号 ----
            if kind == linkparse.KIND_BILI:
                bvid, page, aid = linkparse.parse_bili_input(raw)
                if not bvid:
                    return self._json({"ok": False, "kind": kind,
                                       "error": "没从链接里找到 BV 号，换一条完整链接或直接贴 BV 号试试"})
                try:
                    song = client().get_video(bvid, p=page)
                except Exception as e:  # noqa: BLE001
                    return self._json({"ok": False, "kind": kind,
                                       "error": f"读取视频信息失败：{e}"})
                pl = store.create_playlist(song.get("title") or "B 站导入")
                added, _ = store.add_songs(pl["id"], [{**song, "bvid": bvid}])
                return self._json({
                    "ok": True, "kind": kind, "label": label,
                    "playlist": pl, "added": added, "total": 1,
                    "name": pl["name"], "song": song,
                    "playlists": store.list_playlists(),
                })

            # ---- 4. 认不出来 ----
            return self._json({
                "ok": False, "kind": kind,
                "error": "认不出这是什么。支持三种：椰子歌单分享文本（COCONUTPL1:…）、"
                         "QQ 音乐歌单链接/歌单ID、B 站链接或 BV 号",
            })

        if p == "/api/playlist/delete":
            d = self._body()
            ok = store.delete_playlist(d.get("id"))
            return self._json({"ok": ok, "playlists": store.list_playlists()})

        if p == "/api/playlist/rename":
            d = self._body()
            ok = store.rename_playlist(d.get("id"), d.get("name"))
            return self._json({"ok": ok, "playlists": store.list_playlists()})

        if p == "/api/playlist/add":
            d = self._body()
            ok, msg = store.add_song(d.get("id"), d.get("song") or {})
            return self._json({"ok": ok, "message": msg, "playlists": store.list_playlists()})

        if p == "/api/playlist/add_many":
            d = self._body()
            added, msg = store.add_songs(d.get("id"), d.get("songs") or [])
            return self._json({"ok": True, "added": added, "message": msg,
                               "playlists": store.list_playlists()})

        if p == "/api/playlist/remove":
            d = self._body()
            ok = store.remove_song(d.get("id"), d.get("bvid"))
            return self._json({"ok": ok, "playlists": store.list_playlists()})

        if p == "/api/playlist/replace":
            d = self._body()
            ok = store.replace_song(d.get("id"), d.get("old_bvid"), d.get("song") or {})
            return self._json({"ok": ok, "message": "已换源" if ok else "没找到要替换的歌",
                               "playlists": store.list_playlists()})

        if p == "/api/playlist/move":
            d = self._body()
            ok = store.move_song(d.get("id"), d.get("bvid"), int(d.get("delta") or 0))
            return self._json({"ok": ok})

        # ---------------------------------------------------------- 历史 / 设置
        if p == "/api/history":
            return self._json({"ok": True, "history": store.load().get("history", [])})

        if p == "/api/history/add":
            d = self._body()
            store.add_history(d.get("song") or {})
            return self._json({"ok": True})

        if p == "/api/history/clear":
            store.clear_history()
            return self._json({"ok": True})

        if p == "/api/settings":
            d = self._body()
            if "key" in d:
                store.set_setting(d["key"], d.get("value"))
                if d["key"] == "hotkeys":
                    apply_hotkeys()
            return self._json({"ok": True, "settings": store.get_settings(),
                               "hotkeyStatus": hotkeys.MANAGER.status()})

        # ---------------------------------------------------------- 全局快捷键
        if p == "/api/hotkey/poll":
            return self._json({"ok": True, "actions": hotkeys.MANAGER.poll()})

        if p == "/api/hotkey/status":
            return self._json({"ok": True, **hotkeys.MANAGER.status()})

        if p == "/api/hotkey/apply":
            return self._json({"ok": True, **apply_hotkeys()})

        # ---------------------------------------------------------- QQ 音乐
        if p == "/api/qq/preview":
            d = self._body()
            raw = (d.get("input") or "").strip()
            if not raw:
                return self._json({"ok": False, "error": "请输入歌单链接或 ID"})
            did = qqmusic.extract_disstid(raw)
            if not did:
                return self._json({"ok": False, "error": "没能从这段内容里识别出歌单，"
                                                          "请直接粘贴歌单页面链接"})
            try:
                info, songs = qqmusic.fetch_playlist(did)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
            return self._json({"ok": True, "info": info, "songs": songs})

        # ---------------------------------------------------------- 下载
        if p == "/api/download/status":
            cur = store.get_settings().get("downloadDir") or ""
            info = downloader.dir_status(cur)
            return self._json({"ok": True, "default": downloader.default_dir(),
                               "ext": downloader.AUDIO_EXT, **info})

        if p == "/api/download/pick":
            path = downloader.pick_folder("选择音乐保存位置")
            if not path:
                return self._json({"ok": True, "picked": False})
            store.set_setting("downloadDir", path)
            return self._json({"ok": True, "picked": True, **downloader.dir_status(path)})

        if p == "/api/download/check":
            d = self._body()
            info = downloader.dir_status(d.get("dir") or "")
            if info.get("ok"):
                store.set_setting("downloadDir", info["path"])
            return self._json(info)

        if p == "/api/download/open":
            d = self._body()
            try:
                downloader.open_folder(d.get("dir") or "")
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if p == "/api/download/song":
            d = self._body()
            bvid = d.get("bvid")
            if not bvid:
                return self._json({"ok": False, "error": "缺少歌曲信息"})
            save_dir = d.get("dir") or store.get_settings().get("downloadDir") or ""
            filename = d.get("filename") or "未命名"
            try:
                ent = get_stream(bvid, d.get("cid"))
            except Exception as e:
                return self._json({"ok": False, "error": f"取音频流出错：{e}"})
            urls = [ent["url"]] + list(ent.get("backup") or [])
            try:
                path, size = downloader.save(urls, filename, save_dir, dict(BILI_HEADERS))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
            return self._json({"ok": True, "path": path, "size": size,
                               "name": os.path.basename(path),
                               "dir": os.path.dirname(path),
                               "converted": path.lower().endswith(downloader.AUDIO_EXT),
                               "quality": ent.get("quality")})

        # ---------------------------------------------------------- 登录
        if p == "/api/login/qrcode":
            try:
                return self._json({"ok": True, **qr_generate()})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if p == "/api/login/poll":
            key = qs.get("key")
            if not key:
                return self._json({"ok": False, "error": "缺少 key"})
            try:
                return self._json({"ok": True, **qr_poll(key)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if p == "/api/login/logout":
            store.set_login("", "", "", False)
            refresh_with_cookie("")
            return self._json({"ok": True})

        return self._json({"ok": False, "error": "未知接口 " + p}, 404)


def posixpath_norm(path):
    path = urllib.parse.unquote(path)
    path = path.replace("\\", "/")
    while "//" in path:
        path = path.replace("//", "/")
    parts = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/" + "/".join(parts)


def apply_hotkeys():
    """按当前设置（重新）注册全局热键。

    有些组合会被系统或显卡驱动占掉（最典型是 Ctrl+Alt+方向键 = 屏幕旋转），
    这时热键模块会自动换一组能用的。这里顺手把设置改写成**真正生效**的那组 ——
    界面显示的和实际用的是同一份，不用再额外弹提示。
    """
    cfg = store.get_settings().get("hotkeys") or {}
    mapping = {k: v for k, v in cfg.items() if k != "enabled"}
    st = hotkeys.MANAGER.apply(mapping, bool(cfg.get("enabled", True)))
    if st.get("fallback"):
        fixed = dict(cfg)
        for action, real in hotkeys.MANAGER.active.items():
            fixed[action] = real
        store.set_setting("hotkeys", fixed)
    return st


def run_server(port=0, host="127.0.0.1"):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    try:
        apply_hotkeys()
    except Exception:
        pass
    return httpd


if __name__ == "__main__":
    srv = run_server(8765)
    print("椰子音乐后端已启动： http://127.0.0.1:8765/")
    srv.serve_forever()
