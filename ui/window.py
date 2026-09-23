"""Fixed-size Qt glass cockpit; all worker delivery uses queued Qt signals."""
import os
import sys
import time
from PySide6.QtCore import QObject, Signal, Slot, Qt, QTimer, QEvent
from PySide6.QtGui import QShortcut, QKeySequence, QColor, QIcon
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QStackedWidget, QButtonGroup, QDialog)
import engine as E
from . import theme as T
from .widgets import (label, button, panel, Selector, Parameter, Annunciator,
                      TitleBar, InstantTooltips, MfdGlass, help_tip, set_power_state)
from .transmissions import TransmissionLog
from .settings import VoicePage, RadioPage, DspPage
from .help_text import TIPS


class Dispatcher(QObject):
    posted = Signal(object, object)

    def __init__(self, parent):
        super().__init__(parent)
        self.active = True
        self.posted.connect(self.deliver, Qt.ConnectionType.QueuedConnection)

    @Slot(object, object)
    def deliver(self, fn, args):
        if self.active:
            fn(*args)


class MainWindow(QMainWindow):
    def __init__(self, start_workers=True):
        super().__init__()
        self.setWindowTitle('SquelchWT — Communications')
        self.setWindowIcon(QIcon(str(E.ASSET_DIR / 'radio.ico')))
        # A real top-level Window retains the native taskbar and Alt-Tab identity.
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowMinimizeButtonHint | Qt.WindowType.WindowCloseButtonHint)
        self.setFixedSize(700, 700)
        self.setFont(T.display_font(12))
        self.setStyleSheet(T.stylesheet())
        self.instant_tooltips = InstantTooltips(self)
        QApplication.instance().installEventFilter(self.instant_tooltips)
        self.dispatcher = Dispatcher(self)
        self.service = E.RadioService(self.dispatcher.posted.emit, self.refresh, self.add_record, self.show_fault)
        self._closing = False
        self._last_receive = 0
        self._last_poll_state = None
        self._fault = ''
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(350)
        self.save_timer.timeout.connect(self.save)
        root = QWidget()
        self.setCentralWidget(root)
        self.glass = MfdGlass(root)
        root.installEventFilter(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(6)
        self.titlebar = TitleBar(self)
        layout.addWidget(self.titlebar)
        strip = QHBoxLayout()
        strip.setSpacing(5)
        self.link = Annunciator('01 / GAME CHAT')
        self.receive = Annunciator('02 / TRAFFIC')
        self.audio = Annunciator('03 / AUDIO OUTPUT')
        self.engine = Annunciator('04 / VOICE ENGINE')
        for annunciator in (self.link, self.receive, self.audio, self.engine):
            strip.addWidget(annunciator, 1)
        help_tip(self.link, TIPS['status_pill'])
        help_tip(self.receive, 'RECEIVING means game chat arrived recently; STANDBY means no recent traffic.')
        help_tip(self.audio, 'READY, SYNTHESIZING, SPEAKING or MUTED. Game-chat connection is shown independently.')
        layout.addLayout(strip)
        self.alert = panel()
        alerts = QHBoxLayout(self.alert)
        alerts.setContentsMargins(7, 1, 3, 1)
        self.alert_text = label('', color=T.RED)
        alerts.addWidget(self.alert_text, 1)
        alerts.addWidget(button('ACK', self.ack_fault, 'Acknowledge this notice. Technical details remain in squelchwt.log.'))
        layout.addWidget(self.alert)
        self.alert.hide()
        navigation = QHBoxLayout()
        navigation.setSpacing(4)
        self.nav = QButtonGroup(self)
        self.pages = QStackedWidget()
        names = ('COMMS', 'VOICE', 'RADIO', 'DSP')
        for index, name in enumerate(names):
            navigation_tips = (TIPS['btn_back'], TIPS['btn_settings_nav'], TIPS['btn_adv_back'], TIPS['btn_adv'])
            tab = button(f'{index + 1:02}  {name}', lambda checked, i=index: self.navigate(i), navigation_tips[index], checkable=True)
            tab.setFont(T.display_font(12))
            self.nav.addButton(tab, index)
            navigation.addWidget(tab)
        layout.addLayout(navigation)
        work = QHBoxLayout()
        work.setSpacing(10)
        self.log = TransmissionLog()
        self.voice_page = VoicePage(self)
        self.radio_page = RadioPage(self)
        self.dsp_page = DspPage(self)
        for page in (self.log, self.voice_page, self.radio_page, self.dsp_page):
            self.pages.addWidget(page)
        work.addWidget(self.pages, 1)
        self.console = self.build_console()
        work.addWidget(self.console)
        layout.addLayout(work, 1)
        footer = QHBoxLayout()
        self.persistence = label('SAVED', 11, T.DIM)
        footer.addStretch()
        footer.addWidget(self.persistence)
        layout.addLayout(footer)
        self.glass.raise_()
        self.navigate(0)
        self.refresh()
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(E.POLL_MS)
        self.poll_timer.timeout.connect(self.service.poll)
        self.instrument_timer = QTimer(self)
        self.instrument_timer.setInterval(200)
        self.instrument_timer.timeout.connect(self.refresh_instruments)
        self.instrument_timer.start()
        QShortcut(QKeySequence('Alt+F4'), self, activated=self.close)
        if start_workers:
            self.service.start()
            self.poll_timer.start()
            QTimer.singleShot(0, self.service.poll)

    def build_console(self):
        console = QWidget()
        console.setFixedWidth(205)
        box = QVBoxLayout(console)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        box.addWidget(label('ACTIVE PROFILE', 11, T.DIM))
        self.profile = Selector(list(E.PROFILES) + ['Custom'])
        self.profile.setFont(T.display_font(14))
        self.profile.activated.connect(lambda: self.service.select_profile(self.profile.currentText()))
        help_tip(self.profile, TIPS['cb_profile'])
        box.addWidget(self.profile)
        self.profile_description = label('', muted=True)
        self.profile_description.setWordWrap(True)
        self.profile_description.setMinimumHeight(42)
        box.addWidget(self.profile_description)
        self.volume = Parameter('VOLUME', 0, 1.5, .01, '{:.2f}', TIPS['volume_main_scale'])
        self.volume.changed.connect(lambda value: self.change_radio('volume', value, custom=False))
        box.addWidget(self.volume)
        speed = panel()
        speed_box = QVBoxLayout(speed)
        speed_box.setContentsMargins(8, 5, 8, 5)
        speed_header = QHBoxLayout()
        speed_header.addWidget(label('SPEECH RATE', 11))
        speed_header.addStretch()
        self.rate_value = label('', 15, T.CYAN)
        speed_header.addWidget(self.rate_value)
        speed_box.addLayout(speed_header)
        controls = QHBoxLayout()
        self.slower = button('− SLOWER', lambda: self.service.change_rate(-.1), TIPS['btn_speed_lo'])
        self.faster = button('+ FASTER', lambda: self.service.change_rate(.1), TIPS['btn_speed_hi'])
        controls.addWidget(self.slower)
        controls.addWidget(self.faster)
        speed_box.addLayout(controls)
        box.addWidget(speed)
        self.master = button('RADIO / ON', lambda checked: self.change_radio('enabled', checked, custom=False), TIPS['btn_fx_master'], True)
        box.addWidget(self.master)
        self.stage_status = label('', 10, T.DIM)
        self.stage_status.setWordWrap(True)
        box.addWidget(self.stage_status)
        box.addWidget(label('VOICE TEST', 11, T.DIM))
        self.test_target = Selector(['ALLY', 'ENEMY'])
        self.test_target.setItemData(0, QColor(T.ALLY), Qt.ItemDataRole.ForegroundRole)
        self.test_target.setItemData(1, QColor(T.ENEMY), Qt.ItemDataRole.ForegroundRole)
        self.test_target.currentTextChanged.connect(self.sync_test_target_color)
        help_tip(self.test_target, 'Choose the allied or enemy voice to audition in the current engine.')
        box.addWidget(self.test_target)
        self.sync_test_target_color()
        self.test_button = button('TEST VOICE', self.test_voice, 'Audition the selected voice through the current radio settings.')
        box.addWidget(self.test_button)
        box.addStretch()
        self.mute = button('MUTE OUTPUT', self.service.toggle_mute, TIPS['btn_mute'], True)
        self.mute.setMinimumHeight(30)
        self.mute.setFont(T.display_font(13))
        box.addWidget(self.mute)
        actions = QHBoxLayout()
        actions.setSpacing(4)
        actions.addWidget(button('RESTART', self.restart, TIPS['btn_restart']))
        reset = button('RESET', self.reset_settings, TIPS['btn_reset_all'])
        reset.setProperty('danger', True)
        actions.addWidget(reset)
        box.addLayout(actions)
        return console

    def test_voice(self):
        s = self.service
        enemy = self.test_target.currentText() == 'ENEMY'
        voice = getattr(s, ('neural_' if s.engine_mode == 'NEURAL' else 'piper_') + ('enemy' if enemy else 'ally'))
        s._test_voice(s.engine_mode, voice=voice, enemy=enemy)

    def sync_test_target_color(self):
        color = T.ALLY if self.test_target.currentText() == 'ALLY' else T.ENEMY
        self.test_target.setStyleSheet(f'color: {color};')

    def eventFilter(self, obj, event):
        if obj is self.centralWidget() and event.type() == QEvent.Type.Resize:
            self.glass.setGeometry(obj.rect())
            self.glass.raise_()
        return super().eventFilter(obj, event)

    def navigate(self, index):
        self.instant_tooltips.hide()
        self.pages.setCurrentIndex(index)
        self.nav.button(index).setChecked(True)

    def change_radio(self, key, value, custom=True):
        self.service.set_radio(key, value, custom)
        self.persistence.setText('SAVING')
        self.save_timer.start()

    def save(self):
        ok = self.service.save_cfg()
        self.persistence.setText('SAVED' if ok else 'SAVE FAILED')

    def refresh(self):
        if not hasattr(self, 'persistence') or self._closing:
            return
        s = self.service
        r = s.cfg['radio']
        self.profile.sync(r.get('profile', 'Custom'))
        self.profile_description.setText(E.PROFILE_INFO.get(r.get('profile'), 'Manual radio settings.'))
        self.volume.sync(float(r['volume']))
        self.rate_value.setText(f'{s.rate:.1f}x')
        self.slower.setEnabled(s.rate > .5)
        self.faster.setEnabled(s.rate < 2.5)
        self.master.setChecked(bool(r['enabled']))
        set_power_state(self.master, bool(r['enabled']))
        self.master.setText('RADIO / ' + ('ON' if r['enabled'] else 'BYPASS'))
        self.mute.setChecked(s.muted)
        set_power_state(self.mute, not s.muted)
        self.mute.setText('UNMUTE OUTPUT' if s.muted else 'MUTE OUTPUT')
        states = [('PTT', 'ptt'), ('EQ', 'eq'), ('SYNC', 'sync_burst_enabled'), ('SPKR', 'speaker_enabled')]
        self.stage_status.setText('  ·  '.join(name + (' ON' if r[key] else ' OFF') for name, key in states[:2]) + '\n' +
                                  '  ·  '.join(name + (' ON' if r[key] else ' OFF') for name, key in states[2:]))
        self.voice_page.sync()
        radio_view = dict(r)
        if radio_view != getattr(self, '_last_radio_view', None):
            self.radio_page.sync()
            self.dsp_page.sync()
            self._last_radio_view = radio_view
        self.log.sync(s.connected)
        self.refresh_instruments()

    def refresh_instruments(self):
        s = self.service
        self.link.sync('LIVE' if s.connected else 'NO LINK', T.GREEN if s.connected else T.RED)
        recent = time.monotonic() - self._last_receive < 2
        self.receive.sync('RECEIVING' if recent else 'STANDBY', T.CYAN if recent else T.DIM)
        synthesizing = s.synth_waiting or bool(s.prep) or bool(s.pending)
        audio = 'MUTED' if s.muted else ('SPEAKING' if s.speaking else ('SYNTHESIZING' if synthesizing else 'READY'))
        self.audio.sync(audio, T.RED if s.muted else T.GREEN if s.speaking else T.DIM)
        self.engine.sync(s.engine_mode, T.CYAN)
        # Busy state can change between synthesis and playback without a chat event.
        self.test_button.setEnabled(not s.test_busy and not s.speaking and not s.play_queue and not s.muted)
        self.test_target.setEnabled(True)

    def add_record(self, record):
        if str(record.get('msg', '')).strip():
            self._last_receive = time.monotonic()
            self.log.add(record)
            self.refresh_instruments()

    def show_fault(self, text):
        self._fault = text
        self.alert_text.setText(text)
        self.alert.show()

    def ack_fault(self):
        self._fault = ''
        self.alert.hide()

    def confirm(self, title, message, action):
        dialog = QDialog(self)
        dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dialog.setStyleSheet(T.stylesheet())
        dialog.setFont(T.display_font(13))
        dialog.setWindowTitle(title)
        dialog.setFixedSize(440, 210)
        dialog.move(self.geometry().center().x() - 220, self.geometry().center().y() - 105)
        box = QVBoxLayout(dialog)
        box.setContentsMargins(22, 20, 22, 20)
        box.addWidget(label(title, 21, T.AMBER))
        description = label(message)
        description.setWordWrap(True)
        box.addWidget(description, 1)
        row = QHBoxLayout()
        row.addStretch()
        cancel = button('CANCEL', dialog.reject)
        proceed = button(action, dialog.accept)
        proceed.setProperty('danger', True)
        proceed.setAutoDefault(False)
        cancel.setDefault(True)
        row.addWidget(proceed)
        row.addWidget(cancel)
        box.addLayout(row)
        cancel.setFocus()
        return dialog.exec() == QDialog.DialogCode.Accepted

    def reset_settings(self):
        if self.confirm('RESET ALL SETTINGS', 'Restore voices, engine, rate and every radio setting? Your current settings will be saved as a backup.', 'RESET ALL'):
            self.save_timer.stop()
            self.service._do_reset()
            self.persistence.setText('DEFAULTS SAVED')

    def restart(self):
        if self.confirm('RESTART APPLICATION', 'Restart SquelchWT? Current settings are saved and retained.', 'RESTART'):
            self.shutdown()
            arguments = [sys.executable]
            if not getattr(sys, 'frozen', False):
                arguments.append(str(E.Path(E.__file__).with_name('server.py')))
            os.execv(sys.executable, arguments)

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        self.poll_timer.stop()
        self.instrument_timer.stop()
        self.save_timer.stop()
        self.dispatcher.active = False
        QApplication.instance().removeEventFilter(self.instant_tooltips)
        self.service.stop()

    def closeEvent(self, event):
        self.shutdown()
        event.accept()
