"""面板右键菜单。

难点：上锁后面板是 WS_EX_TRANSPARENT，收不到任何鼠标事件。
做法：WH_MOUSE_LL 全局低级钩子，只做最少的事——命中时写原生槽位 + SetEvent，
工作线程等到事件后经 event_generate（Tk 官方跨线程通道）通知主循环。
钩子回调里不做任何 Python 堆 / Tcl / 打印操作（曾因此 fastfail 崩溃 3 次）。

钩子必须装在自己的专用线程上（`_pump`），不能装在 Tk 主线程：低级钩子的回调
有线程亲和性，系统把它派发到“安装钩子的那条线程”，并要求它尽快返回，否则
整个桌面的鼠标事件都会排队等待（LowLevelHooksTimeout 默认 300ms，超时还会把
钩子静默摘掉）。而主线程每 15s 要花 100~300ms 读硬盘温度（LHM SMART / WMI），
那期间钩子回调发不出去——用户看到的就是“每次温度刷新鼠标卡一下”。
`_pump` 线程只跑消息循环，永不执行采集/渲染，回调本身仍是微秒级。

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
WM_QUIT = 0x0012
WM_USER = 0x0400
PM_NOREMOVE = 0x0000
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
        # 钩子线程的消息循环（钩子回调只在这条线程上派发，必须自己泵消息）
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint, ctypes.c_ssize_t, ctypes.c_ssize_t]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
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
        # 专用钩子线程（低级钩子回调只在安装它的线程上派发，见模块 docstring）
        self._pump_thread = None
        self._pump_tid = 0
        self._pump_ready = threading.Event()

    # ---------- 安装 / 卸载 ----------
    def install(self) -> bool:
        try:
            self._evt = kernel32.CreateEventW(None, False, False, None)
            if not self._evt:
                return False
            # 主循环上下文（Tk 线程）：钩子线程只读这些原生槽位，不碰 Tcl
            self._refresh_rect()
            if not self._bound:
                try:
                    self.app.root.bind(_MENU_EVENT, self._on_menu_event)
                    self._bound = True
                except Exception:
                    pass
            self._stop.clear()
            self._pump_ready.clear()
            self._pump_thread = threading.Thread(
                target=self._pump, name="panel-hook-pump", daemon=True)
            self._pump_thread.start()
            # 等钩子装好：装失败/线程起不来都返回 False，调用方降级为只走 Tk 右键
            if not self._pump_ready.wait(timeout=5.0) or not self._hook:
                self.uninstall()
                return False
            self._thread = threading.Thread(target=self._waiter, daemon=True)
            self._thread.start()
            return True
        except Exception:
            return False

    def _pump(self) -> None:
        """钩子线程：装 WH_MOUSE_LL + 只泵消息，永不执行采集/渲染等慢活。

        低级钩子不注入进程，但回调有线程亲和性：系统把它派发到安装钩子的线程，
        该线程必须正在泵消息且立刻返回，否则整个桌面的鼠标事件都被拖住。主线程
        每 15s 会卡 100~300ms 读硬盘温度，所以钩子绝不能装在主线程上。
        """
        try:
            # 低级钩子(WH_MOUSE_LL)不注入进程，hMod 必须为 NULL（传 exe 句柄会失败）
            self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
            if not self._hook:
                return
            # 前台窗口变化（Alt+Tab 等无鼠标事件的切换）也要关菜单，
            # 否则菜单会以置顶态飘在别的窗口上。SKIPOWNPROCESS 跳过自己人。
            try:
                self._whook = user32.SetWinEventHook(
                    EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
                    self._evt_proc, 0, 0,
                    WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
            except Exception:
                self._whook = None
            try:
                self._pump_tid = int(kernel32.GetCurrentThreadId())
            except Exception:
                self._pump_tid = 0
            # 先强制建出消息队列，否则卸载时的 PostThreadMessage 会丢
            try:
                _pm = wintypes.MSG()
                user32.PeekMessageW(ctypes.byref(_pm), None, WM_USER, WM_USER, PM_NOREMOVE)
            except Exception:
                pass
            self._pump_ready.set()
            msg = wintypes.MSG()
            while not self._stop.is_set():
                if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) in (0, -1):
                    break  # WM_QUIT / 出错
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            print(f"[menu] 钩子线程异常: {e}", file=sys.stderr)
        finally:
            self._pump_ready.set()  # 起不来时别让 install 白等 5s
            self._unhook()

    def _unhook(self) -> None:
        """摘钩子（幂等）。低级钩子由安装线程卸最干净，主线程兜底也能摘掉。"""
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

    def uninstall(self) -> None:
        try:
            self._dismiss()
        except Exception:
            pass
        self._stop.set()
        # 让钩子线程的消息循环退出（它会在 finally 里自己卸钩）
        try:
            if self._pump_tid:
                user32.PostThreadMessageW(self._pump_tid, WM_QUIT, 0, 0)
        except Exception:
            pass
        try:
            if self._pump_thread and self._pump_thread.is_alive():
                self._pump_thread.join(timeout=2)
        except Exception:
            pass
        finally:
            self._pump_thread = None
            self._pump_tid = 0
        self._unhook()  # 兜底：线程没起来/超时没卸掉时这里再摘一次
        try:
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
            show_temp = app.temp_enabled()
            btn_kw = dict(bg="#1e1e1e", fg="#e8e8e8",
                          activebackground="#3a3a3a", activeforeground="#ffffff",
                          relief="flat", bd=0, anchor="w", padx=12, pady=6,
                          font=("Microsoft YaHei", 10))
            b1 = tk.Button(win, text=f"{'☑' if on else '☐'} 开机启动", width=14, **btn_kw,
                           command=lambda: (app.toggle_autostart(), self._dismiss()))
            b1.pack(fill="x")
            bt = tk.Button(win, text=f"{'☑' if show_temp else '☐'} 显示温度", width=14, **btn_kw,
                           command=lambda: (app.toggle_temp(), self._dismiss()))
            bt.pack(fill="x")
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
