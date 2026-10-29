# -*- coding: utf-8 -*-
"""椰子音乐 · 下载模块

负责把在线音源保存成本地音频文件：
- 保存位置可配置（默认桌面），带系统原生「选择文件夹」对话框（零依赖）
- 文件名自动清理 Windows 非法字符，重名自动加序号
- 先写 .part 临时文件，下完再原子改名 —— 中断不会留下半个坏文件
- 音源是 AAC 编码的 MP4 容器（.m4a），下完立刻用自带的 ffmpeg 转成 MP3，
  手机、车机、剪辑软件都能直接吃；转码失败就保留 .m4a，不会白下
"""
import ctypes
import lzma
import os
import re
import subprocess
import threading
import time
from ctypes import wintypes

import paths
import requests

# 音源本身是 AAC 编码的 MP4 容器，转码前的中间文件用它
RAW_EXT = ".m4a"
# 交给用户的最终格式
AUDIO_EXT = ".mp3"
# 音源最高 192K AAC，转 MP3 用 320K CBR：二次编码会有损耗，码率给足避免又掉一档
MP3_BITRATE = "320k"
MP3_TIMEOUT = 600

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_WS = re.compile(r"\s+")


def default_dir():
    """默认保存到桌面"""
    d = os.path.join(os.path.expanduser("~"), "Desktop")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def safe_name(name, fallback="未命名"):
    """清掉 Windows 不允许的文件名字符，并限制长度"""
    name = _ILLEGAL.sub("_", (name or "").strip())
    name = _WS.sub(" ", name).strip(" .")
    return name[:110] or fallback


def ensure_dir(path):
    path = os.path.abspath(os.path.expanduser(path or default_dir()))
    os.makedirs(path, exist_ok=True)
    return path


def unique_path(directory, filename):
    """重名就加 (2) (3)…"""
    path = os.path.join(directory, filename)
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(path):
        path = f"{base} ({n}){ext}"
        n += 1
    return path


def dir_status(path):
    """给前端看的目录信息"""
    try:
        p = ensure_dir(path)
        probe = os.path.join(p, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        free = 0
        try:
            free = __import__("shutil").disk_usage(p).free
        except Exception:
            pass
        return {"ok": True, "path": p, "writable": True, "free": free}
    except Exception as e:
        return {"ok": False, "path": path, "writable": False, "error": str(e)}


# --------------------------------------------------------------- 选文件夹
class _BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", ctypes.c_uint),
        ("lpfn", ctypes.c_void_p),
        ("lParam", ctypes.c_void_p),
        ("iImage", ctypes.c_int),
    ]


def pick_folder(title="选择保存位置", owner_title="椰子音乐"):
    """弹出系统原生「选择文件夹」对话框，返回路径；取消返回 None。

    用 shell32.SHBrowseForFolderW —— 本机 Python 没带 tkinter，所以只能用 Win32 API。
    BIF_NEWDIALOGSTYLE 让它是可调整大小的新式外观，BIF_EDITBOX 允许直接粘贴路径。
    """
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        user32 = ctypes.windll.user32
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(_BROWSEINFOW)]
        shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]

        try:
            hwnd = user32.FindWindowW(None, owner_title)
        except Exception:
            hwnd = None

        bi = _BROWSEINFOW()
        bi.hwndOwner = hwnd
        bi.lpszTitle = title
        bi.ulFlags = 0x0001 | 0x0040 | 0x0010   # RETURNONLYFSDIRS | NEWDIALOGSTYLE | EDITBOX
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return None
        buf = ctypes.create_unicode_buffer(2048)
        ok = shell32.SHGetPathFromIDListW(pidl, buf)
        ole32.CoTaskMemFree(pidl)
        return buf.value if ok else None
    except Exception:
        return None


def open_folder(path):
    """用资源管理器打开目录"""
    p = ensure_dir(path)
    subprocess.Popen(["explorer", os.path.normpath(p)])


def pick_file(title="选择文件", owner_title="椰子音乐", filter_text="文本文件 (*.txt)"):
    """弹出系统原生「打开文件」对话框，返回选中路径；取消返回 None。

    和 pick_folder 一样走 shell32 的 Win32 API（本机 Python 没带 tkinter）。
    这里用 GetOpenFileNameW + OPENFILENAMEW，过滤串是"双\\0 结尾"的
    "显示名\\0通配\\0...\\0" 结构，缓冲区整体再以 \\0 收尾。
    """
    try:
        comdlg32 = ctypes.windll.comdlg32
        user32 = ctypes.windll.user32

        class _OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ("lStructSize", ctypes.c_uint),
                ("hwndOwner", ctypes.c_void_p),
                ("hInstance", ctypes.c_void_p),
                ("lpstrFilter", ctypes.c_wchar_p),
                ("lpstrCustomFilter", ctypes.c_wchar_p),
                ("nMaxCustFilter", ctypes.c_uint),
                ("nFilterIndex", ctypes.c_uint),
                ("lpstrFile", ctypes.c_wchar_p),
                ("nMaxFile", ctypes.c_uint),
                ("lpstrFileTitle", ctypes.c_wchar_p),
                ("nMaxFileTitle", ctypes.c_uint),
                ("lpstrInitialDir", ctypes.c_wchar_p),
                ("lpstrTitle", ctypes.c_wchar_p),
                ("Flags", ctypes.c_uint),
                ("nFileOffset", ctypes.c_ushort),
                ("nFileExtension", ctypes.c_ushort),
                ("lpstrDefExt", ctypes.c_wchar_p),
                ("lCustData", ctypes.c_void_p),
                ("lpfnHook", ctypes.c_void_p),
                ("lpTemplateName", ctypes.c_wchar_p),
                ("pvReserved", ctypes.c_void_p),
                ("dwReserved", ctypes.c_uint),
                ("FlagsEx", ctypes.c_uint),
            ]

        try:
            hwnd = user32.FindWindowW(None, owner_title)
        except Exception:
            hwnd = None

        # 过滤串：显示名 \0 通配 \0 显示名 \0 通配 \0 \0
        flt = (filter_text + "\0*.*\0所有文件 (*.*)\0*.*\0\0")
        buf = ctypes.create_unicode_buffer(2048)
        ofn = _OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
        ofn.hwndOwner = hwnd
        ofn.lpstrFilter = flt
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.cast(buf, ctypes.c_wchar_p)
        ofn.nMaxFile = 2048
        ofn.lpstrTitle = title
        # OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_EXPLORER | OFN_NOCHANGEDIR
        ofn.Flags = 0x00001000 | 0x00000800 | 0x00080000 | 0x00000008
        comdlg32.GetOpenFileNameW.restype = ctypes.c_int
        comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(_OPENFILENAMEW)]
        if not comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            return None
        return buf.value or None
    except Exception:
        return None


# ------------------------------------------------------------------ 转码
_NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW，避免转码时黑框一闪
_FF_LOCK = threading.Lock()
_FF_READY = None                 # 确认能用的 ffmpeg 路径，只探测一次


def tools_dir():
    d = paths.data_path("tools")
    os.makedirs(d, exist_ok=True)
    return d


def _usable(exe):
    """这个 ffmpeg 能不能真转 MP3（有些第三方版本缺 libmp3lame，或者依赖同目录的 dll）"""
    try:
        r = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True,
                           timeout=30, creationflags=_NO_WINDOW)
        return b"libmp3lame" in (r.stdout or b"")
    except Exception:  # noqa: BLE001
        return False


def ffmpeg_path():
    """返回可用的 ffmpeg 路径；拿不到返回 None。探测结果按进程缓存。

    打包版把 ffmpeg 以 ffmpeg.xz（LZMA）带在 exe 里，第一次转码时才解压到
    data\\tools\\ffmpeg.exe 并缓存 —— 这样启动不用白等它解包，也不占安装包体积。
    """
    global _FF_READY
    if _FF_READY:
        return _FF_READY
    with _FF_LOCK:
        if _FF_READY:
            return _FF_READY
        exe = os.path.join(tools_dir(), "ffmpeg.exe")
        if os.path.isfile(exe) and _usable(exe):
            _FF_READY = exe
            return exe
        blob = paths.res_path("ffmpeg.xz")
        if not os.path.isfile(blob):
            # 源码直跑时包里没有 ffmpeg.xz，退而求其次用系统里现成的
            for cand in (os.environ.get("COCONUT_FFMPEG", ""),
                         __import__("shutil").which("ffmpeg") or ""):
                if cand and os.path.isfile(cand) and _usable(cand):
                    _FF_READY = cand
                    return cand
            return None
        part = exe + ".part"
        try:
            with open(blob, "rb") as fin, open(part, "wb") as fout:
                dec = lzma.LZMADecompressor()
                while not dec.eof:
                    chunk = fin.read(1 << 20)
                    if not chunk:
                        break
                    out = dec.decompress(chunk)
                    if out:
                        fout.write(out)
            os.replace(part, exe)
        except Exception:  # noqa: BLE001
            try:
                if os.path.exists(part):
                    os.remove(part)
            except Exception:
                pass
            return None
        _FF_READY = exe if _usable(exe) else None
        return _FF_READY


def to_mp3(src):
    """把下载好的音频转成 MP3。成功返回新路径并删掉原文件；失败返回 None 且保留原文件。"""
    ff = ffmpeg_path()
    if not ff:
        return None
    base, _ext = os.path.splitext(src)
    dst = unique_path(os.path.dirname(src), os.path.basename(base) + AUDIO_EXT)
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
           "-vn", "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, dst]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=MP3_TIMEOUT,
                           creationflags=_NO_WINDOW)
    except Exception:  # noqa: BLE001
        delete(dst)
        return None
    if r.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) <= 0:
        delete(dst)
        return None
    delete(src)
    return dst


# ------------------------------------------------------------------ 下载
def save(urls, filename, save_dir, headers, timeout=60, convert=True):
    """把 urls 里第一个能下的保存成本地文件（默认顺手转成 MP3）。

    urls: 依次尝试的直链列表（主链 + 备用链）
    返回 (文件绝对路径, 字节数)
    """
    directory = ensure_dir(save_dir)
    filename = safe_name(filename)
    if not os.path.splitext(filename)[1]:
        filename += RAW_EXT
    path = unique_path(directory, filename)
    part = path + ".part"

    last_err = None
    for u in urls:
        if not u:
            continue
        try:
            with requests.get(u, headers=headers, stream=True, timeout=timeout) as r:
                if r.status_code not in (200, 206):
                    last_err = f"HTTP {r.status_code}"
                    continue
                total = 0
                with open(part, "wb") as f:
                    for chunk in r.iter_content(262144):
                        if chunk:
                            f.write(chunk)
                            total += len(chunk)
                if total <= 0:
                    last_err = "下载到 0 字节"
                    continue
                os.replace(part, path)
                if convert:
                    mp3 = to_mp3(path)
                    if mp3:
                        return mp3, os.path.getsize(mp3)
                return path, total
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            try:
                if os.path.exists(part):
                    os.remove(part)
            except Exception:
                pass
    raise RuntimeError(f"下载失败（{last_err}）")



def delete(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False
