"""桌面背景常驻面板：3s 轮询，无交互。"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .collector import Metrics, collect, warmup
from .wallpaper import make_click_through, pin_to_wallpaper
from .widgets import _BG, _CARD, _MUTED, _TEXT, BarRow, RingWidget, Section

_INTERVAL_MS = 3000


class PanelWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnBottomHint | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # 鼠标穿透由 win32 WS_EX_TRANSPARENT 保障，这里不拦截
        self.setFocusPolicy(Qt.NoFocus)
        self.setWindowTitle("资源面板")

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        shell = QWidget()
        shell.setStyleSheet(f"background:{_BG}; border-radius:16px; border:1px solid #2a303c;")
        lay = QVBoxLayout(shell)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)

        # 顶部：CPU/GPU 圆环
        rings = QHBoxLayout()
        rings.setSpacing(12)
        self.cpu_ring = RingWidget("CPU", "#4ea1ff")
        self.gpu_ring = RingWidget("GPU", "#a78bfa")
        rings.addWidget(self.cpu_ring)
        rings.addWidget(self.gpu_ring)
        lay.addLayout(rings)

        # 内存 / 显存
        mem_sec = Section("内存 / 显存")
        self.mem_row = BarRow("内存", "#34d399")
        self.vram_row = BarRow("显存", "#f59e0b")
        mem_sec.add_widget(self.mem_row)
        mem_sec.add_widget(self.vram_row)
        lay.addWidget(mem_sec)

        # 硬盘
        self.disk_section = Section("硬盘")
        self.disk_rows: list[BarRow] = []
        self.disk_sub_labels: list[QLabel] = []
        # 预创建 3 行，用可见性控制
        for i in range(4):
            row = BarRow(f"硬盘 {i+1}", "#fbbf24")
            sub = QLabel("")
            sub.setStyleSheet(f"color:{_MUTED}; font-size:10px;")
            sub.setVisible(False)
            row.setVisible(False)
            sub.setVisible(False)
            self.disk_rows.append(row)
            self.disk_sub_labels.append(sub)
            self.disk_section.lay.addWidget(row)
            self.disk_section.lay.addWidget(sub)
        self.disk_section.setVisible(False)
        lay.addWidget(self.disk_section)

        self.hint = QLabel("以管理员运行可显示 CPU/硬盘温度")
        self.hint.setStyleSheet(f"color:{_MUTED}; font-size:10px;")
        self.hint.setAlignment(Qt.AlignHCenter)
        lay.addWidget(self.hint)

        root.addWidget(shell)
        self.setFixedWidth(360)

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        # 延迟到句柄创建后再嵌入壁纸层
        QTimer.singleShot(120, self._pin)

    def _pin(self):
        try:
            hwnd = int(self.winId())
            ok = pin_to_wallpaper(hwnd)
            if not ok:
                make_click_through(hwnd)
        except Exception:
            pass
        self._place_bottom_right()

    def _place_bottom_right(self):
        scr = QApplication.primaryScreen()
        if not scr:
            return
        geo = scr.availableGeometry()
        x = geo.right() - self.width() - 24
        y = geo.bottom() - self.height() - 24
        self.move(max(geo.left(), x), max(geo.top(), y))

    def render_metrics(self, m: Metrics):
        self.cpu_ring.set_value(m.cpu_load, m.cpu_temp)
        self.gpu_ring.set_value(m.gpu_load, m.gpu_temp)
        self.mem_row.set_percent(m.mem_percent, suffix=f"  {m.mem_used_gb:.1f}/{m.mem_total_gb:.1f}GB" if m.mem_used_gb else "")
        if m.vram_percent is not None:
            self.vram_row.set_percent(m.vram_percent, suffix=f"  {m.vram_used_mb:.0f}/{m.vram_total_mb:.0f}MB" if m.vram_used_mb else "")
        else:
            self.vram_row.set_percent(None, sleeping=False)
            self.vram_row.set_text_value("--")

        # 硬盘
        if m.disks:
            self.disk_section.setVisible(True)
            for i, row in enumerate(self.disk_rows):
                if i < len(m.disks):
                    d = m.disks[i]
                    row.lab.setText(d.name[:18])
                    if d.sleeping:
                        row.set_percent(None, sleeping=True)
                    else:
                        row.set_percent(d.busy if d.busy is not None else 0.0)
                    row.setVisible(True)
                    sub = self.disk_sub_labels[i]
                    if d.sleeping:
                        sub.setText("休眠中 · 已跳过检测")
                        sub.setVisible(True)
                    elif d.temp is not None:
                        sub.setText(f"{d.temp:.0f}°C")
                        sub.setVisible(True)
                    else:
                        sub.setVisible(False)
                else:
                    row.setVisible(False)
                    self.disk_sub_labels[i].setVisible(False)
        else:
            self.disk_section.setVisible(False)

        self.hint.setVisible(not m.is_admin)
        self.adjustSize()
        self._place_bottom_right()


def main():
    # DPI
    try:
        ctypes = __import__("ctypes")
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    warmup()

    app = QApplication(sys.argv)
    w = PanelWindow()
    w.show()

    # 3s 轮询（主线程 QTimer，不卡 UI）
    def tick():
        try:
            m = collect()
            w.render_metrics(m)
        except Exception as e:
            print(f"[panel] collect failed: {e}", file=sys.stderr)

    # 首帧立即刷一次，避免 3s 空白
    tick()
    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(_INTERVAL_MS)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
