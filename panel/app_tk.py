"""桌面面板：支持聚焦拖拽、失焦上锁与位置记忆。"""
from __future__ import annotations

import ctypes
import json
import math
import os
import sys
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont

from .collector import Metrics, collect, warmup
from . import collector as _collector
from . import ops
from .contextmenu import PanelMenu
from .widgets_tk import _BG, _CARD, _MUTED, _ACCENT_CPU, _ACCENT_GPU, _ACCENT_MEM, _ACCENT_VRAM, _ACCENT_DISK, _ACCENT_NET, BarRow, RingWidget, Section, rounded_rect

_INTERVAL_MS = 3000
_CONFIG_FILE = os.path.expanduser("~/.task_manager/panel_config.json")

# Tk Label 设了 width 后，固定宽 = width×7px，里面还有约 6px 内边距；
# 超出部分会被硬裁（切断半个字）。所以文本要按像素自己截断。
_LABEL_INSET_PX = 6


def fit_text_px(text: str, max_px: int, font) -> str:
    """把文本截断到 max_px 像素以内，超出部分用省略号收尾。

    直接靠 Tk Label 的 width 截断会把最后一个字切掉一半，所以这里按像素
    自己算。max_px<=0（布局尚未完成）时原样返回。
    """
    if max_px <= 0 or font.measure(text) <= max_px:
        return text
    ell = "…"
    out = ""
    for ch in text:
        if font.measure(out + ch + ell) > max_px:
            break
        out += ch
    out = out.rstrip()
    return (out + ell) if out else ell


def format_rate(bps: float | None) -> str:
    """把字节/秒格式化成紧凑文本，最长 6 字符（如 "1.2G/s"、"125M/s"）。

    控制长度是为了让硬盘行的数值列不用再变宽——列宽是按最坏字符串定的。
    """
    if bps is None:
        return "--"
    v = max(0.0, float(bps))
    if v >= 1024 ** 3:
        return f"{v / 1024 ** 3:.1f}G/s"
    if v >= 1024 ** 2:
        return f"{v / 1024 ** 2:.0f}M/s"
    if v >= 1024:
        return f"{v / 1024:.0f}K/s"
    return f"{v:.0f}B/s"


def format_bytes_rate(bps: float | None) -> str:
    """网速显示：二进制换算（GiB/MiB/KiB）+ 每秒后缀，如 "63M/s"、"1.2G/s"。

    上限 6 字符，和硬盘的 format_rate 同款，便于列宽复用。
    """
    if bps is None:
        return "--"
    v = max(0.0, float(bps))
    if v >= 1024 ** 3:
        return f"{v / 1024 ** 3:.1f}G/s"
    if v >= 1024 ** 2:
        return f"{v / 1024 ** 2:.0f}M/s"
    if v >= 1024:
        return f"{v / 1024:.0f}K/s"
    return f"{v:.0f}B/s"


_NET_LO = 16 * 1024            # 16 KiB/s 以下当作 0（背景噪声）
_NET_HI = 100 * 1024 * 1024    # 100 MiB/s 满格（千兆口单向上限约 118MB/s）


def _net_bar_pct(bps: float | None) -> float:
    """把字节/秒映射到 0..100 的条长（对数刻度）。

    线性刻度下，100Mbps 的日常流量（几 MB/s）在 1000Mbps 口上永远只是右端
    一个小点，看不出变化。所以用对数刻度：
      100KiB/s ≈ 22%，1MB/s ≈ 47%，10MB/s ≈ 73%，100MB/s 满格。
    数字本身始终是真实字节/秒，这里只决定条画多长。
    """
    if bps is None:
        return 0.0
    v = max(0.0, float(bps))
    if v <= _NET_LO:
        return 0.0
    frac = math.log(v / _NET_LO) / math.log(_NET_HI / _NET_LO)
    return max(0.0, min(100.0, frac * 100.0))


# 伪透明键值：root 背景用这个颜色，Tk 的 -transparentcolor 会把它变成全透明，
# 于是圆角之外的部分就"没有像素"，形成圆角外观。
_TRANS_KEY = "#010101"
# 外层圆角半径
OUTER_RADIUS = 14


@dataclass
class MonitorInfo:
    index: int
    x: int
    y: int
    width: int
    height: int
    is_primary: bool


def get_monitors() -> list[MonitorInfo]:
    """获取所有显示器信息，返回按位置排序的列表。"""
    monitors = []
    user32 = ctypes.windll.user32

    def callback(hmon, hdc, rect, data):
        r = ctypes.cast(rect, ctypes.POINTER(ctypes.c_long * 4)).contents
        monitors.append({
            'x': r[0], 'y': r[1], 'w': r[2] - r[0], 'h': r[3] - r[1]
        })
        return 1

    user32.EnumDisplayMonitors(None, None, ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(callback), 0)
    
    # 找出主显示器（x=0 且 y=0）
    primary_idx = None
    for i, m in enumerate(monitors):
        if m['x'] == 0 and m['y'] == 0:
            primary_idx = i
            break
    
    # 如果没找到，假设第一个为主显示器
    if primary_idx is None:
        primary_idx = 0
    
    # 主显示器放第一位，其他按 x 坐标排序
    result = []
    primary = monitors[primary_idx]
    result.append(MonitorInfo(
        index=1,
        x=primary['x'], y=primary['y'],
        width=primary['w'], height=primary['h'],
        is_primary=True
    ))
    
    # 其他显示器按 x 排序后编号
    others = [m for i, m in enumerate(monitors) if i != primary_idx]
    others.sort(key=lambda m: (m['x'], m['y']))
    
    for i, m in enumerate(others):
        result.append(MonitorInfo(
            index=i + 2,
            x=m['x'], y=m['y'],
            width=m['w'], height=m['h'],
            is_primary=False
        ))
    
    return result


def load_config() -> dict:
    """加载配置文件。"""
    try:
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {"monitor": 1}  # 默认显示器1


def save_config(cfg: dict):
    """保存配置文件。"""
    try:
        os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
        with open(_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def _set_wallpaper_embed(hwnd: int) -> bool:
    """尝试将窗口嵌入壁纸层，成功返回 True。"""
    try:
        import win32gui  # type: ignore
        import win32con  # type: ignore

        progman = win32gui.FindWindow("Progman", "Program Manager")
        if not progman:
            return False

        # 触发创建 WorkerW
        ctypes.windll.user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 1000, ctypes.byref(ctypes.c_ulong()))

        workerw = None

        def _enum(h, _):
            nonlocal workerw
            shell = win32gui.FindWindowEx(h, None, "SHELLDLL_DefView", None)
            if shell:
                nxt = win32gui.FindWindowEx(None, h, "WorkerW", None)
                if nxt:
                    workerw = nxt
            return True

        win32gui.EnumWindows(_enum, None)
        if workerw:
            win32gui.SetParent(hwnd, workerw)
            return True
    except Exception:
        pass
    return False


def _make_click_through(hwnd: int):
    """鼠标穿透（沉底后不可交互）。"""
    try:
        import win32gui, win32con  # type: ignore

        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        ex |= win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_TOOLWINDOW
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
        ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, int(255 * 0.92), 2)  # LWA_ALPHA=2
    except Exception:
        pass


class PanelApp:
    def __init__(self, monitor_index: int = 1):
        self.monitor_index = monitor_index
        self._cfg = load_config()
        self._locked = False  # 失焦后上锁
        self._dragging = False
        self._drag_offset = (0, 0)
        self._hwnd: int | None = None
        self._menu: PanelMenu | None = None

        self.root = tk.Tk()
        self.root.title("资源面板")
        self.root.overrideredirect(True)  # 无边框
        self.root.attributes("-transparentcolor", "#010101")  # 伪透明
        self.root.configure(bg="#010101")

        # DPI
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

        # 硬盘名截断用的字体度量（必须在 DPI 设置之后创建，否则度量不准）
        self._disk_font = tkfont.Font(family="Microsoft YaHei", size=9)

        # 外层圆角：Canvas 画圆角背景，圆角之外是 _TRANS_KEY -> 透出桌面
        self._outer_canvas = tk.Canvas(self.root, bg=_TRANS_KEY, highlightthickness=0, bd=0)
        self._outer_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._outer_canvas.bind("<Configure>", lambda _e: self._draw_outer())

        # 主容器：内缩 OUTER_RADIUS，直角边落在圆角弧线以内
        self.shell = tk.Frame(self.root, bg=_BG, padx=4, pady=4)
        self.shell.pack(fill="both", expand=True, padx=OUTER_RADIUS, pady=OUTER_RADIUS)

        # CPU/GPU 圆环
        rings = tk.Frame(self.shell, bg=_BG)
        rings.pack(fill="x", pady=(0, 12))
        self.cpu_ring = RingWidget(rings, "CPU", _ACCENT_CPU)
        self.cpu_ring.pack(side="left", padx=(0, 16))
        self.gpu_ring = RingWidget(rings, "GPU", _ACCENT_GPU)
        self.gpu_ring.pack(side="left")

        # 内存 / 显存（圆角卡片，行加到 body 上）
        # 列宽按各行实际内容定：
        #   内存/显存 label 只需放下两个字 -> label_width=4
        #   数值最长是 "100%  31.9/32G"(92px) -> value_width=14
        self.mem_sec = Section(self.shell, "内存 / 显存")
        self.mem_sec.pack(fill="x", pady=8)
        self.mem_row = BarRow(self.mem_sec.body, "内存", _ACCENT_MEM, bg=_CARD,
                              label_width=4, value_width=14)
        self.mem_row.pack(fill="x")
        self.vram_row = BarRow(self.mem_sec.body, "显存", _ACCENT_VRAM, bg=_CARD,
                               label_width=4, value_width=14)
        self.vram_row.pack(fill="x")

        # 网速 / 硬盘（合并成一张卡：第一行网速，下面是各块硬盘，标题顺序跟着内容走）
        #   网速行：无标签，条从卡片左内边铺满；"↓12M/s ↑134K/s" 最长 104px -> value_width=15
        #   硬盘行：盘名 "PVC 10 SK hyn (C:)" 需要 107px -> label_width=16；"685K/s 50°C" -> value_width=11
        self.disk_sec = Section(self.shell, "网速 / 硬盘")
        self.disk_sec.pack(fill="x", pady=8)

        # 必须先 pack 网速行，pack 顺序决定显示顺序（render 里不会再动它的 pack）
        self.net_row = BarRow(self.disk_sec.body, None, _ACCENT_NET, bg=_CARD,
                              value_width=15, min_fill_px=2.0)
        self.net_row.pack(fill="x", pady=(0, 4))

        self.disk_rows: list[BarRow] = []
        for i in range(4):
            row = BarRow(self.disk_sec.body, f"硬盘 {i+1}", _ACCENT_DISK, bg=_CARD,
                         label_width=16, value_width=11)
            row.pack(fill="x")
            self.disk_rows.append(row)

        # 提示
        self.hint = tk.Label(self.shell, text="以管理员运行可显示 CPU/硬盘温度", bg=_BG, fg=_MUTED, font=("Microsoft YaHei", 9))
        self.hint.pack(pady=(8, 0))

        # 初始定位：有记忆位置就用记忆，否则按显示器右下角
        self._place_initial()
        self.root.update()

        # 聚焦到最前，等待用户拖拽
        self._enter_interactive()

        # 保存 hwnd
        self.root.after(50, self._cache_hwnd)

    # ---------- 圆角外框 ----------
    def _draw_outer(self):
        c = self._outer_canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return
        rounded_rect(c, 0, 0, w, h, OUTER_RADIUS, _BG)

    # ---------- 定位 ----------
    def _place_initial(self):
        """优先用历史 x,y；否则按显示器右下角。"""
        cfg = self._cfg
        if "x" in cfg and "y" in cfg:
            try:
                x, y = int(cfg["x"]), int(cfg["y"])
                # 校验 x,y 在虚拟桌面内，否则回退
                if self._pos_on_any_monitor(x, y):
                    x, y = self._clamp_to_monitors(x, y)
                    self.root.geometry(f"+{x}+{y}")
                    print(f"[panel] 恢复历史位置: ({x}, {y})")
                    return
                else:
                    print(f"[panel] 历史位置 ({x},{y}) 不在任何显示器，丢弃")
            except Exception:
                pass
        # 否则按显示器右下角
        self._place_on_monitor(self.monitor_index)

    def _clamp_to_monitors(self, x: int, y: int) -> tuple[int, int]:
        """把左上角夹进所在显示器：加了新卡片后面板会变高，历史位置可能把它顶出屏幕。

        留 8px 边距，避免紧贴屏幕边缘（也尽量不压到任务栏）。
        """
        margin = 8
        try:
            self.root.update_idletasks()
            w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        except Exception:
            return x, y
        if w <= 1 or h <= 1:
            return x, y
        for m in get_monitors():
            if m.x <= x <= m.x + m.width and m.y <= y <= m.y + m.height:
                nx = min(max(x, m.x + margin), max(m.x + margin, m.x + m.width - w - margin))
                ny = min(max(y, m.y + margin), max(m.y + margin, m.y + m.height - h - margin))
                if (nx, ny) != (x, y):
                    print(f"[panel] 面板尺寸 {w}x{h}，位置夹回 ({nx}, {ny})")
                return nx, ny
        return x, y

    def _pos_on_any_monitor(self, x: int, y: int) -> bool:
        for m in get_monitors():
            if m.x - 400 <= x <= m.x + m.width and m.y - 400 <= y <= m.y + m.height:
                return True
        return False

    def _place_on_monitor(self, monitor_index: int):
        """将窗口定位到指定显示器的右下角。"""
        monitors = get_monitors()
        if not monitors:
            self._place_bottom_right()
            return
        
        target = None
        for m in monitors:
            if m.index == monitor_index:
                target = m
                break
        
        if target is None:
            target = monitors[0]
        
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        
        x = target.x + target.width - w - 24
        y = target.y + target.height - h - 24
        
        self.root.geometry(f"+{x}+{y}")
        print(f"[panel] 定位到显示器 {target.index}: ({x}, {y}) 尺寸 {target.width}x{target.height}")

    def _place_bottom_right(self):
        """回退：定位到主显示器右下角。"""
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - w - 24
        y = sh - h - 24
        self.root.geometry(f"+{x}+{y}")

    def _cache_hwnd(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                hwnd = self.root.winfo_id()
            self._hwnd = hwnd
        except Exception:
            pass

    # ---------- 交互：聚焦+可拖拽，失焦上锁 ----------
    def _enter_interactive(self):
        """启动时聚焦到最前，绑定拖拽与失焦上锁。"""
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.focus_force()
        except Exception:
            pass

        # 进入时面板始终在顶层可交互，不立即做 click-through / WorkerW
        # 拖拽：按住 shell 任意处移动
        self.shell.bind("<Button-1>", self._drag_start)
        self.shell.bind("<B1-Motion>", self._drag_move)
        self.shell.bind("<ButtonRelease-1>", self._drag_end)
        # 子控件也透传拖拽（递归，避免点到圆环/进度条/卡片内部就拖不动）
        self._bind_drag_tree(self.shell)

        # 首次失焦 -> 保存位置 + 沉到底层并上锁
        self.root.bind("<FocusOut>", self._on_first_focus_out)
        # 右键菜单：未锁定时走 Tk 事件（锁定时走全局钩子，见 contextmenu）
        self.root.bind("<Button-3>", self._on_right_click)
        # 兜底：如果启动时没抢到焦点（FocusOut 永不触发），N 秒后自动上锁，
        # 避免面板永远顶层可交互挡住别的窗口
        self.root.after(15000, self._auto_lock_if_never_focused)

    def _auto_lock_if_never_focused(self):
        if self._locked or self._dragging:
            return
        try:
            if self.root.focus_get() is not None:
                return  # 用户正在交互，等正常失焦流程
        except Exception:
            pass
        print("[panel] 启动后未获得焦点，自动上锁")
        self._save_position()
        self._force_lock()

    def _bind_drag_tree(self, widget):
        """递归给 widget 及其所有后代绑定拖拽，保证面板任意位置都能拖。"""
        for w in [widget, *widget.winfo_children()]:
            try:
                w.bind("<Button-1>", self._drag_start)
                w.bind("<B1-Motion>", self._drag_move)
                w.bind("<ButtonRelease-1>", self._drag_end)
            except Exception:
                pass
            for grand in w.winfo_children():
                self._bind_drag_tree(grand)

    def _drag_start(self, event):
        if self._locked:
            return
        self._dragging = True
        # 记录鼠标在窗口内的偏移
        try:
            wx, wy = self.root.winfo_x(), self.root.winfo_y()
            self._drag_offset = (event.x_root - wx, event.y_root - wy)
        except Exception:
            self._drag_offset = (0, 0)

    def _drag_move(self, event):
        if self._locked or not self._dragging:
            return
        try:
            ox, oy = self._drag_offset
            x = event.x_root - ox
            y = event.y_root - oy
            self.root.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _drag_end(self, event):
        if self._locked:
            return
        self._dragging = False
        # 拖完立即保存
        self._save_position()

    def _save_position(self):
        try:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            # 用 GetWindowRect 拿屏幕绝对坐标（Tk 的 winfo 在多屏下偶尔偏差）
            if self._hwnd:
                rect = (ctypes.c_long * 4)()
                ctypes.windll.user32.GetWindowRect(self._hwnd, rect)
                x, y = int(rect[0]), int(rect[1])
            self._cfg["x"] = int(x)
            self._cfg["y"] = int(y)
            # 同时记住 monitor，便于无历史时回退
            self._cfg["monitor"] = int(self.monitor_index)
            save_config(self._cfg)
            print(f"[panel] 已保存位置: ({x}, {y})")
        except Exception as e:
            print(f"[panel] 保存位置失败: {e}", file=sys.stderr)

    def _on_first_focus_out(self, event=None):
        if self._locked or self._dragging:
            return
        # Tk 的 FocusOut 会在子控件切换时也触发，用 after 去抖：若 250ms 内又获得焦点则不锁
        self.root.after(250, self._do_lock_if_still_unfocused)

    def _do_lock_if_still_unfocused(self):
        try:
            focused = self.root.focus_displayof()
        except Exception:
            focused = None
        # 若当前焦点仍在面板内，不锁
        if focused is not None:
            try:
                if str(focused) == str(self.root) or str(focused).startswith(str(self.root)):
                    return
            except Exception:
                pass
            # 更稳妥：若焦点 widget 的 toplevel 仍是本 root，不锁
            try:
                w = self.root.focus_get()
                if w is not None and str(w).startswith(str(self.root)):
                    return
            except Exception:
                pass
        if self._locked:
            return
        # 先保存最终位置
        self._save_position()
        self._force_lock()

    def _force_lock(self):
        """执行上锁（沉底 + 鼠标穿透）。调用前确保用户已放好位置。"""
        if self._locked:
            return
        self._locked = True
        print("[panel] 已上锁（沉底 + 鼠标穿透）")
        # 解绑拖拽与失焦事件
        try:
            self.shell.unbind("<Button-1>")
            self.shell.unbind("<B1-Motion>")
            self.shell.unbind("<ButtonRelease-1>")
            self.root.unbind("<FocusOut>")
        except Exception:
            pass
        # 取消置顶，沉到底层
        try:
            self.root.attributes("-topmost", False)
            # 放到最底层，接近壁纸
            ctypes.windll.user32.SetWindowPos(self._hwnd or self.root.winfo_id(), 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM, SWP_NOSIZE|NOMOVE|NOACTIVATE
        except Exception:
            pass
        # 鼠标穿透：上锁后不再拦截点击，点桌面图标正常
        try:
            hwnd = self._hwnd or ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            _make_click_through(hwnd)
        except Exception:
            pass

    def get_hwnd(self) -> int | None:
        """面板窗口句柄（缓存未就绪时回退到 winfo_id）。"""
        try:
            if self._hwnd:
                return self._hwnd
            return self.root.winfo_id()
        except Exception:
            return None

    def _on_right_click(self, event=None):
        """未锁定时的 Tk 右键（锁定时走全局钩子，不会到这里）。"""
        print("[menu] Tk 收到右键")
        if self._menu is None:
            return
        try:
            x = event.x_root if event is not None else self.root.winfo_x()
            y = event.y_root if event is not None else self.root.winfo_y()
        except Exception:
            x, y = self.root.winfo_x(), self.root.winfo_y()
        self._menu.show(int(x), int(y))
        return "break"

    def toggle_autostart(self):
        try:
            enable = not ops.autostart_enabled()
            ok, msg = ops.autostart_set(enable)
            print(f"[panel] 开机启动: {msg}")
        except Exception as e:
            print(f"[panel] 开机启动切换失败: {e}", file=sys.stderr)

    def quit_panel(self):
        """退出：先卸钩子（连带停事件线程）再关窗口。"""
        print("[panel] 退出")
        try:
            if self._menu is not None:
                self._menu.uninstall()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def render(self, m: Metrics):
        self.cpu_ring.set_value(m.cpu_load, m.cpu_temp)
        self.gpu_ring.set_value(m.gpu_load, m.gpu_temp)

        # 内存：用 GB 单位
        mem_suffix = f"  {m.mem_used_gb:.1f}/{m.mem_total_gb:.0f}G" if m.mem_used_gb else ""
        self.mem_row.set_percent(m.mem_percent, suffix=mem_suffix)

        # 显存：转换为 GB 单位
        if m.vram_used_mb and m.vram_total_mb:
            vram_used_gb = m.vram_used_mb / 1024
            vram_total_gb = m.vram_total_mb / 1024
            vram_suffix = f"  {vram_used_gb:.1f}/{vram_total_gb:.0f}G"
        else:
            vram_suffix = ""
        self.vram_row.set_percent(m.vram_percent, suffix=vram_suffix)

        # 硬盘：简化名称 + 显示温度
        for i, row in enumerate(self.disk_rows):
            if i < len(m.disks):
                d = m.disks[i]
                # 名字按标签可用像素截断（Tk 硬裁会切断半个字）
                simple_name = self._simplify_disk_name(d.name)
                avail = row.lab.winfo_reqwidth() - _LABEL_INSET_PX
                row.lab.configure(text=self._fit_px(simple_name, avail))
                if d.sleeping:
                    row.set_percent(None, sleeping=True)
                else:
                    # 条 = PDH 繁忙率；文字 = 读写合计速率（psutil 字节差分，NVMe 也准）
                    rate = (d.read_bps or 0.0) + (d.write_bps or 0.0)
                    row.set_percent(
                        d.busy if d.busy is not None else 0.0,
                        temp=d.temp,
                        value_text=format_rate(rate),
                    )
                row.pack(fill="x")
            else:
                row.pack_forget()

        # 网络：物理网卡合计吞吐（有线+无线），与 clash 是否开 TUN 无关。
        #   条画的是收发合计，文字给 ↓/↑ 明细。
        down, up = m.net_recv_bps, m.net_sent_bps
        total = None if (down is None and up is None) else (down or 0.0) + (up or 0.0)
        self.net_row.set_text(
            f"↓{format_bytes_rate(down)} ↑{format_bytes_rate(up)}",
            _net_bar_pct(total),
            color=_ACCENT_NET,
        )

        self.hint.pack_forget() if m.is_admin else self.hint.pack(pady=(8, 0))

    def _simplify_disk_name(self, name: str) -> str:
        """简化硬盘名称；保留盘符信息，截断时尽量不切断盘符括号。"""
        if not name:
            return "硬盘"
        name = name.strip()
        # 统一 PVC10 写法
        if "PVC10" in name:
            name = name.replace("PVC10", "PVC 10")
        if "Samsung SSD " in name:
            name = name.replace("Samsung SSD ", "")
        # 展示宽度约 18 字符，超出则在盘符前截断更友好
        if len(name) <= 18:
            return name
        # 若含盘符后缀 " (X:)"，保留它
        if " (" in name and name.endswith(")"):
            prefix, suffix = name.rsplit(" (", 1)
            suffix = " (" + suffix
            keep = 18 - len(suffix)
            if keep >= 8:
                return prefix[:keep].rstrip() + suffix
        return name[:18]

    def _fit_px(self, text: str, max_px: int) -> str:
        """把文本按像素截断（见模块级 fit_text_px）。"""
        return fit_text_px(text, max_px, self._disk_font)

    def run(self):
        warmup()

        # 右键菜单钩子（锁定时靠它，见 contextmenu）
        try:
            self.root.update_idletasks()
            self._cache_hwnd()
            self._menu = PanelMenu(self)
            if self._menu.install():
                print("[panel] 右键菜单钩子已安装")
            else:
                print("[panel] 右键菜单钩子安装失败（仅影响锁定时右键）", file=sys.stderr)
        except Exception as e:
            print(f"[panel] 右键菜单初始化失败: {e}", file=sys.stderr)

        # 首帧
        self._tick()
        # 3s 轮询
        self._schedule_tick()
        self.root.mainloop()

    def _tick(self):
        try:
            m = collect()
            _t = time.perf_counter()
            self.render(m)
            _render_dt = time.perf_counter() - _t
            _tim = _collector._last_timing
            _total = _tim.get("total", 0.0)
            # 卡顿看门狗：采集慢才打印，平时静默
            if _total > 0.3 or _render_dt > 0.1:
                print(f"[panel] 慢轮询: total={_total:.2f}s lhm={_tim.get('lhm', -1):.2f}s render={_render_dt:.2f}s")
        except Exception as e:
            print(f"[panel] collect failed: {e}", file=sys.stderr)

    def _schedule_tick(self):
        self.root.after(_INTERVAL_MS, self._tick_and_schedule)

    def _tick_and_schedule(self):
        self._tick()
        self._schedule_tick()


def main():
    # 日志 + 单实例（exe 无控制台，靠日志排错）
    log_path = ops.setup_logging()
    mutex = ops.ensure_single_instance()
    if mutex is None:
        print("[panel] 已有实例在运行，退出")
        sys.exit(0)

    # 加载配置
    cfg = load_config()
    monitor_index = cfg.get("monitor", 1)
    
    # 打印显示器信息
    monitors = get_monitors()
    print(f"[panel] 检测到 {len(monitors)} 个显示器:")
    for m in monitors:
        print(f"  显示器 {m.index}: {m.width}x{m.height} 位置({m.x}, {m.y}) {'[主]' if m.is_primary else ''}")
    print(f"[panel] 将显示在显示器 {monitor_index}")
    
    app = PanelApp(monitor_index=monitor_index)
    app.run()


if __name__ == "__main__":
    main()
