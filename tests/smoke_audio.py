"""Opt-in real synthesis/playback smoke test. Does not save configuration."""
import io
import sys
import time
import wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as E

cfg = E.load_config()
E.set_piper_voice(cfg['piper_voice'])
for name, synth in [
    ('LOCAL', lambda: E.tts_local('Squelch radio check. Local voice ready.', 1.0, radio=dict(cfg['radio']))),
    ('NEURAL', lambda: E.tts_neural('Squelch radio check. Neural voice ready.', 1.0, cfg['neural_ally'], radio=dict(cfg['radio']))),
]:
    started = time.monotonic()
    try:
        data = synth()
        with wave.open(io.BytesIO(data)) as wav:
            print(name, 'WAV', wav.getnframes(), 'frames', wav.getframerate(), 'Hz', flush=True)
        E.winsound.PlaySound(data, E.winsound.SND_MEMORY | E.winsound.SND_NODEFAULT)
        print(name, 'PLAYBACK COMPLETE', round(time.monotonic() - started, 2), 's', flush=True)
    except Exception as exc:
        print(name, 'FAILED', type(exc).__name__, str(exc), flush=True)
try:
    records = E.fetch_chat(0)
    print('GAME CHAT LIVE', len(records), 'records', flush=True)
except Exception as exc:
    print('GAME CHAT UNAVAILABLE', type(exc).__name__, str(exc), flush=True)
