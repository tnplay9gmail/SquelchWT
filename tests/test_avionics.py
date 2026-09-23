"""Integration checks run with an isolated configuration and real Qt widgets."""
import copy
import io
import json
import tempfile
import threading
import time
import unittest
import numpy as np
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt, QTimer, QThread, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QWidget
import engine as E
from radio_processor import RadioConfig, RadioProcessor
from ui.window import MainWindow
from ui.settings import GROUPS
from ui import theme as T

APP = QApplication.instance() or QApplication([])


class AvionicsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / 'config.json'
        self.backup = Path(self.temp.name) / 'config.backup.json'
        self.config.write_text(json.dumps(E.DEFAULT_CONFIG))
        self.paths = patch.multiple(E, CONFIG_PATH=self.config, CONFIG_BACKUP_PATH=self.backup)
        self.paths.start()
        self.window = MainWindow(start_workers=False)
        self.window.show()
        APP.processEvents()
        self.service = self.window.service

    def tearDown(self):
        self.window.close()
        APP.processEvents()
        self.paths.stop()
        self.temp.cleanup()

    def wait_for(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def test_open_navigation_never_modifies_configuration(self):
        original = copy.deepcopy(self.service.cfg)
        for page in range(4):
            self.window.nav.button(page).click()
            self.window.refresh()
            self.assertEqual(self.window.pages.currentIndex(), page)
        self.assertEqual(original, self.service.cfg)

    def test_all_dsp_controls_ranges_steps_and_tooltips(self):
        keys = [k for group in GROUPS.values() for k in group]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), {s[0] for s in E.DSP_SLIDERS})
        for key, _, lo, hi, step, fmt in E.DSP_SLIDERS:
            p = self.window.dsp_page.parameters[key]
            self.assertEqual((p.lo, p.hi, p.step, p.fmt), (lo, hi, step, fmt))
            self.assertIn(E.DSP_SLIDER_TIPS[key], p.slider.accessibleDescription())
            p.slider.setValue(0)
            p.slider.setValue(p.slider.maximum())
            self.assertAlmostEqual(self.service.cfg['radio'][key], hi)
            p.slider.setValue(1)
            self.assertAlmostEqual(self.service.cfg['radio'][key], lo + step)
        self.assertEqual(self.service.cfg['radio']['profile'], 'Custom')

    def test_every_profile_and_legacy_out_of_range_value(self):
        self.assertEqual(
            {values['channel_model'] for values in E.PROFILES.values()},
            {'digital', 'fm', 'am', 'ssb'},
        )
        for name, values in E.PROFILES.items():
            self.service.select_profile(name)
            expected = {**E.DSP_DEFAULTS, **values}
            for key, value in expected.items():
                self.assertEqual(self.service.cfg['radio'][key], value)
            for _ in range(3):
                self.window.refresh()
            self.assertEqual(self.service.cfg['radio']['profile'], name)
        self.service.select_profile('AM Radio Night')
        self.assertEqual(self.service.cfg['radio']['lp_cut'], 5000)
        self.assertIn('5000', self.window.dsp_page.parameters['lp_cut'].value_label.text())
        self.window.navigate(3)
        self.assertEqual(self.service.cfg['radio']['lp_cut'], 5000)

    def test_modulation_models_are_stable_and_legacy_is_exact_bypass(self):
        fs = 8000
        t = np.arange(fs // 3) / fs
        speech = 0.35 * np.sin(2 * np.pi * 180 * t) + 0.12 * np.sin(2 * np.pi * 1700 * t)
        legacy = RadioProcessor(RadioConfig(channel_model='legacy', seed=4))
        np.testing.assert_array_equal(legacy._channel_roundtrip(speech, fs, np.random.default_rng(4)), speech)
        outputs = {}
        for model in ('fm', 'am', 'ssb', 'digital'):
            cfg = RadioConfig(channel_model=model, channel_snr_db=20.0,
                              ssb_tuning_offset_hz=18.0, seed=4)
            y = RadioProcessor(cfg)._channel_roundtrip(speech, fs, np.random.default_rng(4))
            self.assertEqual(y.shape, speech.shape)
            self.assertTrue(np.isfinite(y).all())
            outputs[model] = y
        np.testing.assert_array_equal(outputs['digital'], speech)
        for model in ('fm', 'am', 'ssb'):
            self.assertGreater(float(np.mean(np.abs(outputs[model] - speech))), 1e-4)

    def test_pre_modulation_config_migrates_to_legacy_channel(self):
        old = copy.deepcopy(E.DEFAULT_CONFIG)
        old['radio'].pop('channel_model', None)
        self.config.write_text(json.dumps(old))
        self.assertEqual(E.load_config()['radio']['channel_model'], 'legacy')

    def test_primary_controls_and_persistence(self):
        self.window.volume.slider.setValue(118)
        self.window.radio_page.noise.slider.setValue(54)
        self.window.faster.click()
        self.window.save()
        stored = json.loads(self.config.read_text())
        self.assertEqual(stored['radio']['volume'], 1.18)
        self.assertEqual(stored['radio']['noise'], .54)
        self.assertEqual(stored['rate'], 1.1)
        self.assertEqual(stored['radio']['profile'], E.DEFAULT_PROFILE)
        self.assertFalse(self.config.with_suffix('.json.tmp').exists())
        self.window.mute.click()
        self.assertTrue(self.service.muted)
        self.assertEqual(self.window.audio.readout.text(), 'MUTED')
        self.assertEqual(self.window.link.readout.text(), 'NO LINK')
        self.service.set_status(True)
        self.assertEqual(self.window.link.readout.text(), 'LIVE')

    def test_all_radio_switches_codec_and_voices(self):
        for key, (control, _) in self.window.radio_page.toggles.items():
            before = self.service.cfg['radio'][key]
            control.click()
            self.assertEqual(self.service.cfg['radio'][key], not before)
            self.assertEqual(control.property('powerState'), 'off' if before else 'on')
        self.window.radio_page.codec.setCurrentText('none')
        self.window.radio_page.codec.activated.emit(1)
        self.assertEqual(self.service.cfg['radio']['codec'], 'none')
        for which in ('ally', 'enemy'):
            self.window.voice_page.selectors[which].activated.emit(2)
            self.assertEqual(getattr(self.service, 'neural_' + which), E.NEURAL_VOICES[2][1])
        with patch.object(E, 'load_piper', return_value=None):
            self.window.voice_page.piper_selectors['ally'].activated.emit(3)
            self.window.voice_page.piper_selectors['enemy'].activated.emit(4)
            self.window.voice_page.local.click()
        self.assertEqual(self.service.piper_ally, E.PIPER_PRESETS[3][1])
        self.assertEqual(self.service.piper_enemy, E.PIPER_PRESETS[4][1])
        self.assertEqual(self.service.engine_mode, 'LOCAL')

    def test_master_and_mute_use_operational_on_off_states(self):
        self.assertEqual(self.window.master.property('powerState'), 'on')
        self.assertEqual(self.window.mute.property('powerState'), 'on')
        self.window.master.click()
        self.assertEqual(self.window.master.property('powerState'), 'off')
        self.assertIn('BYPASS', self.window.master.text())
        self.window.mute.click()
        self.assertEqual(self.window.mute.property('powerState'), 'off')
        self.assertIn('UNMUTE', self.window.mute.text())

    def test_window_controls_are_aligned_and_compact(self):
        bar = self.window.titlebar
        self.assertEqual(bar.height(), 30)
        self.assertEqual((bar.minimize.width(), bar.minimize.height()), (28, 24))
        self.assertEqual((bar.close.width(), bar.close.height()), (28, 24))
        self.assertEqual(bar.minimize.y(), bar.close.y())
        self.assertEqual(bar.minimize.y(), 3)

    def test_sidebar_voice_test_and_comms_alert(self):
        self.assertEqual(self.window.pages.count(), 4)
        self.assertEqual(self.window.log.empty_state.text(), 'NO LINK')
        self.assertEqual(self.window.log.empty_detail.text(), 'GAME CHAT UNAVAILABLE')
        self.assertTrue(self.window.log.blink_timer.isActive())
        with patch.object(self.service, '_test_voice') as audition:
            self.window.test_target.setCurrentText('ENEMY')
            self.window.test_button.click()
            audition.assert_called_once_with('NEURAL', voice=self.service.neural_enemy, enemy=True)
            self.service.engine_mode = 'LOCAL'
            self.window.refresh()
            self.window.test_button.click()
            self.assertEqual(audition.call_args.kwargs['voice'], self.service.piper_enemy)
        self.service.set_status(True)
        self.assertFalse(self.window.log.blink_timer.isActive())
        self.assertEqual(self.window.log.empty_state.text(), 'STANDBY')

    def test_tooltip_is_requested_on_enter_without_delay(self):
        QApplication.sendEvent(self.window.mute, QEvent(QEvent.Type.Enter))
        self.assertEqual(self.window.mute.toolTip(), '')
        self.assertEqual(self.window.instant_tooltips.popup.text(), self.window.mute.property('helpText'))
        self.assertTrue(self.window.instant_tooltips.popup.isVisible())
        self.assertIn(f'border: 1px solid {T.GREEN}', self.window.instant_tooltips.popup.styleSheet())
        self.assertTrue(all(not widget.toolTip() for widget in self.window.findChildren(QWidget)))
        self.window.instant_tooltips.hide()

    def test_tooltip_does_not_open_from_focus_alone(self):
        self.window.instant_tooltips.hide()
        QApplication.sendEvent(self.window.mute, QEvent(QEvent.Type.FocusIn))
        self.assertFalse(self.window.instant_tooltips.popup.isVisible())

    def test_voice_test_menu_has_individual_allegiance_colors(self):
        self.assertEqual(self.window.test_target.itemData(0, Qt.ItemDataRole.ForegroundRole).name(), T.ALLY)
        self.assertEqual(self.window.test_target.itemData(1, Qt.ItemDataRole.ForegroundRole).name(), T.ENEMY)

    def test_status_updates_skip_unchanged_radio_controls(self):
        with patch.object(self.window.dsp_page, 'sync') as sync:
            self.window.refresh()
            self.service.set_status(False)
            self.window.refresh()
            sync.assert_not_called()
            self.service.set_radio('noise', .37)
            sync.assert_called_once()

    def test_offline_models_migrate_and_route_by_allegiance(self):
        self.config.write_text(json.dumps({'piper_voice': 'en_US-ryan-low'}))
        migrated = E.load_config()
        self.assertEqual(migrated['piper_ally'], 'en_US-ryan-low')
        self.assertEqual(migrated['piper_enemy'], E.PIPER_ENEMY_VOICE)
        self.service.engine_mode = 'LOCAL'
        with patch.object(E, 'tts_local', return_value=b'audio') as synth:
            self.service.synthesize({'msg': 'friendly', 'enemy': False})
            self.assertEqual(synth.call_args.kwargs['model'], self.service.piper_ally)
            self.service.synthesize({'msg': 'hostile', 'enemy': True})
            self.assertEqual(synth.call_args.kwargs['model'], self.service.piper_enemy)

    def test_model_download_reports_percentage_and_installs_both_files(self):
        class Response(io.BytesIO):
            def __init__(self, data):
                super().__init__(data)
                self.headers = {'Content-Length': str(len(data))}

        model = 'en_US-ryan-low'
        target = Path(self.temp.name) / 'voices'
        config = json.dumps({'audio': {'sample_rate': 16000}}).encode()
        percentages = []
        with patch.object(E, 'VOICES_DIR', target), patch.object(E, 'urlopen', side_effect=[Response(b'model'), Response(config)]):
            E.download_piper_model(model, percentages.append)
            self.assertTrue(E.piper_installed(model))
            self.assertEqual(sorted(percentages), percentages)
            self.assertEqual(percentages[-1], 100)
            self.assertFalse(list(target.glob('*.part')))
            (target / f'{model}.onnx.json').write_text('invalid', encoding='utf-8')
            self.assertFalse(E.piper_installed(model))

    def test_incomplete_model_download_does_not_look_installed(self):
        class ShortResponse(io.BytesIO):
            headers = {'Content-Length': '100'}

        model = 'en_US-joe-medium'
        target = Path(self.temp.name) / 'voices'
        with patch.object(E, 'VOICES_DIR', target), patch.object(E, 'urlopen', return_value=ShortResponse(b'short')):
            with self.assertRaises(OSError):
                E.download_piper_model(model)
            self.assertFalse(E.piper_installed(model))
            self.assertFalse(list(target.glob('*.part')))

    def test_log_is_plain_text_bounded_and_clearable(self):
        for i in range(83):
            self.window.add_record({'sender': f'PILOT {i}', 'msg': '<b>Need cover</b>', 'mode': 'Team', 'enemy': i % 2})
        self.assertEqual(len(self.window.log.rows), 80)
        labels = self.window.log.rows[-1].findChildren(QLabel)
        self.assertIn('<b>Need cover</b>', [w.text() for w in labels])
        self.assertTrue(all(w.textFormat() == Qt.TextFormat.PlainText for w in labels))
        self.assertIn('ALLY/TEAM', [w.text() for w in labels])
        self.assertIn('border-left: 3px solid', self.window.log.rows[-1].styleSheet())
        self.assertEqual(self.window.log.stack.indexOf(self.window.log.rows[-1]), 79)
        self.window.log.clear_button.click()
        self.assertEqual(self.window.log.rows, [])
        self.assertTrue(self.window.log.empty.isVisible())

    def test_poll_history_then_new_traffic_and_disconnect(self):
        first = [{'id': i, 'sender': 'P', 'msg': f'{i}', 'mode': 'Team'} for i in range(1, 16)]
        with patch.object(E, 'fetch_chat', return_value=first):
            self.service.poll_once()
        APP.processEvents()
        self.assertEqual(len(self.window.log.rows), 12)
        self.assertEqual(len(self.service.pending), 0)
        with patch.object(E, 'fetch_chat', return_value=[{'id': 16, 'msg': 'new'}]):
            self.service.poll_once()
        APP.processEvents()
        self.assertEqual(len(self.service.pending), 1)
        self.assertTrue(self.service.connected)
        with patch.object(E, 'fetch_chat', side_effect=OSError('offline')):
            self.service.poll_once()
        APP.processEvents()
        self.assertFalse(self.service.connected)
        self.assertEqual(self.service.last_id, 0)
        self.assertFalse(self.service.initialized)
        pending_before = len(self.service.pending)
        with patch.object(E, 'fetch_chat', return_value=[{'id': 1, 'msg': 'old session'}]):
            self.service.poll_once()
        self.assertEqual(len(self.service.pending), pending_before)

    def test_default_radio_message_filter_exact_prefix_and_toggle(self):
        self.assertTrue(E.is_default_radio_message('Awesome!'))
        self.assertTrue(E.is_default_radio_message('  THANK YOU VERY MUCH.  '))
        self.assertTrue(E.is_default_radio_message('Someone, attack the B point!'))
        self.assertFalse(E.is_default_radio_message('Awesome landing, pilot'))
        self.assertFalse(E.is_default_radio_message('Attack them now'))
        self.service.initialized = True
        filtered = [
            {'id': 20, 'sender': 'P', 'msg': 'Gramercy!'},
            {'id': 21, 'sender': 'P', 'msg': 'Someone, attack the marked target'},
            {'id': 22, 'sender': 'P', 'msg': 'Enemy aircraft north'},
        ]
        with patch.object(E, 'fetch_chat', return_value=filtered):
            self.service.poll_once()
        APP.processEvents()
        self.assertEqual(len(self.window.log.rows), 3)
        self.assertEqual([r['msg'] for r in self.service.pending], ['Enemy aircraft north'])
        self.service.pending.clear()
        self.service.set_radio('mute_default_messages', False, custom=False)
        with patch.object(E, 'fetch_chat', return_value=[{'id': 23, 'msg': 'Awesome!'}]):
            self.service.poll_once()
        self.assertEqual([r['msg'] for r in self.service.pending], ['Awesome!'])

    def test_worker_callbacks_are_queued_on_gui_thread(self):
        delivered = []
        thread = threading.Thread(target=lambda: self.service._post(lambda: delivered.append(QThread.currentThread())))
        thread.start()
        thread.join()
        self.assertEqual(delivered, [])
        APP.processEvents()
        self.assertEqual(delivered, [APP.thread()])

    def test_test_guard_covers_synthesis_queue_and_playback(self):
        release = threading.Event()
        entered = threading.Event()

        def playback(*args):
            entered.set()
            release.wait(2)

        with patch.object(self.service, 'synthesize', return_value=b'wav'), patch.object(E.winsound, 'PlaySound', side_effect=playback):
            self.service._test_voice('LOCAL')
            self.service._test_voice('NEURAL')
            self.assertEqual(len(self.service.pending), 1)
            self.service.process_burst()
            self.assertTrue(self.service.test_busy)
            self.service._test_voice('LOCAL')
            self.assertEqual(len(self.service.pending), 0)
            player = threading.Thread(target=self.service.player_worker)
            player.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.service.test_busy)
            release.set()
            self.wait_for(lambda: not self.service.test_busy)
            self.service.running = False
            self.service.play_event.set()
            player.join(1)
            self.assertFalse(player.is_alive())

    def test_synthesis_fault_unblocks_test_and_annunciates(self):
        with patch.object(self.service, 'synthesize', side_effect=RuntimeError('test failure')):
            self.service._test_voice('LOCAL')
            self.service.process_burst()
        APP.processEvents()
        self.assertFalse(self.service.test_busy)
        self.assertTrue(self.window.alert.isVisible())
        self.assertIn('VOICE FAILED', self.window.alert_text.text())

    def test_reset_backup_and_restart_dispatch(self):
        self.service.set_radio('volume', 1.21, False)
        self.window.save()
        old = json.loads(self.config.read_text())
        with patch.object(self.window, 'confirm', return_value=False):
            self.window.reset_settings()
        self.assertEqual(old, self.service.cfg)
        with patch.object(self.window, 'confirm', return_value=True):
            self.window.reset_settings()
        self.assertEqual(json.loads(self.backup.read_text()), old)
        self.assertEqual(self.service.cfg, E.DEFAULT_CONFIG)
        with patch.object(self.window, 'confirm', return_value=True), patch('ui.window.os.execv') as execv:
            self.window.restart()
        self.assertTrue(execv.called)
        self.assertFalse(self.service.running)

    def test_confirmation_defaults_to_cancel(self):
        def press_return():
            dialog = self.window.findChild(QDialog)
            self.assertIsNotNone(dialog)
            self.assertTrue(bool(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint))
            QTest.keyClick(dialog, Qt.Key.Key_Return)
        QTimer.singleShot(30, press_return)
        self.assertFalse(self.window.confirm('RESET', 'Test confirmation', 'RESET'))

    def test_fixed_size_minimize_restore_and_native_identity(self):
        self.window.resize(1200, 800)
        self.assertEqual((self.window.width(), self.window.height()), (700, 700))
        self.window.titlebar.minimize.click()
        QTest.qWait(50)
        self.assertTrue(self.window.isMinimized())
        self.window.showNormal()
        QTest.qWait(50)
        self.assertFalse(self.window.isMinimized())
        self.assertFalse(bool(self.window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
        self.assertEqual(self.window.windowType(), Qt.WindowType.Window)

    def test_every_widget_uses_viper_display_font(self):
        for widget in [self.window, *self.window.findChildren(QWidget)]:
            self.assertEqual(widget.font().family(), 'Hornet Display',
                             f'{type(widget).__name__} uses {widget.font().family()}')
            self.assertTrue(widget.font().bold(), f'{type(widget).__name__} is not bold')

    def test_save_failure_is_actionable(self):
        with patch.object(E.os, 'replace', side_effect=PermissionError('blocked')):
            self.window.save()
        APP.processEvents()
        self.assertEqual(self.window.persistence.text(), 'SAVE FAILED')
        self.assertIn('CONFIG NOT SAVED', self.window.alert_text.text())

    def test_rate_bounds_and_download_error_recovery(self):
        self.service.change_rate(99)
        self.assertEqual(self.service.rate, 2.5)
        self.assertFalse(self.window.faster.isEnabled())
        self.service.change_rate(-99)
        self.assertEqual(self.service.rate, .5)
        self.assertFalse(self.window.slower.isEnabled())
        self.service.downloading = True
        self.service.download_target = self.service.piper_enemy
        self.service.download_percent = 42
        self.window.refresh()
        self.assertFalse(self.window.voice_page.piper_selectors['ally'].isEnabled())
        self.assertEqual(self.window.voice_page.progress_bars['enemy'].value(), 42)
        with patch.object(E, 'download_piper_model', side_effect=OSError('network')):
            self.service._download(self.service.piper_enemy)
        APP.processEvents()
        self.assertFalse(self.service.downloading)
        self.assertTrue(self.window.voice_page.piper_selectors['ally'].isEnabled())
        self.assertIn('VOICE DOWNLOAD FAILED', self.window.alert_text.text())


if __name__ == '__main__':
    unittest.main()
