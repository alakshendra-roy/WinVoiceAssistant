"""
overlay_widget.py

The floating "pill" overlay: a frameless, translucent, always-on-top widget
pinned to the top-center of the primary monitor. It never steals keyboard
focus from whatever app the user is working in.

States:
    idle       - small pill, pulsing mic glyph
    listening  - same, glow intensifies
    streaming  - pill widens to show the live interim transcript
    action     - a badge (e.g. "Opening Arc...") is shown, then fades back
"""

from __future__ import annotations

from enum import Enum, auto

from PyQt6.QtCore import Qt, QTimer, QRectF, QPropertyAnimation, QEasingCurve, pyqtProperty, pyqtSlot
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QFont, QFontMetrics
from PyQt6.QtWidgets import QWidget, QApplication

PILL_HEIGHT = 44
PILL_MIN_WIDTH = 140
PILL_MAX_WIDTH = 640
PILL_PADDING_X = 20
TOP_MARGIN = 14

BG_COLOR = QColor(20, 20, 25, int(255 * 0.88))
BORDER_COLOR = QColor(255, 255, 255, 22)
TEXT_COLOR = QColor(235, 235, 240, 255)
ACCENT_COLOR = QColor(120, 170, 255, 255)
ACTION_COLOR = QColor(120, 255, 170, 255)

ACTION_BADGE_LIFETIME_MS = 2200


class PillState(Enum):
    IDLE = auto()
    LISTENING = auto()
    STREAMING = auto()
    ACTION = auto()


class PillOverlay(QWidget):
    def __init__(self) -> None:
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        self._state = PillState.IDLE
        self._transcript = ""
        self._action_label = ""
        self._glow = 0.4  # 0..1, driven by the pulse animation

        self._font = QFont("Segoe UI", 11)
        self._font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)

        self._pulse_anim = QPropertyAnimation(self, b"glow")
        self._pulse_anim.setDuration(1400)
        self._pulse_anim.setStartValue(0.25)
        self._pulse_anim.setEndValue(0.9)
        self._pulse_anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._pulse_anim.setLoopCount(-1)
        self._pulse_anim.finished.connect(self._reverse_pulse)
        self._pulse_direction_up = True

        self._action_timer = QTimer(self)
        self._action_timer.setSingleShot(True)
        self._action_timer.timeout.connect(self._clear_action)

        self._resize_to_content()
        self._reposition()
        self._pulse_anim.start()

    # -- glow property (animated) -----------------------------------------

    def _get_glow(self) -> float:
        return self._glow

    def _set_glow(self, value: float) -> None:
        self._glow = value
        self.update()

    glow = pyqtProperty(float, _get_glow, _set_glow)

    def _reverse_pulse(self) -> None:
        # QPropertyAnimation with loopCount(-1) already loops start->end; we
        # ping-pong manually so the glow breathes in and out.
        start, end = self._pulse_anim.startValue(), self._pulse_anim.endValue()
        self._pulse_anim.setStartValue(end)
        self._pulse_anim.setEndValue(start)

    # -- public API (called from the intent/voice pipeline) ----------------

    @pyqtSlot(bool)
    def set_listening(self, listening: bool) -> None:
        self._state = PillState.LISTENING if listening else PillState.IDLE
        if not listening:
            self._transcript = ""
        self._resize_to_content()
        self.update()

    @pyqtSlot(str)
    def update_transcript(self, text: str) -> None:
        self._transcript = text
        self._state = PillState.STREAMING if text else PillState.LISTENING
        self._resize_to_content()
        self.update()

    @pyqtSlot(str)
    def show_action(self, label: str) -> None:
        self._action_label = label
        self._state = PillState.ACTION
        self._resize_to_content()
        self.update()
        self._action_timer.start(ACTION_BADGE_LIFETIME_MS)

    def _clear_action(self) -> None:
        self._state = PillState.STREAMING if self._transcript else PillState.IDLE
        self._resize_to_content()
        self.update()

    # -- layout --------------------------------------------------------

    def _content_text(self) -> str:
        if self._state == PillState.ACTION:
            return self._action_label
        if self._state in (PillState.STREAMING, PillState.LISTENING) and self._transcript:
            return self._transcript
        return "Listening..." if self._state == PillState.LISTENING else "●"

    def _resize_to_content(self) -> None:
        metrics = QFontMetrics(self._font)
        text = self._content_text()
        text_width = metrics.horizontalAdvance(text)
        width = max(PILL_MIN_WIDTH, min(PILL_MAX_WIDTH, text_width + PILL_PADDING_X * 2 + 28))
        self.resize(width, PILL_HEIGHT)
        self._reposition()

    def _reposition(self) -> None:
        screen = QApplication.primaryScreen().geometry()
        x = screen.x() + (screen.width() - self.width()) // 2
        y = screen.y() + TOP_MARGIN
        self.move(x, y)

    # -- painting --------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(rect, self.height() / 2, self.height() / 2)

        painter.fillPath(path, BG_COLOR)
        painter.setPen(BORDER_COLOR)
        painter.drawPath(path)

        self._paint_mic_glyph(painter)
        self._paint_text(painter)

    def _paint_mic_glyph(self, painter: QPainter) -> None:
        cx, cy = 24, self.height() / 2
        radius = 5 + (4 if self._state == PillState.LISTENING else 0) * self._glow

        color = ACTION_COLOR if self._state == PillState.ACTION else ACCENT_COLOR
        glow_color = QColor(color)
        glow_color.setAlpha(int(60 * self._glow) if self._state in (PillState.LISTENING, PillState.IDLE) else 200)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow_color)
        painter.drawEllipse(QRectF(cx - radius, cy - radius, radius * 2, radius * 2))

        painter.setBrush(color)
        painter.drawEllipse(QRectF(cx - 4, cy - 4, 8, 8))

    def _paint_text(self, painter: QPainter) -> None:
        painter.setFont(self._font)
        color = ACTION_COLOR if self._state == PillState.ACTION else TEXT_COLOR
        painter.setPen(color)
        text_rect = QRectF(44, 0, self.width() - 44 - PILL_PADDING_X, self.height())
        metrics = QFontMetrics(self._font)
        elided = metrics.elidedText(self._content_text(), Qt.TextElideMode.ElideLeft, int(text_rect.width()))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
