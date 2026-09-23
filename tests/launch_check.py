"""Windows entry-point, single-instance, native identity and graceful-close smoke test."""
import ctypes
from ctypes import wintypes
import subprocess
import sys
import time
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
u = ctypes.windll.user32
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
u.GetWindowLongW.restype = wintypes.LONG
u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
u.GetWindow.restype = wintypes.HWND
u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
u.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
u.IsWindowVisible.argtypes = [wintypes.HWND]
u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

temp = tempfile.TemporaryDirectory()
pid_path = Path(temp.name) / 'pid.txt'
env = dict(os.environ, SQUELCH_QA_PID_FILE=str(pid_path))
# Windows venv redirectors can launch a second process; record the actual interpreter PID.
bootstrap = "import os,pathlib,runpy; pathlib.Path(os.environ['SQUELCH_QA_PID_FILE']).write_text(str(os.getpid())); runpy.run_path('server.py',run_name='__main__')"
child = subprocess.Popen([sys.executable, '-c', bootstrap], cwd=ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=subprocess.CREATE_NO_WINDOW)
try:
    handles = []

    @callback_type
    def collect(hwnd, _):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        actual_pid = int(pid_path.read_text()) if pid_path.exists() else child.pid
        if pid.value == actual_pid and u.IsWindowVisible(hwnd):
            length = u.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length + 1)
            u.GetWindowTextW(hwnd, title, length + 1)
            if title.value == 'SquelchWT — Communications':
                handles.append(hwnd)
        return True

    deadline = time.monotonic() + 10
    while not handles and time.monotonic() < deadline and child.poll() is None:
        u.EnumWindows(collect, 0)
        time.sleep(.05)
    assert handles, 'Application did not create a visible native window'
    hwnd = handles[0]
    style = u.GetWindowLongW(hwnd, -16)
    extended = u.GetWindowLongW(hwnd, -20)
    assert not (extended & 0x80), 'Unexpected tool window (would hide from taskbar)'
    assert not (extended & 0x8), 'Unexpected always-on-top style'
    assert not u.GetWindow(hwnd, 4), 'Unexpected owner (would alter task switching)'
    assert style & 0x20000, 'Minimize style missing'
    duplicate = subprocess.run([sys.executable, str(ROOT / 'server.py')], cwd=ROOT,
                               capture_output=True, text=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    assert duplicate.returncode != 0
    assert 'already running' in duplicate.stderr + duplicate.stdout
    u.PostMessageW(hwnd, 0x10, 0, 0)
    stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, (child.returncode, stdout, stderr)
    print('PASS: native taskbar identity, minimize style, no topmost, single-instance lock, WM_CLOSE, clean process exit')
finally:
    if child.poll() is None:
        for hwnd in handles:
            u.PostMessageW(hwnd, 0x10, 0, 0)
        time.sleep(.2)
    if child.poll() is None:
        child.terminate()
        child.communicate(timeout=5)
    temp.cleanup()
