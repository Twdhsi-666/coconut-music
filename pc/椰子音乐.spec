# -*- mode: python ; coding: utf-8 -*-
"""椰子音乐 · PyInstaller 打包配置

产物：单文件 exe（无控制台），界面资源与图标打进去，数据目录落在 exe 旁边。

## 打包前要准备什么

1. `pip install pyinstaller`（其余依赖都是标准库 + requests）
2. `ffmpeg.xz` —— 这是 LZMA 压缩过的 ffmpeg.exe，用来给下载的音频转 MP3。
   仓库里不放（22 MB，进 git 不合适），从本项目的 Release 页面下载同名文件，
   放到本目录（和本 spec 同级）即可。
   **没有它也能打包**：spec 会自动跳过，exe 仍可运行，只是「下载后自动转 MP3」
   这一条降级成「保留原始 .m4a」，并在界面上如实提示，不会假装转好了。

## 为什么 ffmpeg 要压成 xz 塞进包，而不是直接放 ffmpeg.exe

单文件模式每次启动都会把包内所有内容解压到临时目录。直接塞 83 MB 的 ffmpeg.exe
等于每次开机多写 83 MB；压成 xz 只多 21.9 MB，而且解压动作推迟到"真的要转码的那一次"，
解到 data/tools/ffmpeg.exe 后永久缓存。实测首次转码多等 1.1 秒。

## 用法

    pyinstaller 椰子音乐.spec --noconfirm
"""
import os

from PyInstaller.utils.hooks import collect_all

HERE = os.path.abspath(SPECPATH)      # 本 spec 所在目录（仓库里是 pc/）
SRC = HERE                            # 源码就在同目录，没有 src 子层

datas = [
    (os.path.join(SRC, "web"), "web"),
    (os.path.join(SRC, "icon.ico"), "."),
    (os.path.join(SRC, "icon.png"), "."),
]
_ffmpeg = os.path.join(SRC, "ffmpeg.xz")
if os.path.exists(_ffmpeg):
    datas.append((_ffmpeg, "."))
else:
    print("!! 没找到 ffmpeg.xz：打出来的包不带转码能力，下载会保留 .m4a")
binaries = []

hiddenimports = [
    "store", "hotkeys", "downloader", "paths", "bili", "qqmusic",
    "server", "share", "linkparse",
]

tmp_ret = collect_all("qrcode")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    [os.path.join(SRC, "app.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PIL", "numpy"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="椰子音乐",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(SRC, "icon.ico")],
)
