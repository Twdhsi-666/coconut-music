# -*- coding: utf-8 -*-
"""
椰子音乐 · 歌单分享文本（TXT）

这里是 PC 端与手机端共用的同一份线协议，**字节级完全对齐**，
手机端实现在 `coconut-apk/app/java/org/coconut/music/NativeBridge.java`
的 `buildShareText()` / `exportPlaylistTxt()`，以及 `.../MainActivity.java`
的 `readText()`；解析端在 `coconut-apk/app/assets/app.js` 的 `importSharedText()`。

## 格式（一字不差，改动即破坏双向兼容）

文件是纯文本，UTF-8 **无 BOM**、LF（\\n）换行、**末尾没有换行**：

    第 1 行   🥥 椰子音乐 · 歌单分享
    第 2 行   「<歌单名>」共 <N> 首
    第 3 行   把本文件发到对方手机，在椰子音乐的「导入」里选择即可
    第 4 行   COCONUTPL1:<Base64>

Base64 是 UTF-8 JSON 的标准 base64（含 `=` 填充），解码后是一个对象：

    {"v":1,"name":"<歌单名>","songs":[ [六元组], [六元组], ... ]}

- 键顺序固定 `v` → `name` → `songs`（Android JSONObject 保插入序，PC 端这里也一样）
- 分隔符必须是紧凑形式 `,` / `:`，**冒号后不留空格**（Android 的 JSONObject.toString 就是紧凑的）
- `songs` 每个元素是**数组**不是对象，顺序固定六个：
  `[bvid, cid, 歌名, 歌手, 时长(整秒), 封面url]`
- cid / duration 是数字，其余是字符串；缺失字段按空串 / 0 输出

## 几个格式上的约定

- 中文**不转义**（`ensure_ascii=False`），直接以 UTF-8 进 base64。
  注意 Android 那边也是直接 `getBytes("UTF-8")`，所以两边一致。
- 手机端 `android.util.Base64.NO_WRAP` 不换行，PC 端 `base64.b64encode` 同样不换行。
- 末尾不加换行：手机端 `os.write(text.getBytes())` 写完即止。

## 占位音源（"对方导出时还没匹配音源"的歌）

手机端约定：歌单里 B 站匹配还没做的歌，`bvid` 存成 `qq_<歌曲mid>`，
导入方看到 `qq_` 前缀就还原成 `qq_mid` + `unmatched=true`，播放时再懒匹配。
PC 端完整遵守这个约定，双向都认。
"""
import base64
import json
import re

# 分享文本里嵌的 Base64 前缀。识别时用宽松匹配，容忍前面的说明文字和换行。
PREFIX = "COCONUTPL1:"

# 与手机端 app.js 完全相同的识别正则（原样照搬，别改）
SHARE_RE = re.compile(r"COCONUTPL1:([A-Za-z0-9+/=\s]+)")

# 占位音源前缀：qq_<mid> 表示"这首歌只匹配到了 QQ 音乐信息，B 站音源还没找到"
PLACEHOLDER_PREFIX = "qq_"

HEADER_LINE1 = "\U0001F95C 椰子音乐 · 歌单分享"
HEADER_LINE3 = "把本文件发到对方手机，在椰子音乐的「导入」里选择即可"


def _safe_file_name(name, fallback="歌单", limit=24):
    """文件名净化：与手机端 exportPlaylistTxt 的规则一致。

    手机端：name.replaceAll("[\\\\/:*?\\"<>|\\s]+", "_")，再截到 24 个字符。
    这里用等价的正则表达（Python 里 \\s 覆盖的字符集和 Java 略有出入，
    但都是"空白与非合法文件名字符统一换下划线"，实际结果一致）。
    """
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "")).strip()
    if not s:
        s = fallback
    return s[:limit]


def build_payload(playlist):
    """把一份歌单变成载荷 dict（键顺序即最终 JSON 的键顺序）。

    playlist 是 store 里的歌单 dict：{name, songs:[{bvid,cid,title/name,artist,duration,cover}]}
    """
    songs = (playlist or {}).get("songs") or []
    arr = []
    for s in songs:
        if not isinstance(s, dict):
            continue
        bvid = str(s.get("bvid") or "").strip()
        if not bvid:
            # 手机端同样跳过没有 bvid 的条目（没法定位音源，导过去也没用）
            continue
        # 歌名优先 title，退回 name；再把 name 也顺手补上，方便两边显示一致
        title = s.get("title") or s.get("name") or ""
        arr.append([
            bvid,
            int(s.get("cid") or 0),
            str(title),
            str(s.get("artist") or s.get("author") or ""),
            int(s.get("duration") or 0),
            str(s.get("cover") or ""),
        ])
    # 用 dict 保序（Py3.7+ 的 dict 就是插入序），再靠 json.dumps 的紧凑分隔符对齐 Android
    return {"v": 1, "name": str((playlist or {}).get("name") or ""), "songs": arr}


def build_share_text(playlist):
    """生成完整的分享文本。歌单为空 / 没有可用歌曲时返回 None（与手机端一致）。"""
    payload = build_payload(playlist)
    if not payload["songs"]:
        return None
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
    return (
        HEADER_LINE1 + "\n"
        + "「" + payload["name"] + "」共 " + str(len(payload["songs"])) + " 首\n"
        + HEADER_LINE3 + "\n"
        + PREFIX + b64
    )


def share_file_name(playlist):
    """分享 TXT 的文件名（与手机端同一套净化规则，带 .txt 后缀）。"""
    return _safe_file_name((playlist or {}).get("name"), "歌单") + ".txt"


# ------------------------------------------------------------------ 解析

class ShareError(Exception):
    """分享文本解析失败。message 是直接能给用户看的中文。"""


def looks_like_share(text):
    """粗略判断一段文本是不是椰子分享文本（用于导入框自动分流）。"""
    return bool(SHARE_RE.search(text or ""))


def extract_b64(text):
    """从任意文本里抠出 Base64 段；抠不到返回 None。"""
    m = SHARE_RE.search(text or "")
    return m.group(1) if m else None


def decode_payload(text):
    """文本 → payload dict。失败抛 ShareError（带中文提示）。"""
    b64 = extract_b64(text)
    if not b64:
        raise ShareError("这段文本里没有找到椰子歌单分享内容")
    # 与手机端一致：先把空白去掉再解（Base64 里可能被聊天软件插了换行）
    compact = re.sub(r"\s+", "", b64)
    try:
        raw = base64.b64decode(compact, validate=False)
    except Exception:
        raise ShareError("分享文本不完整或已损坏（Base64 解不开）")
    try:
        # 手机端是 TextDecoder('utf-8')，PC 端就用 utf-8；非法字节用替换字符兜住而不是抛错
        obj = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise ShareError("分享文本不完整或已损坏（内容不是有效的歌单数据）")
    if not isinstance(obj, dict):
        raise ShareError("分享文本格式不对：顶层不是歌单对象")
    return obj


def parse_songs(payload):
    """payload → 可直接入库的 PC 端 song 列表。

    字段映射与手机端 importSharedText 完全一致：
        [0] bvid  [1] cid  [2] title/name  [3] artist  [4] duration  [5] cover
    """
    arr = (payload or {}).get("songs")
    if not isinstance(arr, list) or not arr:
        raise ShareError("这段分享文本里没有歌曲")

    songs = []
    skipped = 0
    for a in arr:
        if not isinstance(a, list) or not a:
            skipped += 1
            continue
        bvid = str(a[0] or "").strip()
        if not bvid:
            skipped += 1
            continue

        def _at(i, default=""):
            return a[i] if i < len(a) and a[i] is not None else default

        title = str(_at(2))
        try:
            cid = int(_at(1, 0) or 0)
        except (TypeError, ValueError):
            cid = 0
        try:
            duration = int(float(_at(4, 0) or 0))
        except (TypeError, ValueError):
            duration = 0

        song = {
            "bvid": bvid,
            "cid": cid,
            "title": title,
            "name": title,
            "artist": str(_at(3)),
            "duration": duration,
            "cover": str(_at(5)),
            "source": "bili",
        }
        # 占位音源：对方导出时 B 站音源还没匹配上，记下 mid 与 unmatched 标记，
        # 播放时由 PC 端自己懒匹配（与手机端同一套约定）。
        if bvid.startswith(PLACEHOLDER_PREFIX):
            song["qq_mid"] = bvid[len(PLACEHOLDER_PREFIX):]
            song["unmatched"] = True
            song["source"] = "qq"
        songs.append(song)

    if not songs:
        raise ShareError("这段分享文本里没有可导入的歌曲")
    return songs, skipped


def parse_share_text(text):
    """一站式：文本 → {name, songs, skipped, total}。失败抛 ShareError。"""
    payload = decode_payload(text)
    songs, skipped = parse_songs(payload)
    name = str(payload.get("name") or "").strip() or "分享的歌单"
    return {
        "name": name,
        "songs": songs,
        "skipped": skipped,
        "total": len(songs) + skipped,
    }
