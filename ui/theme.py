"""Semantic display tokens. All sizing is in Qt device-independent pixels."""
from PySide6.QtGui import QFont, QFontDatabase
from pathlib import Path
import sys

BG = '#000000'
PANEL = '#000000'
RAISED = '#071408'
TEXT = '#79ff8b'
DIM = '#55b767'
BORDER = '#225c30'
CYAN = '#a1ffad'
GREEN = '#79ff8b'
AMBER = '#79ff8b'
RED = '#ff4a4a'
DISABLED = '#245b2d'
ALLY = '#69b8ff'
ENEMY = RED
SPACE = 6
RADIUS = 0
LINE = 1


def display_font(size=14):
    families = QFontDatabase.families()
    family = next((f for f in families if f.lower() == 'hornet display'), 'Bahnschrift')
    font = QFont(family)
    font.setPixelSize(size)
    font.setBold(True)
    return font


def stylesheet():
    chevron = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / 'ui' / 'chevron.svg'
    return f'''
    QWidget {{ background: {BG}; color: {TEXT}; font-family: "Hornet Display"; font-weight: bold; font-size: 13px; }}
    QFrame[panel="true"] {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: {RADIUS}px; }}
    QFrame[annunciator="true"] {{ background: transparent; border: none; border-bottom: 1px solid {BORDER}; }}
    QLabel {{ background: transparent; border: none; }}
    QLabel[muted="true"] {{ color: {DIM}; }}
    QPushButton {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: {RADIUS}px;
                   padding: 5px 8px; color: {TEXT}; }}
    QPushButton:hover {{ background: {RAISED}; border-color: {DIM}; }}
    QPushButton:pressed {{ background: {BORDER}; }}
    QPushButton:checked {{ color: {CYAN}; background: {RAISED}; border-color: {CYAN}; }}
    QPushButton[powerState="on"] {{ color: {GREEN}; border-color: {GREEN}; }}
    QPushButton[powerState="off"] {{ color: {RED}; border-color: {RED}; background: {BG}; }}
    QPushButton[powerState="off"]:hover {{ background: #1a0000; border-color: {RED}; }}
    QPushButton[windowControl="true"] {{ padding: 0; }}
    QPushButton:focus, QComboBox:focus, QSlider:focus {{ border: 1px solid {CYAN}; }}
    QPushButton:disabled {{ color: {DISABLED}; border-color: {PANEL}; }}
    QPushButton[danger="true"] {{ color: {RED}; }}
    QComboBox {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 5px 8px;
                 selection-background-color: {RAISED}; min-height: 18px; }}
    QComboBox:hover {{ border-color: {CYAN}; }}
    QComboBox::drop-down {{ border: none; width: 25px; }}
    QComboBox::down-arrow {{ image: url("{chevron.as_posix()}"); width: 12px; height: 8px; }}
    QComboBox QAbstractItemView {{ background: {PANEL}; color: {TEXT};
        selection-background-color: {RAISED}; selection-color: {CYAN}; outline: none; padding: 4px; }}
    QSlider {{ background: transparent; min-height: 20px; border: 1px solid transparent; }}
    QSlider::groove:horizontal {{ height: 3px; background: {BORDER}; }}
    QSlider::sub-page:horizontal {{ background: {CYAN}; }}
    QSlider::handle:horizontal {{ background: {TEXT}; border: 1px solid {GREEN}; width: 9px;
                                margin: -5px 0; }}
    QSlider::handle:horizontal:hover {{ background: {CYAN}; }}
    QSlider::sub-page:horizontal:disabled {{ background: {DISABLED}; }}
    QProgressBar {{ background: {BG}; border: 1px solid {BORDER}; color: {TEXT}; text-align: center; min-height: 16px; }}
    QProgressBar::chunk {{ background: {GREEN}; }}
    QScrollArea, QStackedWidget {{ border: none; background: transparent; }}
    QScrollBar:vertical {{ background: {BG}; width: 11px; margin: 0; border-left: 1px solid {BORDER}; }}
    QScrollBar::handle:vertical {{ background: {DIM}; border: 1px solid {GREEN}; min-height: 34px; margin: 2px; }}
    QScrollBar::handle:vertical:hover {{ background: {GREEN}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QDialog {{ border: 1px solid {BORDER}; }}
    '''
