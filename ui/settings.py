"""Voice, radio and grouped DSP pages."""
from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QButtonGroup, QProgressBar
import engine as E
from . import theme as T
from .help_text import TIPS
from .widgets import label, button, panel, Selector, Parameter, help_tip, set_power_state

GROUPS = {
    'SIGNAL': ('hp_cut', 'lp_cut', 'nb_rate', 'presence_db'),
    'CODEC': ('opus_bitrate', 'lpc_order', 'lpc_frame_ms', 'cvsd_oversample', 'vocoder_mix'),
    'DYNAMICS': ('comp_ratio', 'comp_thresh_db', 'comp_release_ms', 'sat_drive', 'tube_drive', 'tube_sag', 'agc_amount'),
    'RF': ('fade_depth_db', 'fade_rate_hz', 'het_freq_hz', 'het_level', 'mpath_delay_ms', 'mpath_doppler_hz', 'mpath_ratio'),
    'TX': ('noise_level', 'dropout_prob', 'ptt_gain', 'ptt_freq', 'ptt_dur_ms', 'squelch_gain', 'squelch_dur_ms', 'squelch_burst_gain'),
    'OUTPUT': ('speaker_res_db', 'vox_clip_ms', 'output_gain'),
}


def page(title, subtitle=''):
    widget = QWidget()
    box = QVBoxLayout(widget)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(5)
    box.addWidget(label(title, 15))
    if subtitle:
        description = label(subtitle, muted=True)
        description.setWordWrap(True)
        box.addWidget(description)
    return widget, box


class VoicePage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        s = window.service
        content, box = page('VOICE CONFIGURATION')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(content)
        modes = QHBoxLayout()
        self.cloud = button('NEURAL / CLOUD', lambda: s.set_engine('NEURAL'), TIPS['btn_cloud'], True)
        self.local = button('LOCAL / OFFLINE', lambda: s.set_engine('LOCAL'), TIPS['btn_offline'], True)
        modes.addWidget(self.cloud)
        modes.addWidget(self.local)
        box.addLayout(modes)
        self.voices = QStackedWidget()
        cloud = panel()
        cloud_box = QVBoxLayout(cloud)
        cloud_box.setSpacing(6)
        self.selectors = {}
        for which in ('ally', 'enemy'):
            cloud_box.addWidget(label('ALLIED VOICE' if which == 'ally' else 'ENEMY VOICE', 11,
                                      T.ALLY if which == 'ally' else T.ENEMY))
            selector = Selector([n for n, _ in E.NEURAL_VOICES])
            selector.activated.connect(lambda idx, w=which: s.set_voice(w, E.NEURAL_VOICES[idx][1]))
            help_tip(selector, TIPS['cb_' + which])
            self.selectors[which] = selector
            cloud_box.addWidget(selector)
        cloud_box.addStretch()
        self.voices.addWidget(cloud)
        local = panel()
        local_box = QVBoxLayout(local)
        local_box.setSpacing(7)
        self.piper_selectors = {}
        self.model_status = {}
        self.download_buttons = {}
        self.progress_bars = {}
        for which in ('ally', 'enemy'):
            color = T.ALLY if which == 'ally' else T.ENEMY
            local_box.addWidget(label(('ALLIED' if which == 'ally' else 'ENEMY') + ' OFFLINE VOICE', 11, color))
            selector = Selector([p[0] for p in E.PIPER_PRESETS])
            selector.activated.connect(lambda idx, w=which: s.set_voice('piper_' + w, E.PIPER_PRESETS[idx][1]))
            help_tip(selector, TIPS['cb_piper'])
            self.piper_selectors[which] = selector
            local_box.addWidget(selector)
            row = QHBoxLayout()
            status = label('', 11, color)
            self.model_status[which] = status
            row.addWidget(status, 1)
            download = button('DOWNLOAD', lambda checked=False, w=which: s.download_voice(w), TIPS['local_dl_btn'])
            self.download_buttons[which] = download
            row.addWidget(download)
            local_box.addLayout(row)
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(0)
            progress.setAccessibleName(which.upper() + ' offline voice download progress')
            self.progress_bars[which] = progress
            local_box.addWidget(progress)
            progress.hide()
        self.piper = self.piper_selectors['ally']
        self.download = self.download_buttons['ally']
        local_box.addStretch()
        self.voices.addWidget(local)
        box.addWidget(self.voices, 1)

    def sync(self):
        s = self.window.service
        self.cloud.setChecked(s.engine_mode == 'NEURAL')
        self.local.setChecked(s.engine_mode == 'LOCAL')
        set_power_state(self.cloud, s.engine_mode == 'NEURAL')
        set_power_state(self.local, s.engine_mode == 'LOCAL')
        self.voices.setCurrentIndex(0 if s.engine_mode == 'NEURAL' else 1)
        for which, selector in self.selectors.items():
            voice = getattr(s, 'neural_' + which)
            selector.sync(next((n for n, v in E.NEURAL_VOICES if v == voice), voice))
        for which in ('ally', 'enemy'):
            model = getattr(s, 'piper_' + which)
            selector = self.piper_selectors[which]
            selector.sync(next((p[0] for p in E.PIPER_PRESETS if p[1] == model), model))
            selector.setEnabled(not s.downloading)
            installed = E.piper_installed(model)
            active = s.downloading and s.download_target == model
            failed = E.piper_failed(model)
            status = ('LOADING VOICE' if s.download_percent == 100 else 'DOWNLOADING') if active else ('VOICE UNAVAILABLE' if failed else ('INSTALLED' if installed else 'NOT INSTALLED'))
            self.model_status[which].setText(status)
            self.download_buttons[which].setEnabled((not installed or failed) and not s.downloading)
            size = E.PIPER_PRESET_BY_MODEL.get(model, ('', '?'))[1]
            self.download_buttons[which].setText(f'{s.download_percent}%' if active else ('REINSTALL' if failed else ('INSTALLED' if installed else f'DOWNLOAD {size} MB')))
            progress = self.progress_bars[which]
            progress.setVisible(active)
            if active:
                progress.setValue(s.download_percent)


class RadioPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        content, box = page('RADIO PROCESSOR')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(content)
        self.toggles = {}
        grid = QGridLayout()
        specs = [('enabled', 'RADIO PROCESSING', 'btn_fx_master'), ('ptt', 'PTT EFFECTS', 'btn_fx_ptt'),
                 ('eq', 'BANDPASS EQ', 'btn_fx_eq'), ('sync_burst_enabled', 'SYNC BURST', 'btn_fx_sync'),
                 ('speaker_enabled', 'SPEAKER', 'btn_adv_speaker'),
                 ('mute_default_messages', 'MUTE DEFAULT MESSAGES', 'mute_default_messages')]
        for i, (key, name, tip) in enumerate(specs):
            w = button(name, lambda checked, k=key: window.change_radio(k, checked, custom=k != 'enabled'), TIPS[tip], True)
            grid.addWidget(w, i // 2, i % 2)
            self.toggles[key] = (w, name)
        box.addLayout(grid)
        row = QHBoxLayout()
        row.addWidget(label('VOICE CODEC', 11))
        self.codec = Selector(E.CODEC_UI_CHOICES)
        self.codec.activated.connect(lambda: window.change_radio('codec', self.codec.currentText()))
        help_tip(self.codec, TIPS['cb_codec'])
        row.addWidget(self.codec, 1)
        box.addLayout(row)
        self.noise = Parameter('NOISE', 0, 1, .01, '{:.2f}', TIPS['noise_scale'])
        self.noise.changed.connect(lambda value: window.change_radio('noise', value, custom=False))
        box.addWidget(self.noise)
        box.addStretch()
        box.addWidget(button('OPEN ADVANCED DSP', lambda: window.navigate(3), TIPS['btn_adv']))

    def sync(self):
        radio = self.window.service.cfg['radio']
        for key, (w, name) in self.toggles.items():
            on = bool(radio.get(key, E.DSP_DEFAULTS.get(key, True)))
            w.setChecked(on)
            set_power_state(w, on)
            w.setText(name + ('  /  ON' if on else '  /  OFF'))
        # Preserve legacy codec selections without making opening this page change them.
        codec = radio['codec']
        if self.codec.findText(codec) < 0:
            self.codec.addItem(codec)
        self.codec.sync(codec)
        self.noise.sync(float(radio['noise']))


class DspPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        box.addWidget(label('ADVANCED DSP', 15))
        tabs = QHBoxLayout()
        tabs.setSpacing(4)
        self.group = QButtonGroup(self)
        self.stack = QStackedWidget()
        self.parameters = {}
        for index, (group, keys) in enumerate(GROUPS.items()):
            tab = button(group, lambda checked, i=index: self.stack.setCurrentIndex(i), checkable=True)
            tab.setStyleSheet('padding: 4px 3px; font-size: 11px;')
            self.group.addButton(tab, index)
            tabs.addWidget(tab)
            if index == 0:
                tab.setChecked(True)
            sheet = QWidget()
            grid = QGridLayout(sheet)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(4)
            grid.setAlignment(Qt.AlignmentFlag.AlignTop)
            for i, key in enumerate(keys):
                spec = next(row for row in E.DSP_SLIDERS if row[0] == key)
                _, name, lo, hi, step, fmt = spec
                control = Parameter(name, lo, hi, step, fmt, E.DSP_SLIDER_TIPS[key], compact=True)
                control.changed.connect(lambda value, k=key: window.change_radio(k, value))
                control.contextual.connect(self.show_help)
                grid.addWidget(control, i // 2, i % 2)
                self.parameters[key] = control
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            self.stack.addWidget(sheet)
        box.addLayout(tabs)
        box.addWidget(self.stack, 1)
        self.context = label('PARAMETER HELP', muted=True)
        self.context.setWordWrap(True)
        self.context.setMinimumHeight(38)
        box.addWidget(self.context)

    def show_help(self, text):
        self.context.setText(text)

    def sync(self):
        s = self.window.service
        for key, control in self.parameters.items():
            control.sync(float(s.cfg['radio'][key]))
