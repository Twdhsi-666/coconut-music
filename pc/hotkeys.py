# -*- coding: utf-8 -*-
"""椰子音乐 · 全局快捷键

用 Win32 的 RegisterHotKey 注册系统级热键 —— 这样软件不在前台（缩在后台听歌）时也能切歌、调音量。
热键属于注册它的线程，所以这里单开一条线程跑消息循环，收到 WM_HOTKEY 就丢进队列；
前端定时轮询 /api/hotkey/poll 把动作取走执行（播放状态在页面上，后端不重复一份）。
"""
import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001

MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
}

VK_MAP = {
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2,
    "media_play_pause": 0xB3, "volume_mute": 0xAD,
    "volume_down": 0xAE, "volume_up": 0xAF,
}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK_MAP[_c.lower()] = ord(_c)
for _d in "0123456789":
    VK_MAP[_d] = ord(_d)
for _i in range(1, 13):
    VK_MAP[f"f{_i}"] = 0x6F + _i


def parse_combo(combo):
    """'Ctrl+Alt+Left' -> (修饰键掩码, 虚拟键码)；解析失败返回 (0, 0)"""
    if not combo:
        return 0, 0
    parts = [p.strip() for p in str(combo).replace("＋", "+").split("+") if p.strip()]
    if not parts:
        return 0, 0
    mods = 0
    vk = 0
    for p in parts:
        low = p.lower().replace("control", "ctrl").replace(" ", "")
        if low in MODIFIERS:
            mods |= MODIFIERS[low]
        elif low in VK_MAP:
            vk = VK_MAP[low]
        elif len(p) == 1:
            vk = user32.VkKeyScanW(ord(p)) & 0xFF
    if not vk:
        return 0, 0
    return mods, vk


def describe(combo):
    """给界面看的友好写法"""
    parts = [p.strip() for p in str(combo or "").split("+") if p.strip()]
    names = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
    out = [names.get(p.lower(), p) for p in parts]
    return " + ".join(out) if out else "未设置"


class HotkeyManager:
    """注册 / 注销全局热键，并把触发的动作放进队列等前端来取"""

    def __init__(self):
        self.queue = []
        self.lock = threading.Lock()
        self.thread = None
        self._stop = threading.Event()
        self.active = {}        # action -> 实际生效的组合键
        self.failed = []        # [(action, combo)] 彻底注册不上
        self.fallback = []      # [(action, 原组合, 实际用的组合)] 原键被占，自动换了备用

    # ------------------------------------------------------------ 对外接口
    def apply(self, mapping, enabled=True):
        """(重新) 注册一批热键。mapping 形如 {"prev": "Ctrl+Alt+Left", ...}"""
        self.stop()
        self.active = {}
        self.failed = []
        self.fallback = []
        if not enabled:
            return self.status()
        self._stop.clear()
        self.thread = threading.Thread(target=self._loop, args=(dict(mapping),), daemon=True)
        self.thread.start()
        for _ in range(25):          # 等注册结果（秒级）
            time.sleep(0.03)
            if self.active or self.failed:
                break
            if self.thread is None or not self.thread.is_alive():
                break
        time.sleep(0.05)
        return self.status()

    def stop(self):
        self._stop.set()
        t = self.thread
        if t and t.is_alive():
            t.join(timeout=1.2)
        self.thread = None
        with self.lock:
            self.queue.clear()

    def poll(self):
        with self.lock:
            out = list(self.queue)
            self.queue.clear()
        return out

    def status(self):
        return {
            "active": {k: describe(v) for k, v in self.active.items()},
            "raw": dict(self.active),          # 原始写法，界面回填用
            "failed": [{"action": a, "combo": describe(c)} for a, c in self.failed],
            "fallback": [{"action": a, "want": describe(w), "got": describe(g)}
                         for a, w, g in self.fallback],
            "enabled": bool(self.thread and self.thread.is_alive()),
        }

    # ------------------------------------------------------------ 消息循环
    def _loop(self, mapping):
        ids = {}
        hid = 0xA100
        for action, combo in mapping.items():
            placed = None
            # 先试用户设的组合，被占了就试备用组合
            for cand in (combo, FALLBACK.get(action)):
                if not cand:
                    continue
                mods, vk = parse_combo(cand)
                if not vk or not mods:
                    continue
                hid += 1
                if user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                    ids[hid] = action
                    placed = cand
                    if cand != combo:
                        self.fallback.append((action, combo, cand))
                    break
            if placed:
                self.active[action] = placed
            else:
                self.failed.append((action, combo))

        msg = wintypes.MSG()
        while not self._stop.is_set():
            got = False
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                got = True
                if msg.message == WM_HOTKEY:
                    action = ids.get(msg.wParam)
                    if action:
                        with self.lock:
                            self.queue.append(action)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.03 if got else 0.08)

        for i in ids:
            user32.UnregisterHotKey(None, i)


MANAGER = HotkeyManager()

DEFAULT_HOTKEYS = {
    "prev": "Ctrl+Alt+Left",
    "next": "Ctrl+Alt+Right",
    "volUp": "Ctrl+Alt+Up",
    "volDown": "Ctrl+Alt+Down",
}

# 系统/驱动常常占用一些组合键 —— 最典型的就是 Ctrl+Alt+方向键（显卡驱动的「屏幕旋转」）。
# 注册失败时自动退到这一组，免得用户开机就发现快捷键没反应。
FALLBACK = {
    "prev": "Ctrl+Alt+Shift+Left",
    "next": "Ctrl+Alt+Shift+Right",
    "volUp": "Ctrl+Alt+Shift+Up",
    "volDown": "Ctrl+Alt+Shift+Down",
}
