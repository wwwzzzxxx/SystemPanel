"""桌面面板控件：圆环 + 进度条，基于 tkinter，无外部依赖。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

_BG = "#1a1d24"
_CARD = "#232730"
_RING_BG = "#2e3440"
_TEXT = "#e5e7eb"
_MUTED = "#9aa4b2"
_ACCENT_CPU = "#4ea1ff"
_ACCENT_GPU = "#a78bfa"
_ACCENT_MEM = "#34d399"
_ACCENT_VRAM = "#f59e0b"
_ACCENT_DISK = "#fbbf24"
_ACCENT_NET = "#22d3ee"


def rounded_rect(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float,
                 r: float, fill: str, tag: str | None = None):
    """在 Canvas 上画真正的圆角矩形（两块矩形 + 四段圆角拼接）。

    Tk 没有原生圆角，这里用几何拼接近似；r 会自动收敛到可用范围。
    """
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    kw: dict = {"fill": fill, "outline": ""}
    if tag:
        kw["tags"] = tag
    if r <= 0.5:
        canvas.create_rectangle(x0, y0, x1, y1, **kw)
        return
    d = 2 * r
    # 中间十字（两块矩形）
    canvas.create_rectangle(x0 + r, y0, x1 - r, y1, **kw)
    canvas.create_rectangle(x0, y0 + r, x1, y1 - r, **kw)
    # 四个圆角
    canvas.create_oval(x0, y0, x0 + d, y0 + d, **kw)
    canvas.create_oval(x1 - d, y0, x1, y0 + d, **kw)
    canvas.create_oval(x0, y1 - d, x0 + d, y1, **kw)
    canvas.create_oval(x1 - d, y1 - d, x1, y1, **kw)


class RingWidget(tk.Canvas):
    """圆环：value 0..100，中心显示百分比与温度。"""

    def __init__(self, master, title: str, color: str, size: int = 130):
        super().__init__(master, width=size, height=size, bg=_BG, highlightthickness=0)
        self.title = title
        self.color = color
        self.size = size
        self._value: float | None = None
        self._temp: float | None = None
        self._thickness = 8
        self._draw()

    def set_value(self, value: float | None, temp: float | None = None):
        self._value = value
        self._temp = temp
        self._draw()

    def _draw(self):
        self.delete("all")
        cx, cy = self.size // 2, self.size // 2 + 6
        r = (self.size - self._thickness - 16) // 2

        # 背景环
        self.create_oval(cx - r, cy - r, cx + r, cy + r, outline=_RING_BG, width=self._thickness)

        # 进度环（从顶部开始，顺时针）
        if self._value is not None:
            v = max(0, min(100, self._value))
            # tkinter arc 是从 3 点开始逆时针，我们改成从 12 点顺时针
            extent = -v / 100 * 360
            # Tk 的 create_arc 存在两个退化值：
            #   extent == 0   （0%）  -> 什么都不画（空弧）
            #   extent == -360（100%）-> 首尾重合，同样不渲染
            # 注意 extent 会被截断为整数度：给 -0.5 会截成 -0 → 反而被当成整圆。
            # 因此分别替换成一个最小可见弧段，保证 0% 和 100% 都能看到蓝环。
            if extent == 0:
                extent = -1
            elif extent <= -360:
                extent = -359
            self.create_arc(
                cx - r, cy - r, cx + r, cy + r,
                start=90, extent=extent,
                outline=self.color, style="arc", width=self._thickness,
            )

        # 标题
        self.create_text(cx, 10, text=self.title, fill=_MUTED, font=("Microsoft YaHei", 9))

        # 中心数值
        if self._value is not None:
            self.create_text(cx, cy - 6, text=f"{self._value:.0f}%", fill=_TEXT, font=("Microsoft YaHei", 18, "bold"))
        else:
            self.create_text(cx, cy - 6, text="--", fill=_TEXT, font=("Microsoft YaHei", 14))

        # 温度
        if self._temp is not None:
            self.create_text(cx, cy + 16, text=f"{self._temp:.0f}°C", fill=_MUTED, font=("Microsoft YaHei", 9))


class BarRow(tk.Frame):
    """单行进度条：label + bar + 右侧值。

    进度条用 Canvas 绘制（圆角、可调粗细）。同一组内各行共用
    LABEL_WIDTH / VALUE_WIDTH，所以进度条的左右边界天然对齐。
    """

    # 左侧标签/右侧数值的固定字符宽度（保持组内对齐，可按需调整）
    LABEL_WIDTH = 10
    VALUE_WIDTH = 15
    # 进度条粗细（像素）
    BAR_HEIGHT = 10

    def __init__(self, master, label: str | None, color: str, bar_height: int | None = None,
                 bg: str = _BG, label_width: int | None = None,
                 value_width: int | None = None, min_fill_px: float = 1.0):
        super().__init__(master, bg=bg)
        self.color = color
        self._bg = bg
        self._sleeping = False
        self._pct: float | None = None
        self._min_fill = float(min_fill_px)
        self._bar_height = bar_height or self.BAR_HEIGHT

        # 左侧标签：固定宽度，防止挤压。
        # 标签短的行（如"内存"）可以传更小的 label_width，让进度条左移、变长。
        # label=None 表示这行不要标签（条铺满整行）。
        self.lab = tk.Label(self, text=label or "", bg=bg, fg=_MUTED, font=("Microsoft YaHei", 9),
                            width=label_width or self.LABEL_WIDTH, anchor="w")
        if label is not None:
            self.lab.pack(side="left", padx=(0, 6))

        # 进度条：Canvas 画圆角，width=1 避免它反过来撑宽整个面板
        self._bar = tk.Canvas(self, width=1, height=self._bar_height, bg=bg,
                              highlightthickness=0, bd=0)
        self._bar.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._bar.bind("<Configure>", lambda _e: self._redraw())

        # 右侧值：固定宽度，按各行实际需要传 value_width（过宽会白占进度条空间）
        self.val = tk.Label(self, text="--", bg=bg, fg=_TEXT, font=("Microsoft YaHei", 9),
                            width=value_width or self.VALUE_WIDTH, anchor="e")
        self.val.pack(side="right")

    def _rounded(self, x0: float, y0: float, x1: float, y1: float, r: float, fill: str):
        """进度条圆角：委托给模块级 rounded_rect。"""
        rounded_rect(self._bar, x0, y0, x1, y1, r, fill)

    def _redraw(self):
        c = self._bar
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return
        radius = h / 2

        # 轨道
        track = "#3a3f4b" if self._sleeping else _RING_BG
        self._rounded(0, 0, w, h, radius, track)

        # 填充：0% 不画；其余按真实比例，最小 1px，避免把 1% 画成 19%
        if self._sleeping or not self._pct:
            return
        fw = min(max(w * self._pct / 100, self._min_fill), w)
        self._rounded(0, 0, fw, h, radius, self.color)

    def set_text(self, text: str, pct: float | None, color: str | None = None):
        """无标签行：整行就是一根条 + 右侧数值（用于网速这种横着的框）。

        与 set_percent 不同，pct 只用来画填充，0 时整条仍可见（只有轨道）。
        """
        self._sleeping = False
        self._pct = None if pct is None else max(0.0, min(100.0, pct))
        if color is not None:
            self.color = color
        self.val.configure(text=text, fg=_TEXT)
        self._redraw()

    def set_percent(self, pct: float | None, suffix: str = "", sleeping: bool = False,
                    temp: float | None = None, value_text: str | None = None):
        """value_text: 覆盖默认的 "xx%" 文本（硬盘行用来显示速率）。"""
        self._sleeping = sleeping
        if sleeping:
            self._pct = None
            self.val.configure(text="休眠", fg=_MUTED)
            self.lab.configure(fg=_MUTED)
            self._redraw()
            return

        self.lab.configure(fg=_MUTED)
        self.val.configure(fg=_TEXT)

        if pct is None:
            self._pct = None
            self.val.configure(text="--")
        else:
            v = max(0, min(100, pct))
            self._pct = v
            # 显示格式：覆盖文本或百分比 + 后缀 + 温度（如果有）
            text = value_text if value_text is not None else f"{v:.0f}%{suffix}"
            if temp is not None:
                text += f" {temp:.0f}°C"
            self.val.configure(text=text)
        self._redraw()


class Section(tk.Frame):
    """圆角卡片：Canvas 画圆角背景，内容放在内缩的 body 里。

    body 内缩量 = 圆角半径，这样 body 的直角边始终落在圆角弧线以内，
    所以看不到任何直角。内容请加到 body（不是 Section 本身）。
    """

    RADIUS = 12

    def __init__(self, master, title: str | None = None, bg_card: str = _CARD, bg_out: str = _BG,
                 radius: int | None = None):
        super().__init__(master, bg=bg_out)
        self._card = bg_card
        self._radius = radius if radius is not None else self.RADIUS

        # 背景层：放最底下，画圆角卡片
        self._bg_canvas = tk.Canvas(self, bg=bg_out, highlightthickness=0, bd=0)
        self._bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._bg_canvas.bind("<Configure>", lambda _e: self._draw_bg())

        # 内容层：内缩 radius，决定 Section 的实际尺寸
        self.body = tk.Frame(self, bg=bg_card)
        self.body.pack(fill="both", expand=True, padx=self._radius, pady=self._radius)

        # title=None 表示不要标题（网络那条就是无标题的单行）
        self.title_label = None
        if title:
            self.title_label = tk.Label(self.body, text=title, bg=bg_card, fg=_MUTED,
                                        font=("Microsoft YaHei", 9), anchor="w")
            self.title_label.pack(fill="x", pady=(0, 8))

    def _draw_bg(self):
        c = self._bg_canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return
        rounded_rect(c, 0, 0, w, h, self._radius, self._card)

    def add_widget(self, w: tk.Widget):
        w.pack(fill="x", pady=2)
