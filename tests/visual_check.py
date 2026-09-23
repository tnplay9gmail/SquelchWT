"""Render all pages at the process's QT_SCALE_FACTOR without touching user settings.

Run as `python -m tests.visual_check` or `python tests/visual_check.py`.
Fixture traffic is explicitly synthetic and never enters speech queues.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPoint, QTimer, QEvent
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton, QComboBox,
                               QSlider, QDialog, QAbstractScrollArea)
import engine as E
from ui.window import MainWindow


def main():
    app = QApplication([])
    scale = os.environ.get('SQUELCH_QA_LABEL', os.environ.get('QT_SCALE_FACTOR', 'native'))
    output = Path('docs') / f'qa-{scale}'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, patch.object(E, 'CONFIG_PATH', Path(tmp) / 'config.json'):
        E.CONFIG_PATH.write_text(json.dumps(E.load_config()))
        window = MainWindow(False)
        window.show()
        issues = []

        def capture(name):
            app.processEvents()
            window.grab().save(str(output / (name + '.png')))
            for widget in window.findChildren(QWidget):
                if not isinstance(widget, (QLabel, QPushButton, QComboBox, QSlider)):
                    continue
                if not widget.isVisible():
                    continue
                ancestor = widget.parentWidget()
                inside_scroll_area = False
                while ancestor is not None:
                    if isinstance(ancestor, QAbstractScrollArea):
                        inside_scroll_area = True
                        break
                    ancestor = ancestor.parentWidget()
                if inside_scroll_area:
                    continue
                p = widget.mapTo(window, QPoint())
                if p.x() < 0 or p.y() < 0 or p.x() + widget.width() > window.width() or p.y() + widget.height() > window.height():
                    issues.append((name, type(widget).__name__, p.x(), p.y(), widget.width(), widget.height()))
                if isinstance(widget, QLabel) and not widget.wordWrap() and '\n' not in widget.text():
                    if widget.fontMetrics().horizontalAdvance(widget.text()) > widget.width() + 2:
                        issues.append((name, 'text-clipped', widget.text(), widget.width()))

        capture('comms-empty')
        window.service.connected = True
        for i, (sender, msg, enemy) in enumerate([
            ('VIPER 12', 'Need air cover at point B. Two aircraft inbound.', False),
            ('RAVEN 03', 'Returning to base.', True),
            ('ECHO 21', 'Copy. Holding at 2,000 meters; waiting for the next call.', False),
            ('FALCON 07', 'Attention to the designated grid square.', False),
        ]):
            window.add_record({'id': i, 'sender': sender, 'msg': msg, 'enemy': enemy, 'mode': 'All' if enemy else 'Team'})
        window.refresh()
        capture('comms-fixture')
        for i in range(4, 20):
            window.add_record({
                'id': i,
                'sender': f'FLIGHT {i:02d}',
                'msg': f'Overflow check message {i:02d}.',
                'enemy': i % 3 == 0,
                'mode': 'All' if i % 3 == 0 else 'Team',
            })
        app.processEvents()
        capture('comms-scroll')
        window.test_target.showPopup()
        app.processEvents()
        window.test_target.view().grab().save(str(output / 'test-target-menu.png'))
        window.test_target.hidePopup()
        for index, name in [(1, 'voice-neural'), (2, 'radio')]:
            window.navigate(index)
            capture(name)
        window.service.engine_mode = 'LOCAL'
        window.refresh()
        window.navigate(1)
        capture('voice-local')
        window.service.downloading = True
        window.service.download_target = window.service.piper_ally
        window.service.download_percent = 42
        window.refresh()
        capture('voice-local-download')
        window.service.downloading = False
        window.service.download_target = None
        window.refresh()
        window.navigate(3)
        for group in range(6):
            window.dsp_page.group.button(group).click()
            capture(f'dsp-{group}')
        window.dsp_page.group.button(4).click()
        window.show_fault('PLAYBACK ERROR / Check your Windows audio output.')
        capture('dsp-tx-fault')
        QApplication.sendEvent(window.test_button, QEvent(QEvent.Type.Enter))
        app.processEvents()
        window.instant_tooltips.popup.grab().save(str(output / 'tooltip.png'))
        window.instant_tooltips.hide()
        def capture_confirmation():
            dialog = window.findChild(QDialog)
            dialog.grab().save(str(output / 'confirm-reset.png'))
            dialog.reject()
        QTimer.singleShot(0, capture_confirmation)
        window.confirm('RESET ALL SETTINGS', 'Restore voices, engine, rate and every radio parameter?', 'RESET ALL')
        window.close()
        print(json.dumps({'scale': scale, 'device_pixel_ratio': window.devicePixelRatioF(),
                          'overflow': issues, 'output': str(output)}, indent=2))
        return bool(issues)


if __name__ == '__main__':
    sys.exit(main())
