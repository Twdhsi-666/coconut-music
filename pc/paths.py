# -*- coding: utf-8 -*-
"""让代码同时支持「源码运行」和「PyInstaller 打包后的 exe」

打包成 exe 之后，`__file__` 指向的是临时解包目录（`sys._MEIPASS`），
那个目录只读、而且每次启动都会重新解压。所以两类路径必须分开：

- **资源**（`web/`、`icon.ico`）：跟着程序走，打包后在 `_MEIPASS` 里 → `res_dir()`
- **数据**（`data/`：歌单、设置、登录 cookie、Edge 缓存）：必须落在 exe 旁边，
  否则每次启动都会丢 → `data_dir()`
"""
import os
import sys


def is_frozen():
    """是不是被打包成 exe 在跑"""
    return getattr(sys, "frozen", False)


def app_dir():
    """程序所在目录：exe 旁边 / 源码目录。可写的东西都放这里"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def res_dir():
    """只读资源目录（web/、图标）"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", app_dir())
    return os.path.dirname(os.path.abspath(__file__))


def res_path(*parts):
    return os.path.join(res_dir(), *parts)


def data_dir():
    """可写数据目录（自动创建）"""
    d = os.path.join(app_dir(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def data_path(*parts):
    return os.path.join(data_dir(), *parts)
