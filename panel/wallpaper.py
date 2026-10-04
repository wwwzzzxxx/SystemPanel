"""将窗口嵌入桌面壁纸层（WorkerW），无交互。"""
from __future__ import annotations

import ctypes

try:
    import win32con  # type: ignore
    import win32gui  # type: ignore
except ImportError:
    win32gui = None  # type: ignore[assignment]
    win32con = None  # type: ignore[assignment]


def _find_wallpaper_workerw() -> int | None:
    """返回 WorkerW 句柄，失败返回 None。"""
    if win32gui is None:
        return None
    try:
        progman = win32gui.FindWindow("Progman", "Program Manager")
        if not progman:
            return None
        # 触发系统创建 WorkerW（壁纸层容器）
        ctypes.windll.user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 1000, ctypes.byref(ctypes.c_ulong()))
        workerw = None

        def _enum(hwnd, _):
            nonlocal workerw
            # WorkerW 的特征：包含 SHELLDLL_DefView 的下一个 WorkerW
            shell = win32gui.FindWindowEx(hwnd, None, "SHELLDLL_DefView", None)
            if shell:
                nxt = win32gui.FindWindowEx(None, hwnd, "WorkerW", None)
                if nxt:
                    workerw = nxt
            return True

        win32gui.EnumWindows(_enum, None)
        return workerw
    except Exception:
        return None


def pin_to_wallpaper(hwnd: int) -> bool:
    """将给定窗口句柄设为 WorkerW 的子窗口，成功返回 True。"""
    if win32gui is None:
        return False
    workerw = _find_wallpaper_workerw()
    if not workerw:
        return False
    try:
        win32gui.SetParent(hwnd, workerw)
        return True
    except Exception:
        return False


def make_click_through(hwnd: int):
    """鼠标穿透到桌面图标（壁纸层本身就穿透，此为非嵌入时的回退）。"""
    if win32gui is None or win32con is None:
        return
    try:
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        ex |= win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
    except Exception:
        pass
