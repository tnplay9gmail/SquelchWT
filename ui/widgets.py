"""Reusable, keyboard-accessible avionics display controls."""
from PySide6.QtCore import Qt, Signal, QSignalBlocker, QEvent, QObject, QPoint
from PySide6.QtGui import QPainter, QColor, QLinearGradient
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QSlider, QFrame,
                             QHBoxLayout, QVBoxLayout, QComboBox)
from . import theme as T


def label(text='', size=None, color=None, muted=False):
    w = QLabel(text)
    w.setTextFormat(Qt.TextFormat.PlainText)
    if size:
        w.setFont(T.display_font(size))
    if color:
        w.setStyleSheet(f'color: {color};')
    w.setProperty('muted', muted)
    return w


def button(text, callback=None, tip='', checkable=False):
    w = QPushButton(text)
    w.setCheckable(checkable)
    w.setCursor(Qt.CursorShape.PointingHandCursor)
    w.setAccessibleName(text)
    help_tip(w, tip)
    if callback:
        w.clicked.connect(callback)
    return w


def set_power_state(widget, enabled):
    """Apply the operational ON/OFF color independent of the pressed state."""
    state = 'on' if enabled else 'off'
    if widget.property('powerState') == state:
        return
    widget.setProperty('powerState', state)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def help_tip(widget, text):
    widget.setToolTip('')
    widget.setProperty('helpText', text)
    widget.setAccessibleDescription(text)


class InstantTooltips(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.popup = QLabel(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowDoesNotAcceptFocus)
        self.popup.setTextFormat(Qt.TextFormat.PlainText)
        self.popup.setWordWrap(True)
        self.popup.setFont(T.display_font(13))
        self.popup.setMaximumWidth(360)
        self.popup.setStyleSheet(f'background: {T.BG}; color: {T.TEXT}; border: 1px solid {T.GREEN}; padding: 7px;')
        self.popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.popup.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.source = None

    def hide(self):
        self.source = None
        try:
            self.popup.hide()
        except RuntimeError:
            pass

    def eventFilter(self, obj, event):
        if isinstance(obj, QWidget):
            tip = obj.property('helpText')
            if event.type() == QEvent.Type.Enter and tip:
                self.source = obj
                self.popup.setText(tip)
                self.popup.adjustSize()
                self.popup.move(obj.mapToGlobal(QPoint(8, obj.height() + 4)))
                self.popup.show()
            elif obj is self.source and event.type() in (QEvent.Type.Leave, QEvent.Type.FocusOut,
                                                          QEvent.Type.MouseButtonPress, QEvent.Type.Hide):
                self.hide()
        return False


class MfdGlass(QWidget):
    """Subtle edge glass without scanlines over the display."""
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, event):
        painter = QPainter(self)
        edge = QLinearGradient(0, 0, 0, self.height())
        edge.setColorAt(0, QColor(0, 0, 0, 28))
        edge.setColorAt(.12, QColor(0, 0, 0, 0))
        edge.setColorAt(.88, QColor(0, 0, 0, 0))
        edge.setColorAt(1, QColor(0, 0, 0, 28))
        painter.fillRect(self.rect(), edge)


def panel():
    w = QFrame()
    w.setProperty('panel', True)
    return w


class Selector(QComboBox):
    """Wheel changes only after deliberate keyboard/click focus."""
    def __init__(self, values=()):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.addItems(values)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

    def sync(self, text):
        with QSignalBlocker(self):
            self.setCurrentText(text)


class Parameter(QFrame):
    changed = Signal(float)
    contextual = Signal(str)

    def __init__(self, name, lo, hi, step, fmt, tip='', compact=False):
        super().__init__()
        self.setProperty('panel', True)
        self.lo, self.hi, self.step, self.fmt = lo, hi, step, fmt
        self.tip = tip
        self.name = name
        box = QVBoxLayout(self)
        box.setContentsMargins(8, 3 if compact else 6, 8, 2 if compact else 6)
        box.setSpacing(0)
        row = QHBoxLayout()
        self.title = label(name, 11)
        row.addWidget(self.title)
        row.addStretch()
        self.value_label = label('', 14, T.CYAN)
        row.addWidget(self.value_label)
        box.addLayout(row)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        # Integer positions encode exact parameter steps, including fractional steps.
        self.slider.setRange(0, round((hi - lo) / step))
        self.slider.setSingleStep(1)
        self.slider.setPageStep(max(1, round((hi - lo) / step / 10)))
        self.slider.setAccessibleName(name)
        self.slider.valueChanged.connect(self._changed)
        self.slider.installEventFilter(self)
        box.addWidget(self.slider)
        help_tip(self, tip + f' Range: {lo} to {hi}. Step: {step}.')
        help_tip(self.slider, tip + f' Range: {lo} to {hi}. Step: {step}.')
        self.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Enter, QEvent.Type.FocusIn):
            self.contextual.emit(self.name + '  /  ' + self.tip)
        return super().eventFilter(obj, event)

    def _changed(self, position):
        value = round(self.lo + position * self.step, 4)
        self.value_label.setText(self.fmt.format(value))
        self.changed.emit(value)

    def sync(self, value):
        with QSignalBlocker(self.slider):
            self.slider.setValue(round((value - self.lo) / self.step))
        # A legacy preset may exceed the editable range (e.g. AM's 5000 Hz LP).
        # Display the real value without writing a clamped value to configuration.
        self.value_label.setText(self.fmt.format(value))
        self.slider.setAccessibleDescription(f'{self.tip} Current value {self.fmt.format(value)}.')


class Annunciator(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setProperty('annunciator', True)
        box = QVBoxLayout(self)
        box.setContentsMargins(5, 2, 5, 3)
        box.setSpacing(1)
        box.addWidget(label(title, 10, T.DIM))
        self.readout = label('', 16)
        box.addWidget(self.readout)

    def sync(self, text, color):
        self.readout.setText(text)
        self.readout.setStyleSheet(f'color: {color}')


class TitleBar(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setFixedHeight(30)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(label('SQUELCH', 20, T.TEXT))
        row.addWidget(label('WT', 20, T.CYAN))
        row.addWidget(label(' / COMM PROCESSOR', 10, T.DIM))
        row.addStretch()
        self.minimize = button('—', window.showMinimized, 'Minimize SquelchWT to the taskbar.')
        self.close = button('×', window.close, 'Close SquelchWT and save settings.')
        for w in (self.minimize, self.close):
            w.setProperty('windowControl', True)
            w.setFixedSize(28, 24)
            row.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
        self.close.setProperty('danger', True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window.instant_tooltips.hide()
            handle = self.window.windowHandle()
            if handle:
                handle.startSystemMove()
