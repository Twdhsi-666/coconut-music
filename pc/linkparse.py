# -*- coding: utf-8 -*-
"""
椰子音乐 · 统一导入识别

一个输入框，三种来源，自动分辨 —— 与手机版 `app.js` 的 `doImport()` /
`checkClipboard()` 用同一套判据，保证两端"粘同一个东西、做同一件事"。

## 判据顺序（**顺序不能变**）

1. **椰子歌单分享** —— 文本里含 `COCONUTPL1:`
   （手机版注释原话："纯本地解码入库，不需要网络，放最前面识别"）
2. **QQ 音乐歌单** —— 命中 `/y\\.qq\\.com|qq\\.com|qqmusic|qq\\.music|musicu\\.fcg/i`
   或**纯 6 位以上数字**（歌单 ID）
3. **B 站链接 / BV 号 / av 号** —— `bilibili.com` / `b23.tv` / `BV1xxxxxxxxx` / `av12345`
4. 都不是 → 报错，并告诉用户三种都支持

第 2 步必须在第 3 步之前：QQ 音乐的分享短链里也可能出现数字，反过来 B 站链接
不会被 QQ 的判据命中。手机版就是这个顺序，照搬。

## B 站输入的抠取规则（与手机版 NativeBridge 一致）

- BV 号：`BV[0-9A-Za-z]{10}`（手机版 fixSongSource 用 {10}；宽松版用 {8,}）
- av 号：`(?:av|AV)(\\d{4,})`
- 分 P：`[?&]p=(\\d+)`
- 短链 `b23.tv/xxxx` 要**跟随 302** 拿最终地址再抠 —— 手机版用 HttpURLConnection，
  这里用 requests 的 allow_redirects
- 都不含却长得像 URL → 不是 B 站，报错别硬猜

## 与"搜索词"的关系

手机上 `fixSongSource` 有个兜底：输入既不是链接也不是 BV 号时，**当搜索词**去搜。
但那是"换音源"场景。**导入场景不这么做** —— 用户粘一句普通中文进导入框，
当搜索词悄悄导一首歌进来是意外行为，不如明确报错。
"""
import re

import requests

# ---------------------------------------------------------------- QQ 判据
# 与手机版 app.js 逐字相同的正则
QQ_RE = re.compile(r"y\.qq\.com|qq\.com|qqmusic|qq\.music|musicu\.fcg", re.I)
QQ_ID_RE = re.compile(r"^\d{6,}$")

# ---------------------------------------------------------------- B 站判据
BILI_RE = re.compile(r"bilibili\.com|b23\.tv|BV1[0-9A-Za-z]{9}|av\d{5,}", re.I)
BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
AID_RE = re.compile(r"(?:av|AV)(\d{4,})")
PAGE_RE = re.compile(r"[?&]p=(\d+)")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 三种来源的标签，给前端提示用
KIND_SHARE = "share"
KIND_QQ = "qq"
KIND_BILI = "bili"
KIND_UNKNOWN = "unknown"

KIND_LABEL = {
    KIND_SHARE: "椰子歌单分享",
    KIND_QQ: "QQ 音乐歌单链接",
    KIND_BILI: "B 站链接",
    KIND_UNKNOWN: "无法识别",
}


def detect(text):
    """判断一段输入属于哪种来源，返回 (kind, 说明)。

    判据顺序与手机版一致，见模块 docstring。`kind` 为 KIND_* 之一。
    """
    t = (text or "").strip()
    if not t:
        return KIND_UNKNOWN, "内容为空"
    if "COCONUTPL1:" in t:
        return KIND_SHARE, KIND_LABEL[KIND_SHARE]
    if QQ_RE.search(t) or QQ_ID_RE.match(t):
        return KIND_QQ, KIND_LABEL[KIND_QQ]
    if BILI_RE.search(t):
        return KIND_BILI, KIND_LABEL[KIND_BILI]
    # 再宽一点：用户可能只贴了个裸 BV 号（V 大写小写混着写）
    if BVID_RE.search(t) or AID_RE.search(t):
        return KIND_BILI, KIND_LABEL[KIND_BILI]
    return KIND_UNKNOWN, KIND_LABEL[KIND_UNKNOWN]


def looks_like_url(s):
    return (s or "").startswith("http") or "b23.tv" in (s or "") or "bilibili.com" in (s or "")


def follow_redirect(url, timeout=10):
    """b23.tv 等短链跟随 302 拿最终地址；拿不到就原样返回（与手机版同语义）。"""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout,
                         allow_redirects=True)
        return r.url or url
    except Exception:
        return url


def parse_bili_input(text):
    """把用户输入解析成 (bvid, p)。解析不出 bvid 返回 (None, 1)。

    顺序：先直接抠 BV/av，抠不到且像链接就跟随跳转再抠（手机版 fixSongSource 同款）。
    """
    t = (text or "").strip()
    bvid = None
    m = BVID_RE.search(t)
    if m:
        bvid = m.group()
    page = 1
    pm = PAGE_RE.search(t)
    if pm:
        try:
            page = max(1, int(pm.group(1)))
        except ValueError:
            page = 1

    if not bvid and looks_like_url(t):
        final = follow_redirect(t)
        fm = BVID_RE.search(final)
        if fm:
            bvid = fm.group()
        fp = PAGE_RE.search(final)
        if fp:
            try:
                page = max(page, int(fp.group(1)))
            except ValueError:
                pass

    if not bvid:
        # 只有 av 号：B 站 view 接口也支持 aid，但我们要统一用 bvid，
        # 所以这里交给调用方去用 aid 查（见 server 侧处理）
        am = AID_RE.search(t)
        if am:
            return None, page, am.group(1)
    return bvid, page, None
