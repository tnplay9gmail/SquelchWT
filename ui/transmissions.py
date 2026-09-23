"""Bounded tactical communications log; timestamps indicate local receipt time."""
from datetime import datetime
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QGraphicsDropShadowEffect
from .widgets import label, panel, button, help_tip
from . import theme as T


class TransmissionLog(QWidget):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.total = 0
        self.connected = False
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(5)
        header = QHBoxLayout()
        self.clear_button = button('CLEAR', self.clear, 'Clear the list of received transmissions.')
        header.addWidget(self.clear_button)
        header.addWidget(label('TRAFFIC LOG', 14))
        self.count = label('00 RX', 10, T.DIM)
        header.addStretch()
        header.addWidget(self.count)
        box.addLayout(header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.stack = QVBoxLayout(self.body)
        self.stack.setContentsMargins(0, 0, 6, 0)
        self.stack.setSpacing(4)
        self.stack.setAlignment(Qt.AlignmentFlag.AlignBottom)
        self.scroll.setWidget(self.body)
        self.scroll.verticalScrollBar().rangeChanged.connect(
            lambda _minimum, maximum: self.scroll.verticalScrollBar().setValue(maximum)
        )
        box.addWidget(self.scroll, 1)
        self.empty = panel()
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.addStretch()
        self.empty_state = label('ACQUIRING', 28, T.RED)
        self.empty_detail = label('GAME CHAT UNAVAILABLE', 16, T.RED)
        for w in (self.empty_state, self.empty_detail):
            w.setAlignment(Qt.AlignmentFlag.AlignCenter)
            glow = QGraphicsDropShadowEffect(w)
            glow.setBlurRadius(24)
            glow.setOffset(0, 0)
            glow.setColor(QColor(255, 54, 54, 145))
            w.setGraphicsEffect(glow)
            empty_layout.addWidget(w)
        empty_layout.addStretch()
        box.addWidget(self.empty, 1)
        self.scroll.hide()
        self.blink_bright = True
        self.blink_timer = QTimer(self)
        self.blink_timer.setInterval(650)
        self.blink_timer.timeout.connect(self.blink)

    def blink(self):
        self.blink_bright = not self.blink_bright
        color = T.RED if self.blink_bright else '#993535'
        self.empty_state.setStyleSheet(f'color: {color}; font-size: 28px;')
        self.empty_detail.setStyleSheet(f'color: {color}; font-size: 17px;')
        for w in (self.empty_state, self.empty_detail):
            w.graphicsEffect().setColor(QColor(255, 54, 54, 145 if self.blink_bright else 50))

    def sync(self, connected):
        self.connected = connected
        self.empty_state.setText('STANDBY' if connected else 'NO LINK')
        self.empty_detail.setText('AWAITING TRAFFIC' if connected else 'GAME CHAT UNAVAILABLE')
        if connected or self.rows:
            self.blink_timer.stop()
            color = T.GREEN if connected else T.RED
            self.empty_state.setStyleSheet(f'color: {color}; font-size: 28px;')
            self.empty_detail.setStyleSheet(f'color: {T.DIM if connected else color}; font-size: 17px;')
            for w in (self.empty_state, self.empty_detail):
                w.graphicsEffect().setEnabled(not connected)
        elif not self.blink_timer.isActive():
            self.blink_bright = True
            self.empty_state.setStyleSheet(f'color: {T.RED}; font-size: 28px;')
            self.empty_detail.setStyleSheet(f'color: {T.RED}; font-size: 17px;')
            for w in (self.empty_state, self.empty_detail):
                w.graphicsEffect().setEnabled(True)
            self.blink_timer.start()

    def add(self, record):
        text = str(record.get('msg', '')).strip()
        if not text:
            return
        enemy = bool(record.get('enemy', False))
        color = T.ENEMY if enemy else T.ALLY
        card = panel()
        box = QVBoxLayout(card)
        box.setContentsMargins(8, 5, 8, 5)
        box.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        sender = label(str(record.get('sender') or 'SYSTEM'), 13, color)
        sender.setMaximumWidth(200)
        help_tip(sender, sender.text())
        top.addWidget(sender)
        top.addStretch()
        top.addWidget(label(('ENEMY' if enemy else 'ALLY') + '/' + str(record.get('mode') or 'CHAT').upper(), 10, color))
        top.addWidget(label(datetime.now().strftime('%H:%M'), 11, color))
        box.addLayout(top)
        message = label(text, color=color)
        message.setWordWrap(True)
        message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(message)
        self.stack.addWidget(card)
        self.rows.append(card)
        if len(self.rows) > 80:
            old = self.rows.pop(0)
            self.stack.removeWidget(old)
            old.deleteLater()
        self.total += 1
        self.count.setText(f'{self.total:02d} RX')
        self.empty.hide()
        self.blink_timer.stop()
        self.scroll.show()
        card.setStyleSheet(
            f'QFrame[panel="true"] {{ border: 1px solid {T.BORDER}; border-left: 3px solid {color}; }}'
        )

    def clear(self):
        for card in self.rows:
            self.stack.removeWidget(card)
            card.deleteLater()
        self.rows.clear()
        self.total = 0
        self.count.setText('00 RX')
        self.scroll.hide()
        self.empty.show()
        self.sync(self.connected)
