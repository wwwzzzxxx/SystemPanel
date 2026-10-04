"""桌面面板控件：圆环 + 进度条，无交互。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

_BG = "#1a1d24"
_CARD = "#232730"
_RING_BG = "#2e3440"
_ACCENT_CPU = "#4ea1ff"
_ACCENT_GPU = "#a78bfa"
_ACCENT_MEM = "#34d399"
_ACCENT_VRAM = "#f59e0b"
_ACCENT_DISK = "#fbbf24"
_TEXT = "#e5e7eb"
_MUTED = "#9aa4b2"


class RingWidget(QWidget):
    """圆环：value 0..100，中心显示百分比与温度。"""

    def __init__(self, title: str, color: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.color = color
        self._value: float | None = None
        self._temp: float | None = None
        self.setMinimumSize(128, 128)
        self.setMaximumSize(148, 148)

    def set_value(self, value: float | None, temp: float | None = None):
        self._value = value
        self._temp = temp
        self.update()

    def paintEvent(self, event):  # type: ignore[override]
        w, h = self.width(), self.height()
        size = min(w, h) - 18
        rect = QRectF((w - size) / 2, 12, size, size)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # 背景环
        pen_bg = QPen(QColor(_RING_BG), 10)
        pen_bg.setCapStyle(Qt.RoundCap)
        p.setPen(pen_bg)
        p.drawArc(rect, 0 * 16, 360 * 16)

        # 进度环
        if self._value is not None:
            v = max(0.0, min(100.0, float(self._value)))
            pen = QPen(QColor(self.color), 10)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            # 起点 90°（顶部），顺时针：Qt 以 3 点为 0°，逆时针为正
            p.drawArc(rect, 90 * 16, -int(v / 100 * 360) * 16)

        # 标题（低对比，不抢占环）
        p.setPen(QColor(_MUTED))
        f = QFont("Microsoft YaHei", 8)
        p.setFont(f)
        p.drawText(QRectF(0, 2, w, 16), Qt.AlignHCenter | Qt.AlignVCenter, self.title)

        # 中心数值
        p.setPen(QColor(_TEXT))
        if self._value is not None:
            bf = QFont("Microsoft YaHei", 16, QFont.Bold)
            p.setFont(bf)
            p.drawText(QRectF(0, rect.center().y() - 14, w, 28), Qt.AlignHCenter | Qt.AlignVCenter, f"{self._value:.0f}%")
        else:
            bf = QFont("Microsoft YaHei", 12)
            p.setFont(bf)
            p.drawText(QRectF(0, rect.center().y() - 14, w, 28), Qt.AlignHCenter | Qt.AlignVCenter, "--")

        # 温度副标题（None 时隐藏，不写 --）
        if self._temp is not None:
            p.setPen(QColor(_MUTED))
            sf = QFont("Microsoft YaHei", 8)
            p.setFont(sf)
            p.drawText(QRectF(0, rect.center().y() + 16, w, 16), Qt.AlignHCenter | Qt.AlignVCenter, f"{self._temp:.0f}°C")
        p.end()


class BarRow(QWidget):
    """单行进度条：label + bar + 右侧值/状态。"""

    def __init__(self, label: str, color: str, parent=None):
        super().__init__(parent)
        self.color = color
        self._sleeping = False
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        self.lab = QLabel(label)
        self.lab.setStyleSheet(f"color:{_MUTED}; font-size:11px;")
        self.lab.setMinimumWidth(64)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        self.bar.setStyleSheet(
            f"QProgressBar{{border:none; background:{_RING_BG}; border-radius:5px;}}"
            f"QProgressBar::chunk{{background:{color}; border-radius:5px;}}"
        )
        self.val = QLabel("--")
        self.val.setStyleSheet(f"color:{_TEXT}; font-size:11px;")
        self.val.setMinimumWidth(72)
        self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        outer.addWidget(self.lab)
        outer.addWidget(self.bar, 1)
        outer.addWidget(self.val)

    def set_percent(self, pct: float | None, suffix: str = "", sleeping: bool = False):
        self._sleeping = sleeping
        if sleeping:
            self.bar.setValue(0)
            self.bar.setStyleSheet(
                f"QProgressBar{{border:none; background:{_RING_BG}; border-radius:5px;}}"
                f"QProgressBar::chunk{{background:#3a3f4b; border-radius:5px;}}"
            )
            self.val.setText("休眠")
            self.val.setStyleSheet(f"color:{_MUTED}; font-size:11px;")
            self.setStyleSheet("opacity: 0.7;")
            return
        self.bar.setStyleSheet(
            f"QProgressBar{{border:none; background:{_RING_BG}; border-radius:5px;}}"
            f"QProgressBar::chunk{{background:{self.color}; border-radius:5px;}}"
        )
        self.val.setStyleSheet(f"color:{_TEXT}; font-size:11px;")
        self.setStyleSheet("")
        if pct is None:
            self.bar.setValue(0)
            self.val.setText("--")
        else:
            v = int(max(0, min(100, pct)))
            self.bar.setValue(v)
            self.val.setText(f"{v}%{suffix}")

    def set_text_value(self, text: str):
        self.val.setText(text)


class Section(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"QFrame{{background:{_CARD}; border-radius:12px;}}")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(12, 10, 12, 12)
        self.lay.setSpacing(8)
        lab = QLabel(title)
        lab.setStyleSheet(f"color:{_MUTED}; font-size:10px; letter-spacing:0.5px;")
        self.lay.addWidget(lab)

    def add_widget(self, w: QWidget):
        self.lay.addWidget(w)
