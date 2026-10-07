"""可见性门控：看不见面板就不问盘。

面板沉底后在壁纸层（HWND_BOTTOM + WS_EX_TRANSPARENT），任何全屏/最大化
窗口盖住它时，用户都看不到温度数字——此时跳过 LibreHardwareMonitor 的
Storage hw.Update()（同步 NVMe/SATA SMART passthrough，会顶一下系统，
游戏中表现为一次“系统中断”抖动），CPU（MSR 直读，微秒级）与 GPU（NVML）
不受影响，照常更新。

判定复用 contextmenu._visible_at 的链式逻辑（WindowFromPoint 穿过透明面板
后看落点）：落到面板句柄本身，或一路穿到桌面类（Progman/WorkerW/
SHELLDLL_DefView），都算“露出来了”。面板矩形内采样中心+四角（内缩），
任一点可见即视为可见。

失败默认可见（fail-open）：判定抛异常就返回 True，保持旧行为，不改变轮询。
判定本身只有几个 user32 syscall，无磁盘 IO，每 3s 一轮可忽略。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

try:
    _user32 = ctypes.windll.user32
    _shell32 = ctypes.windll.shell32
except Exception:  # pragma: no cover - 非 Windows
    _user32 = None  # type: ignore[assignment]
    _shell32 = None  # type: ignore[assignment]

_DESKTOP_CLASSES = ("Progman", "WorkerW", "SHELLDLL_DefView")

# SHQueryUserNotificationState 结果（shellapi.h）：
#  2 QUNS_BUSY / 3 QUNS_RUNNING_D3D_FULL_SCREEN / 4 QUNS_PRESENTATION_MODE
# 独占全屏时面板必定不可见，可直接短路，省掉 WindowFromPoint。
_FULLSCREEN_STATES = (2, 3, 4)

_clsbuf = ctypes.create_unicode_buffer(256)


# 游戏白名单：这些进程在跑时一律不问盘（与面板是否可见无关）。
# 扩展时往里加小写 exe 名即可，如 "dota2.exe"。
_GAME_PROCESSES = frozenset({"cs2.exe"})


def game_blocking_temp() -> str | None:
    """白名单游戏在跑就返回其进程名，否则返回 None。

    只扫进程名，不读 IO，不碰盘，每 3s 一轮开销可忽略。
    psutil 缺失/枚举失败时返回 None（fail-open，保持旧行为）。
    """
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        for p in psutil.process_iter(["name"]):
            try:
                name = (p.info.get("name") or "").lower()
            except Exception:
                continue
            if name in _GAME_PROCESSES:
                return name
    except Exception:
        return None
    return None


def _declare() -> None:
    try:
        assert _user32 is not None
        _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_long * 4)]
        _user32.WindowFromPoint.argtypes = [wintypes.POINT]
        _user32.WindowFromPoint.restype = wintypes.HWND
        _user32.GetParent.argtypes = [wintypes.HWND]
        _user32.GetParent.restype = wintypes.HWND
        _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        _user32.GetClassNameW.restype = ctypes.c_int
        _user32.GetForegroundWindow.restype = wintypes.HWND
    except Exception:
        pass


if _user32 is not None:
    _declare()


def _fullscreen_exclusive() -> bool:
    """独占全屏 D3D / presentation 模式时直接判不可见（面板必被盖住）。"""
    try:
        if _shell32 is None:
            return False
        state = wintypes.DWORD(0)
        hr = _shell32.SHQueryUserNotificationState(ctypes.byref(state))
        if hr != 0:  # S_OK=0，非 0 表示拿不到，按“可见”处理
            return False
        return int(state.value) in _FULLSCREEN_STATES
    except Exception:
        return False


def _point_visible(target: int, x: int, y: int) -> bool:
    """单点是否露的是面板（与 contextmenu._visible_at 同口径）。"""
    assert _user32 is not None
    h = _user32.WindowFromPoint(wintypes.POINT(x, y))
    cur = h
    for _ in range(64):
        if not cur:
            break
        if cur == target:
            return True
        try:
            nxt = _user32.GetParent(cur)
        except Exception:
            break
        if not nxt or nxt == cur:
            cur = cur if cur else h
            break
        cur = nxt
    try:
        n = _user32.GetClassNameW(cur, _clsbuf, 256)
    except Exception:
        return False
    if n > 0:
        return _clsbuf.value in _DESKTOP_CLASSES
    return False


def is_panel_visible(hwnd: int | None) -> bool:
    """面板任一采样点露出来即返回 True。拿不到句柄/异常时返回 True。"""
    if not hwnd or _user32 is None:
        return True
    try:
        if _fullscreen_exclusive():
            return False
        rect = (ctypes.c_long * 4)()
        if not _user32.GetWindowRect(hwnd, rect):
            return True
        l, t, r, b = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
        if r <= l or b <= t:
            return True
        inset = 6
        cx, cy = (l + r) // 2, (t + b) // 2
        pts = [
            (cx, cy),
            (l + inset, t + inset),
            (r - inset, t + inset),
            (l + inset, b - inset),
            (r - inset, b - inset),
        ]
        for x, y in pts:
            try:
                if _point_visible(int(hwnd), x, y):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return True
