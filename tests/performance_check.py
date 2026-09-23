"""Opt-in Windows performance check with isolated settings and synthetic chat."""
import ctypes
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import engine as E
from ui.window import MainWindow


class ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]


def memory_mb():
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    current_process = ctypes.windll.kernel32.GetCurrentProcess
    current_process.restype = ctypes.c_void_p
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    get_memory.restype = ctypes.c_int
    if not get_memory(current_process(), ctypes.byref(counters), counters.cb):
        raise OSError('GetProcessMemoryInfo failed')
    return round(counters.WorkingSetSize / (1024 * 1024), 1)


def main():
    app = QApplication([])
    with tempfile.TemporaryDirectory() as directory, patch.object(E, 'CONFIG_PATH', Path(directory) / 'config.json'):
        E.CONFIG_PATH.write_text(json.dumps(E.DEFAULT_CONFIG), encoding='utf-8')
        polls = []
        ticks = []
        poll_durations = []
        with patch.object(E, 'fetch_chat', side_effect=lambda last_id: polls.append(time.monotonic()) or []):
            window = MainWindow(False)
            window.show()
            window.service.start()
            original_poll_once = window.service.poll_once
            def timed_poll_once():
                started = time.perf_counter()
                try:
                    return original_poll_once()
                finally:
                    poll_durations.append((time.perf_counter() - started) * 1000)
            window.service.poll_once = timed_poll_once
            window.poll_timer.timeout.connect(lambda: ticks.append(time.monotonic()))
            window.poll_timer.start()
            app.processEvents()
            initial_cpu = time.process_time()
            initial_wall = time.monotonic()
            initial_memory = memory_mb()
            results = {}
            def measure():
                elapsed = time.monotonic() - initial_wall
                cpu = time.process_time() - initial_cpu
                results['idle'] = {'seconds': round(elapsed, 2), 'cpu_ms': round(cpu * 1000, 1),
                                   'cpu_percent_of_one_core': round(100 * cpu / elapsed, 1),
                                   'memory_start_mb': initial_memory, 'memory_end_mb': memory_mb(),
                                   'polls': len(polls), 'timer_ticks': len(ticks),
                                   'poll_duration_max_ms': round(max(poll_durations, default=0), 2)}
                window.service.downloading = True
                window.service.download_target = window.service.piper_ally
                started = time.perf_counter()
                for percent in range(101):
                    window.service.download_percent = percent
                    window.refresh()
                    app.processEvents()
                results['progress_ms'] = round((time.perf_counter() - started) * 1000)
                results['progress_value'] = window.voice_page.progress_bars['ally'].value()
                window.service.downloading = False
                app.quit()
            QTimer.singleShot(5000, measure)
            app.exec()
            idle = results['idle']
            progress_ms = results['progress_ms']
            progress_value = results['progress_value']
            assert progress_value == 100, progress_value
            window.close()
            app.processEvents()

    local = None
    model = 'en_US-ryan-low'
    if E.piper_installed(model):
        started = time.perf_counter()
        loaded = E.load_piper(model)
        load_ms = round((time.perf_counter() - started) * 1000)
        if loaded is not None:
            samples = []
            for _ in range(3):
                started = time.perf_counter()
                audio = E.tts_local('Radio check, hold position and report contact.', 1.0,
                                    radio=dict(E.DEFAULT_CONFIG['radio']), model=model)
                samples.append({'ms': round((time.perf_counter() - started) * 1000), 'wav_bytes': len(audio)})
            local = {'model': model, 'load_ms': load_ms, 'syntheses': samples, 'memory_after_mb': memory_mb()}
    print(json.dumps({'idle': idle, 'progress_0_to_100_ms': progress_ms,
                      'progress_displayed': progress_value, 'local_voice': local}, indent=2))


if __name__ == '__main__':
    main()
