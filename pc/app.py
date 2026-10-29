# -*- coding: utf-8 -*-
"""椰子音乐 · 主程序

窗口方案：优先使用 Edge 的「应用窗口」模式（msedge --app=...）。
原因：本机 WebView2 运行时启动即崩溃（msedgewebview2 has stopped working /
Error launching CrashSender.exe），pywebview 打开只有白屏，所以只作备选。

任务栏图标：Edge 的 --app 窗口默认挂在 "MSEdge" 这个 AppUserModelID 上，图标会是
Edge 的。这里给它一个自己的 AUMID（启动参数 --app-user-model-id）并在 HKCU 注册
DisplayName / IconUri，任务栏就会显示椰子图标，也和 Edge 本身的窗口分开分组。
"""
import ctypes
import os
import socket
import subprocess
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import paths   # noqa: E402  （要在 sys.path 处理之后导入）

RES_DIR = paths.res_dir()       # 只读资源：web/、图标
DATA_DIR = paths.data_dir()     # 可写数据：歌单、设置、缓存

LOG_PATH = paths.data_path("run.log")
FIXED_PORT = 8765
WINDOW_TITLE = "椰子音乐"
AUMID = "CoconutMusic.App.1"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


_instance_mutex = None


def acquire_single_instance():
    """抢一个命名互斥量判重。

    不用「去请求自己的端口」来判：这台机器上连本地没开的端口不会立刻拒绝，
    而是要等满超时，冷启动会白等 1.5 秒。互斥量是内核对象，微秒级。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    h = kernel32.CreateMutexW(None, False, "Local\\CoconutMusic.SingleInstance")
    if not h:
        return True          # 拿不到互斥量时不阻止启动
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(h))
        return False
    global _instance_mutex
    _instance_mutex = h      # 进程活着期间必须持有
    return True


def find_edge():
    for p in EDGE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def _enum_windows(keyword):
    """列出所有标题包含 keyword 的可见顶层窗口句柄（用「包含」而不是「等于」，
    因为页面播放时会把标题改成「歌名 · 椰子音乐」）"""
    u = ctypes.windll.user32
    found = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, _):
        buf = ctypes.create_unicode_buffer(600)
        u.GetWindowTextW(h, buf, 600)
        if keyword in buf.value and u.IsWindowVisible(h):
            found.append(h)
        return True

    u.EnumWindows(CB(cb), None)
    return found


def activate_window():
    """把已经开着的椰子音乐窗口提到最前"""
    hs = _enum_windows(WINDOW_TITLE)
    if not hs:
        return False
    u = ctypes.windll.user32
    for h in hs:
        try:
            u.ShowWindow(h, 9)          # SW_RESTORE
            u.SetForegroundWindow(h)
            return True
        except Exception:
            continue
    return False


def register_app_id():
    """在 HKCU 注册 AppUserModelID（写 HKCU 不需要管理员），让任务栏用椰子图标"""
    try:
        import winreg
        icon = paths.res_path("icon.ico")
        path = r"Software\Classes\AppUserModelId\\" + AUMID
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, WINDOW_TITLE)
        winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, icon)
        winreg.CloseKey(k)
        log(f"AppUserModelID 已注册：{AUMID} -> {icon}")
    except Exception as e:
        log(f"注册 AppUserModelID 失败（忽略）：{e}")


def set_window_icon():
    """把窗口图标（Alt+Tab / 标题栏）也换成椰子；任务栏主要靠 AUMID"""
    try:
        icon = paths.res_path("icon.ico")
        if not os.path.exists(icon):
            return
        u = ctypes.windll.user32
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        big = u.LoadImageW(None, icon, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        small = u.LoadImageW(None, icon, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        hs = _enum_windows(WINDOW_TITLE)
        WM_SETICON, ICON_BIG, ICON_SMALL = 0x0080, 1, 0
        for h in hs:
            u.SendMessageW(h, WM_SETICON, ICON_BIG, big)
            u.SendMessageW(h, WM_SETICON, ICON_SMALL, small)
        if hs:
            log(f"已给 {len(hs)} 个窗口设置椰子图标")
    except Exception as e:
        log(f"设置窗口图标失败（忽略）：{e}")


def launch_edge_app(url):
    """用 Edge 应用窗口打开，返回进程对象；Edge 不在则返回 None"""
    edge = find_edge()
    if not edge:
        return None
    profile = paths.data_path("edgeprofile")
    os.makedirs(profile, exist_ok=True)
    args = [
        edge,
        f"--app={url}",
        "--window-size=1340,870",
        f"--user-data-dir={profile}",
        f"--app-user-model-id={AUMID}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-service-autorun",
        "--disable-features=msEdgeSidebarV2,msEdgeBingChat",
    ]
    log(f"用 Edge 应用窗口打开：{edge}")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def launch_pywebview(url):
    """备选方案（本机 WebView2 坏了，通常会白屏）"""
    try:
        import webview
    except Exception as e:
        log(f"pywebview 不可用：{e}")
        return False
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    webview.create_window(WINDOW_TITLE, url, width=1340, height=870,
                          min_size=(1000, 640), background_color="#eef0f4",
                          text_select=False)
    try:
        webview.start(debug=False, private_mode=False,
                      storage_path=paths.data_path("webview"),
                      gui="edgechromium")
        return True
    except Exception as e:
        log(f"pywebview 启动失败：{e}")
        return False


def open_in_browser(url):
    import webbrowser
    webbrowser.open(url)
    log(f"已用默认浏览器打开 {url}")


def warmup():
    """后台预热 B 站匿名会话，用户第一次搜索就不用等风控重试"""
    try:
        from server import client
        c = client()
        for kw in ("音乐", "热门歌曲"):
            try:
                r = c.search(kw, page_size=20)
            except Exception as e:
                log(f"预热搜索 '{kw}' 出错：{e}")
                continue
            log(f"预热搜索 '{kw}' -> {len(r)} 条")
            if r:
                return
    except Exception as e:
        log(f"预热异常：{e}")


def main():
    if not acquire_single_instance():
        log("检测到椰子音乐已经在运行")
        for _ in range(50):
            if activate_window():
                log("已把已有窗口切到前台，本次退出")
                return
            time.sleep(0.1)
            # 老实例可能刚关窗口、正在退（最多留 3 秒观察期）：互斥量一松就接手启动，
            # 否则「关掉马上再双击」会变成什么都没发生。
            if acquire_single_instance():
                log("上一个实例正在退出，本次接手启动")
                break
        else:
            log("没找到已有窗口，本次不再重复启动")
            return

    port = FIXED_PORT
    from server import run_server
    try:
        srv = run_server(port, "127.0.0.1")
    except OSError:
        port = free_port()
        srv = run_server(port, "127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"服务已启动：http://127.0.0.1:{port}/")

    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=warmup, daemon=True).start()
    register_app_id()

    proc = launch_edge_app(url)
    if proc is None:
        log("没找到 Edge，改用 pywebview")
        if launch_pywebview(url):
            log("窗口已关闭，程序退出")
            return
        open_in_browser(url)
        while True:
            time.sleep(2)

    # 等窗口真正出现，然后换上椰子图标
    appeared = False
    for _ in range(40):
        time.sleep(1)
        if _enum_windows(WINDOW_TITLE):
            appeared = True
            break
    log("窗口已出现" if appeared else "窗口迟迟没出现（继续等待）")
    if appeared:
        time.sleep(1.5)
        set_window_icon()

    try:
        # 不能拿 Edge 进程当「窗口还开着」的信号：Edge 对同一个 user-data-dir 是单实例，
        # 已有 Edge 在跑时新起的 msedge 会把窗口交给老进程、自己立刻退出。
        # 那样 proc.wait() 瞬间返回，主程序先退，窗口变成没有后端的空壳 —— 点歌单就是「歌单不存在」。
        # 改成盯窗口本身：连续 3 秒看不见椰子窗口才退出。
        missing = 0
        while True:
            time.sleep(1)
            if _enum_windows(WINDOW_TITLE):
                missing = 0
            else:
                missing += 1
                if missing >= 3:
                    break
    except KeyboardInterrupt:
        pass
    log("窗口已关闭，程序退出")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("崩溃：\n" + traceback.format_exc())
        raise
