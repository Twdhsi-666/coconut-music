# -*- coding: utf-8 -*-
"""椰子音乐 · 下载模块

负责把在线音源保存成本地音频文件：
- 保存位置可配置（默认桌面），带系统原生「选择文件夹」对话框（零依赖）
- 文件名自动清理 Windows 非法字符，重名自动加序号
- 先写 .part 临时文件，下完再原子改名 —— 中断不会留下半个坏文件
"""
import ctypes
import os
import re
import subprocess
import time
from ctypes import wintypes

import requests


AUDIO_EXT = '.m4a'

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_WS = re.compile(r'\s+')


def default_dir():
    """默认保存到桌面"""
    d = os.path.join(os.path.expanduser('~'), 'Desktop')
    return d if os.path.isdir(d) else os.path.expanduser('~')


def safe_name(name, fallback='未命名'):
    """清掉 Windows 不允许的文件名字符，并限制长度"""
    name = _ILLEGAL.sub('_', (name or '').strip())
    name = _WS.sub(' ', name).strip(' .')
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
        path = f'{base} ({n}){ext}'
        n += 1
    return path


def dir_status(path):
    """给前端看的目录信息"""
    try:
        p = ensure_dir(path)
        probe = os.path.join(p, '.write_test')
        with open(probe, 'w') as f:
            f.write('ok')
        os.remove(probe)
        free = 0
        try:
            free = __import__('shutil').disk_usage(p).free
        except Exception:
            pass
        return {'ok': True, 'path': p, 'writable': True, 'free': free}
    except Exception as e:
        return {'ok': False, 'path': path, 'writable': False, 'error': str(e)}



class _BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ('hwndOwner', wintypes.HWND),
        ('pidlRoot', ctypes.c_void_p),
        ('pszDisplayName', wintypes.LPWSTR),
        ('lpszTitle', wintypes.LPCWSTR),
        ('ulFlags', ctypes.c_uint),
        ('lpfn', ctypes.c_void_p),
        ('lParam', ctypes.c_void_p),
        ('iImage', ctypes.c_int),
    ]


def pick_folder(title='选择保存位置', owner_title='椰子音乐'):
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
        bi.ulFlags = 81
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
    subprocess.Popen(['explorer', os.path.normpath(p)])


def pick_file(title='选择文件', owner_title='椰子音乐', filter_text='文本文件 (*.txt)'):
    r"""弹出系统原生「打开文件」对话框，返回选中路径；取消返回 None。

    和 pick_folder 一样走 shell32 的 Win32 API（本机 Python 没带 tkinter）。
    这里用 GetOpenFileNameW + OPENFILENAMEW，过滤串是"双\0 结尾"的
    "显示名\0通配\0...\0" 结构，缓冲区整体再以 \0 收尾。
    """
    try:
        comdlg32 = ctypes.windll.comdlg32
        user32 = ctypes.windll.user32

        class _OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ('lStructSize', ctypes.c_uint),
                ('hwndOwner', ctypes.c_void_p),
                ('hInstance', ctypes.c_void_p),
                ('lpstrFilter', ctypes.c_wchar_p),
                ('lpstrCustomFilter', ctypes.c_wchar_p),
                ('nMaxCustFilter', ctypes.c_uint),
                ('nFilterIndex', ctypes.c_uint),
                ('lpstrFile', ctypes.c_wchar_p),
                ('nMaxFile', ctypes.c_uint),
                ('lpstrFileTitle', ctypes.c_wchar_p),
                ('nMaxFileTitle', ctypes.c_uint),
                ('lpstrInitialDir', ctypes.c_wchar_p),
                ('lpstrTitle', ctypes.c_wchar_p),
                ('Flags', ctypes.c_uint),
                ('nFileOffset', ctypes.c_ushort),
                ('nFileExtension', ctypes.c_ushort),
                ('lpstrDefExt', ctypes.c_wchar_p),
                ('lCustData', ctypes.c_void_p),
                ('lpfnHook', ctypes.c_void_p),
                ('lpTemplateName', ctypes.c_wchar_p),
                ('pvReserved', ctypes.c_void_p),
                ('dwReserved', ctypes.c_uint),
                ('FlagsEx', ctypes.c_uint),
            ]

        try:
            hwnd = user32.FindWindowW(None, owner_title)
        except Exception:
            hwnd = None


        flt = filter_text + '\x00*.*\x00所有文件 (*.*)\x00*.*\x00\x00'
        buf = ctypes.create_unicode_buffer(2048)
        ofn = _OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
        ofn.hwndOwner = hwnd
        ofn.lpstrFilter = flt
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.cast(buf, ctypes.c_wchar_p)
        ofn.nMaxFile = 2048
        ofn.lpstrTitle = title

        ofn.Flags = 530440
        comdlg32.GetOpenFileNameW.restype = ctypes.c_int
        comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(_OPENFILENAMEW)]
        if not comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            return None
        return buf.value or None
    except Exception:
        return None



def save(urls, filename, save_dir, headers, timeout=60):
    """把 urls 里第一个能下的保存成本地文件。

    urls: 依次尝试的直链列表（主链 + 备用链）
    返回 (文件绝对路径, 字节数)
    """
    directory = ensure_dir(save_dir)
    filename = safe_name(filename)
    if not os.path.splitext(filename)[1]:
        filename += AUDIO_EXT
    path = unique_path(directory, filename)
    part = path + '.part'

    last_err = None
    for u in urls:
        if not u:
            continue
        try:
            with requests.get(u, headers=headers, stream=True, timeout=timeout) as r:
                if r.status_code not in (200, 206):
                    last_err = f'HTTP {r.status_code}'
                    continue
                total = 0
                with open(part, 'wb') as f:
                    for chunk in r.iter_content(262144):
                        if not chunk: continue
                        f.write(chunk)
                        total += len(chunk)
                if total <= 0:
                    last_err = '下载到 0 字节'
                    continue
                os.replace(part, path)
                return path, total
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
            try:
                if os.path.exists(part):
                    os.remove(part)
            except Exception:
                pass
    raise RuntimeError(f'下载失败（{last_err}）')


def delete(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False
