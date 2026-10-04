"""面板右键菜单。

难点：上锁后面板是 WS_EX_TRANSPARENT，收不到任何鼠标事件。
做法：WH_MOUSE_LL 全局低级钩子，只做最少的事——命中时写原生槽位 + SetEvent，
工作线程等到事件后经 event_generate（Tk 官方跨线程通道）通知主循环。
钩子回调里不做任何 Python 堆 / Tcl / 打印操作（曾因此 fastfail 崩溃 3 次）。

- 右键按下在面板内 -> 按下和随后松开都吞掉（桌面菜单是在松开时弹的），弹我们的菜单
- 菜单开着时，落在菜单矩形外的任何按下 -> 关菜单（不吞，放行；焦点不可靠，只能靠钩子）
- 其他情况 -> 原样放行，桌面行为零影响

未锁定（拖位置）状态下面板本身可点，走 Tk 的 <Button-3>，钩子只在锁定时出手，
两者不会双重触发。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import tkinter as tk
from ctypes import wintypes

from . import ops

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_MOUSE_LL = 14
HC_ACTION = 0
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
WM_RBUTTONUP = 0x0205
GWL_EXSTYLE = -20
INFINITE = 0xFFFFFFFF
_MENU_EVENT = "<<PanelMenuClick>>"
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

REQ_NONE = 0
REQ_OPEN = 1
REQ_DISMISS = 2


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_ssize_t, ctypes.c_ssize_t)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                  wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)


def _declare():
    """64 位下 LPARAM/WPARAM 是 64 位，不声明 argtypes 会溢出。"""
    try:
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, ctypes.c_ssize_t, ctypes.c_ssize_t]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_long * 4)]
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.GetTickCount.argtypes = []
        kernel32.GetTickCount.restype = wintypes.DWORD
        user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                           ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD]
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    except Exception:
        pass


_declare()


class PanelMenu:
    """右键菜单控制器，由 PanelApp 持有。"""

    def __init__(self, app):
        self.app = app
        self._hook = None
        # 回调对象必须保持引用，否则被 GC 后钩子崩进程
        self._proc = HOOKPROC(self._hook_cb)
        self._evt_proc = WINEVENTPROC(self._winevent_cb)
        self._whook = None
        # 回调与主循环之间的原生槽位：回调里只读写它们
        self._slot = (ctypes.c_long * 7)()  # x, y, req, down_tick, open_tick, dis_x, dis_y
        self._panel = (ctypes.c_long * 4)()  # 面板矩形（主循环刷新，回调只读）
        self._rect = (ctypes.c_long * 4)()  # 命中矩形：平时=面板，菜单开着=菜单
        self._panel_hwnd = 0  # 面板句柄缓存（主循环刷新，回调只读，不进 Tcl）
        self._clsbuf = ctypes.create_unicode_buffer(256)  # 类名缓存（回调复用，不分配）
        self._mopen = (ctypes.c_long * 1)()  # 菜单是否开着（主循环写，回调读）
        self._menu = None
        self._evt = None
        self._stop = threading.Event()
        self._thread = None
        self._bound = False

    # ---------- 安装 / 卸载 ----------
    def install(self) -> bool:
        try:
            # 低级钩子(WH_MOUSE_LL)不注入进程，hMod 必须为 NULL（传 exe 句柄会失败）
            self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
            if not self._hook:
                return False
            self._evt = kernel32.CreateEventW(None, False, False, None)
            if not self._evt:
                self.uninstall()
                return False
            # 前台窗口变化（Alt+Tab 等无鼠标事件的切换）也要关菜单，
            # 否则菜单会以置顶态飘在别的窗口上。SKIPOWNPROCESS 跳过自己人。
            try:
                self._whook = user32.SetWinEventHook(
                    EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
                    self._evt_proc, 0, 0,
                    WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
            except Exception:
                self._whook = None
            self._refresh_rect()
            if not self._bound:
                try:
                    self.app.root.bind(_MENU_EVENT, self._on_menu_event)
                    self._bound = True
                except Exception:
                    pass
            self._stop.clear()
            self._thread = threading.Thread(target=self._waiter, daemon=True)
            self._thread.start()
            return True
        except Exception:
            return False

    def uninstall(self) -> None:
        try:
            self._dismiss()
        except Exception:
            pass
        try:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
        finally:
            self._hook = None
        try:
            if getattr(self, "_whook", None):
                user32.UnhookWinEvent(self._whook)
        except Exception:
            pass
        finally:
            self._whook = None
        try:
            self._stop.set()
            if self._evt:
                kernel32.SetEvent(self._evt)  # 唤醒 _waiter 让它退出
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=2)
        except Exception:
            pass
        finally:
            self._thread = None
        try:
            if self._evt:
                kernel32.CloseHandle(self._evt)
        except Exception:
            pass
        finally:
            self._evt = None

    # ---------- 钩子（极简） ----------
    def _armed(self) -> bool:
        """只在锁定时出手；未锁定走 Tk 事件。"""
        try:
            return bool(self.app._locked)
        except Exception:
            return False

    @staticmethod
    def _inside(r, x: int, y: int) -> bool:
        try:
            return r[2] > r[0] and r[0] <= x < r[2] and r[1] <= y < r[3]
        except Exception:
            return False

    def _hook_cb(self, nCode, wParam, lParam):
        try:
            if nCode == HC_ACTION:
                st = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                x, y = st.x, st.y
                s = self._slot
                menu_open = self._mopen[0]
                in_menu = self._inside(self._rect, x, y) if menu_open else False
                in_panel = self._inside(self._panel, x, y)
                now = kernel32.GetTickCount()
                if wParam == WM_RBUTTONDOWN:
                    if menu_open:
                        if in_menu:
                            pass  # 菜单内：菜单自己处理
                        elif self._armed() and in_panel and self._visible_at(x, y):
                            s[0], s[1] = x, y
                            s[2] = REQ_OPEN
                            s[3] = now
                            try:
                                if self._evt:
                                    kernel32.SetEvent(self._evt)
                            except Exception:
                                pass
                            return 1  # 面板上：关旧菜单开新菜单（_open 先 unpost）
                        else:
                            s[2] = REQ_DISMISS
                            s[5], s[6] = x, y
                            try:
                                if self._evt:
                                    kernel32.SetEvent(self._evt)
                            except Exception:
                                pass
                            # 放行，让这次点击正常生效
                    elif self._armed() and in_panel and self._visible_at(x, y):
                        s[0], s[1] = x, y
                        s[2] = REQ_OPEN
                        s[3] = now
                        try:
                            if self._evt:
                                kernel32.SetEvent(self._evt)
                        except Exception:
                            pass
                        return 1  # 吞掉按下（面板被盖住时不吞，直接放行）
                    else:
                        s[2] = REQ_NONE
                elif wParam == WM_RBUTTONUP:
                    if s[2] == REQ_OPEN:
                        return 1  # 配对的松开（菜单还没弹出来）：吞掉
                    if menu_open:
                        if in_menu:
                            pass  # 菜单项的松开：放行让命令触发
                        elif in_panel and (now - s[4]) % 2**32 < 500:
                            return 1  # 刚开菜单的手势松开：吞掉，避免自杀
                        else:
                            s[2] = REQ_DISMISS
                            s[5], s[6] = x, y
                            try:
                                if self._evt:
                                    kernel32.SetEvent(self._evt)
                            except Exception:
                                pass
                            # 放行
                elif wParam in (WM_LBUTTONDOWN, WM_MBUTTONDOWN):
                    if menu_open:
                        if not in_menu:
                            s[2] = REQ_DISMISS
                            s[5], s[6] = x, y
                            try:
                                if self._evt:
                                    kernel32.SetEvent(self._evt)
                            except Exception:
                                pass
                            # 放行+关菜单
                    else:
                        s[2] = REQ_NONE
        except Exception:
            pass
        try:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        except Exception:
            return 0

    def _winevent_cb(self, _hook, _event, _hwnd, _a, _b, _c, _d):
        # 前台切到别的进程：关菜单。与鼠标钩子同理，只写槽位+发事件。
        try:
            if self._mopen[0]:
                self._slot[2] = REQ_DISMISS
                try:
                    if self._evt:
                        kernel32.SetEvent(self._evt)
                except Exception:
                    pass
        except Exception:
            pass

    # ---------- 事件线程 -> 主循环 ----------
    def _waiter(self) -> None:
        while not self._stop.is_set():
            try:
                kernel32.WaitForSingleObject(self._evt, INFINITE)
            except Exception:
                break
            if self._stop.is_set():
                break
            try:
                self.app.root.event_generate(_MENU_EVENT, when="tail")
            except Exception:
                break

    def _on_menu_event(self, _event=None) -> None:
        try:
            req = self._slot[2]
            self._slot[2] = REQ_NONE
            print(f"[menu] event req={req} mopen={self._mopen[0]} dis=({self._slot[5]},{self._slot[6]})")
            if req == REQ_OPEN:
                self._open(int(self._slot[0]), int(self._slot[1]))
            elif req == REQ_DISMISS:
                self._dismiss()
        except Exception as e:
            print(f"[menu] event 异常: {e}")

    def _refresh_rect(self) -> None:
        """主循环内调用：刷新面板矩形与句柄；菜单没开时同步命中矩形。"""
        try:
            hwnd = self.app.get_hwnd()
            if hwnd:
                self._panel_hwnd = int(hwnd)
                user32.GetWindowRect(hwnd, self._panel)
                if not self._mopen[0]:
                    for i in range(4):
                        self._rect[i] = self._panel[i]
        except Exception:
            pass

    def _visible_at(self, x: int, y: int) -> bool:
        """回调内调用：该点真正露出来的是不是面板（被别的窗口盖住则否）。

        面板透明（WS_EX_TRANSPARENT），WindowFromPoint 会穿过它返回底下的窗口：
        - 链上遇到面板句柄 -> 可见
        - 一路到顶是桌面类（Progman/WorkerW/SHELLDLL_DefView）-> 可见（面板透出来）
        - 否则 -> 被盖住
        """
        try:
            target = self._panel_hwnd
            if not target:
                return False
            h = user32.WindowFromPoint(wintypes.POINT(x, y))
            cur = h
            for _ in range(64):
                if not cur:
                    break
                if cur == target:
                    return True
                nxt = user32.GetParent(cur)
                if not nxt or nxt == cur:
                    cur = cur if cur else h
                    break
                cur = nxt
            n = user32.GetClassNameW(cur, self._clsbuf, 256)
            if n > 0:
                return self._clsbuf.value in ("Progman", "WorkerW", "SHELLDLL_DefView")
            return False
        except Exception:
            return False

    # ---------- 菜单（自绘 Toplevel：tk.Menu 的原生窗口在壁纸层子窗口下关不掉） ----------
    def _open(self, x: int, y: int) -> None:
        app = self.app
        print(f"[menu] show ({x}, {y})")
        try:
            self._dismiss()
            root = app.root
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            try:
                win.attributes("-toolwindow", True)
            except Exception:
                pass
            win.configure(bg="#1e1e1e", bd=1, relief="solid")
            on = ops.autostart_enabled()
            btn_kw = dict(bg="#1e1e1e", fg="#e8e8e8",
                          activebackground="#3a3a3a", activeforeground="#ffffff",
                          relief="flat", bd=0, anchor="w", padx=12, pady=6,
                          font=("Microsoft YaHei", 10))
            b1 = tk.Button(win, text=f"{'☑' if on else '☐'} 开机启动", width=14, **btn_kw,
                           command=lambda: (app.toggle_autostart(), self._dismiss()))
            b1.pack(fill="x")
            b2 = tk.Button(win, text="退出", width=14, **btn_kw,
                           command=app.quit_panel)
            b2.pack(fill="x")
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            x = min(max(0, x), max(0, sw - w - 4))
            y = min(max(0, y), max(0, sh - h - 40))
            win.geometry(f"+{x}+{y}")
            win.update_idletasks()
            try:
                rx, ry = win.winfo_rootx(), win.winfo_rooty()
                rw, rh = win.winfo_width(), win.winfo_height()
                if rw > 1 and rh > 1:
                    self._rect[0], self._rect[1] = rx, ry
                    self._rect[2], self._rect[3] = rx + rw, ry + rh
            except Exception:
                pass
            self._menu = win
            self._mopen[0] = 1
            try:
                self._slot[4] = kernel32.GetTickCount()
            except Exception:
                pass
            try:
                # 兜底：10s 无人理会自动收（Alt+Tab 等无鼠标事件的切换，钩子看不到）
                win.after(10000, lambda w=win: self._dismiss_if(w))
            except Exception:
                pass
            try:
                win.bind("<Escape>", lambda _e: self._dismiss())
            except Exception:
                pass
            print("[menu] 菜单已弹出")
        except Exception as e:
            import traceback
            print(f"[menu] show 失败: {e}\n{traceback.format_exc()}", file=sys.stderr)
            self._dismiss()

    def _dismiss_if(self, win) -> None:
        """超时兜底：只有传入的还是当前菜单才关，避免误杀新菜单。"""
        try:
            if win is not None and self._menu is win:
                print("[menu] 超时自动关闭")
                self._dismiss()
        except Exception:
            pass

    def _dismiss(self) -> None:
        self._mopen[0] = 0
        m, self._menu = self._menu, None
        # 地毯式：把 root 下所有残留 Toplevel 全关（防跟踪丢失导致的幽灵菜单）
        strays = 0
        try:
            for w in list(self.app.root.winfo_children()):
                try:
                    if w.winfo_class() == "Toplevel" and w is not m:
                        w.destroy()
                        strays += 1
                except Exception:
                    pass
        except Exception:
            pass
        if m is not None:
            print(f"[menu] dismiss (strays={strays})")
            try:
                m.destroy()
            except Exception as e:
                print(f"[menu] destroy 失败: {e}")
        elif strays:
            print(f"[menu] dismiss strays={strays}")
        self._refresh_rect()

    # 兼容旧调用（未锁定时 Tk <Button-3> 直接进这里，主循环上下文安全）
    def show(self, x: int, y: int) -> None:
        self._open(x, y)
