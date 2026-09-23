import asyncio
import http.client
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
import winsound
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import edge_tts
import miniaudio
import numpy as np

from radio_processor import RadioConfig, RadioProcessor

API_ROOT = os.environ.get('SQUELCHWT_API_ROOT', 'http://127.0.0.1:8111').rstrip('/')
POLL_MS = 100
SYNTH_WORKERS = 3
VOICE = 'en-US-GuyNeural'
ALLY_VOICE = 'en-US-AndrewNeural'
ENEMY_VOICE = 'en-US-BrianNeural'
TEST_PHRASE = 'The quick brown fox jumps over the lazy dog.'
ASSET_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
DATA_DIR = (Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'SquelchWT'
            if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent)
DATA_DIR.mkdir(parents=True, exist_ok=True)

VOICES_DIR = DATA_DIR / 'voices'
PIPER_VOICE = 'en_US-ryan-medium'
PIPER_ENEMY_VOICE = 'en_US-joe-medium'

NEURAL_VOICES = [
    ('Andrew (US male)', 'en-US-AndrewNeural'),
    ('Brian (US male)', 'en-US-BrianNeural'),
    ('Christopher (US male)', 'en-US-ChristopherNeural'),
    ('Eric (US male)', 'en-US-EricNeural'),
    ('Guy (US male)', 'en-US-GuyNeural'),
    ('Roger (US male)', 'en-US-RogerNeural'),
    ('Steffan (UK male)', 'en-GB-SteffanNeural'),
    ('Thomas (UK male)', 'en-GB-ThomasNeural'),
    ('Aria (US female)', 'en-US-AriaNeural'),
    ('Jenny (US female)', 'en-US-JennyNeural'),
    ('Michelle (US female)', 'en-US-MichelleNeural'),
    ('Sonia (UK female)', 'en-GB-SoniaNeural'),
]

PIPER_PRESETS = [
    ('ryan-medium (US male)', 'en_US-ryan-medium', 63),
    ('ryan-high (US male, HD)', 'en_US-ryan-high', 101),
    ('ryan-low (US male)', 'en_US-ryan-low', 20),
    ('joe-medium (US male)', 'en_US-joe-medium', 63),
    ('mike-medium (US male)', 'en_US-mike-medium', 63),
    ('sam-medium (US male)', 'en_US-sam-medium', 63),
    ('norman-medium (US male)', 'en_US-norman-medium', 63),
    ('bryce-medium (US male)', 'en_US-bryce-medium', 63),
    ('alan-medium (UK male)', 'en_GB-alan-medium', 63),
    ('northern-english-male (UK male)', 'en_GB-northern_english_male-medium', 63),
    ('lessac-medium (US female)', 'en_US-lessac-medium', 63),
    ('lessac-high (US female)', 'en_US-lessac-high', 101),
    ('amy-medium (US female)', 'en_US-amy-medium', 63),
    ('kristin-medium (US female)', 'en_US-kristin-medium', 63),
    ('jenny-dioco (UK female)', 'en_GB-jenny_dioco-medium', 63),
]

PIPER_PRESET_BY_MODEL = {p[1]: (p[0], p[2]) for p in PIPER_PRESETS}

CONFIG_PATH = DATA_DIR / 'config.json'
CONFIG_BACKUP_PATH = DATA_DIR / 'config.backup.json'

DSP_DEFAULTS = {
    # Tactical narrowband chain tunables (see radio_processor.RadioConfig).
    # 'codec' UI offers 'cvsd' (secure voice) and 'none' (bypass); the
    # backend still accepts 'lpc', 'melp' (alias -> LPC fallback) and
    # 'opus' via config.json for A/B testing.
    'hp_cut': 300.0,
    'lp_cut': 3400.0,
    'nb_rate': 8000,
    'codec': 'cvsd',
    # Existing and custom configurations keep the established DSP path.
    # Named profiles explicitly opt into a modulation-aware channel model.
    'channel_model': 'legacy',
    'channel_snr_db': 36.0,
    'fm_preemphasis_us': 750.0,
    'am_modulation_index': 0.65,
    'ssb_tuning_offset_hz': 0.0,
    'opus_bitrate': 8000,
    'lpc_order': 12,
    'lpc_frame_ms': 20.0,
    'cvsd_oversample': 4,
    'fade_depth_db': 0.0,
    'fade_rate_hz': 0.5,
    'het_freq_hz': 0.0,
    'het_level': 0.0,
    'tube_drive': 0.0,
    'tube_sag': 0.0,
    'agc_amount': 0.0,
    'mpath_delay_ms': 0.0,
    'mpath_doppler_hz': 0.5,
    'mpath_ratio': 0.7,
    'vocoder_mix': 0.5,
    'presence_db': 3.0,
    'comp_ratio': 8.0,
    'comp_thresh_db': -18.0,
    'comp_release_ms': 100.0,
    'sat_drive': 1.5,
    'noise_level': 0.015,
    'dropout_prob': 0.03,
    'ptt_gain': 0.55,
    'ptt_freq': 240.0,
    'ptt_dur_ms': 7.0,
    'squelch_gain': 0.5,
    'squelch_dur_ms': 16.0,
    'output_gain': 0.82,
    'sync_burst_enabled': False,
    'speaker_enabled': True,
    'speaker_res_db': 3.5,
    'squelch_burst_gain': 1.6,
    'vox_clip_ms': 8.0,
}

DSP_SLIDERS = [
    ('hp_cut', 'HP CUT', 80, 1200, 1, '{:.0f} Hz'),
    ('lp_cut', 'LP CUT', 1200, 3900, 1, '{:.0f} Hz'),
    ('nb_rate', 'NB RATE', 6000, 16000, 1000, '{:.0f} Hz'),
    ('opus_bitrate', 'OPUS BITRATE', 6000, 12000, 100, '{:.0f} bps'),
    ('lpc_order', 'LPC ORDER', 4, 20, 1, '{:.0f}'),
    ('lpc_frame_ms', 'LPC FRAME', 10.0, 30.0, 0.5, '{:.1f} ms'),
    ('cvsd_oversample', 'CVSD OVERSAMPLE', 1, 6, 1, '{:.0f}x'),
    ('vocoder_mix', 'VOCODER MIX', 0.0, 1.0, 0.05, '{:.2f}'),
    ('presence_db', 'PRESENCE', -6.0, 9.0, 0.5, '{:+.1f} dB'),
    ('comp_ratio', 'COMP RATIO', 2.0, 20.0, 0.5, '{:.1f}:1'),
    ('comp_thresh_db', 'COMP THRESH', -36.0, -6.0, 0.5, '{:.1f} dB'),
    ('comp_release_ms', 'COMP RELEASE', 40.0, 200.0, 1, '{:.0f} ms'),
    ('sat_drive', 'SAT DRIVE', 0.5, 4.0, 0.05, '{:.2f}'),
    ('fade_depth_db', 'FADE DEPTH', 0.0, 12.0, 0.5, '{:.1f} dB'),
    ('fade_rate_hz', 'FADE RATE', 0.05, 2.0, 0.05, '{:.2f} Hz'),
    ('het_freq_hz', 'HET FREQ', 0.0, 3000.0, 10, '{:.0f} Hz'),
    ('het_level', 'HET LEVEL', 0.0, 0.05, 0.001, '{:.3f}'),
    ('tube_drive', 'TUBE DRIVE', 0.0, 0.6, 0.01, '{:.2f}'),
    ('tube_sag', 'TUBE SAG', 0.0, 1.0, 0.05, '{:.2f}'),
    ('agc_amount', 'AGC AMOUNT', 0.0, 1.0, 0.05, '{:.2f}'),
    ('mpath_delay_ms', 'MPATH DELAY', 0.0, 3.0, 0.1, '{:.1f} ms'),
    ('mpath_doppler_hz', 'MPATH DOPPLER', 0.05, 3.0, 0.05, '{:.2f} Hz'),
    ('mpath_ratio', 'MPATH RATIO', 0.0, 1.0, 0.05, '{:.2f}'),
    ('noise_level', 'NOISE FLOOR', 0.0, 0.06, 0.001, '{:.3f}'),
    ('dropout_prob', 'DROPOUT PROB', 0.0, 0.10, 0.005, '{:.3f}'),
    ('ptt_gain', 'PTT CLICK VOL', 0.0, 1.5, 0.01, '{:.2f}'),
    ('ptt_freq', 'PTT CLICK PITCH', 80, 600, 1, '{:.0f} Hz'),
    ('ptt_dur_ms', 'PTT CLICK LEN', 5.0, 10.0, 0.5, '{:.1f} ms'),
    ('squelch_gain', 'SQUELCH VOL', 0.0, 1.5, 0.01, '{:.2f}'),
    ('squelch_dur_ms', 'SQUELCH LEN', 5.0, 40.0, 0.5, '{:.1f} ms'),
    ('squelch_burst_gain', 'SQUELCH CRASH', 1.0, 2.5, 0.05, '{:.2f}x'),
    ('speaker_res_db', 'SPEAKER BOX', 0.0, 9.0, 0.5, '{:+.1f} dB'),
    ('vox_clip_ms', 'VOX CLIP', 0.0, 20.0, 0.5, '{:.1f} ms'),
    ('output_gain', 'OUTPUT LEVEL', 0.3, 1.5, 0.01, '{:.2f}'),
]

# Codec names accepted by the backend (_radio_config_from_dict + profiles +
# config.json). The Advanced dropdown only offers the two everyday choices;
# 'lpc', 'melp' and 'opus' keep working if set via config.json for A/B tests.
CODEC_CHOICES = ('lpc', 'melp', 'cvsd', 'opus', 'none')
CODEC_UI_CHOICES = ('cvsd', 'none')

# One-line help for every Advanced slider (shown on hover).
DSP_SLIDER_TIPS = {
    'hp_cut': 'High-pass cutoff: removes bass/rumble below this. 300 Hz = classic radio thinness; lower = fuller voice.',
    'lp_cut': 'Low-pass cutoff: removes hiss/air above this. 3400 Hz = phone-like; 2700 Hz = long-range HF; higher = brighter.',
    'nb_rate': 'Internal processing rate. 8000 Hz = real narrowband radio; 16000 Hz = wide broadcast modes (AM/SW profiles).',
    'opus_bitrate': 'Opus voice bitrate in bps. Only matters if codec opus is set via config.json (backend option).',
    'lpc_order': 'LPC vocoder detail (4-20 poles). Higher = clearer formants, more CPU. Only matters for the backend lpc codec.',
    'lpc_frame_ms': 'LPC analysis frame length. ~20 ms matches real vocoders. Only matters for the backend lpc codec.',
    'cvsd_oversample': 'CVSD rate multiplier: x2 = 16 kbps VINSON secure voice, x4 = 32 kbps cleaner. Active with codec cvsd.',
    'vocoder_mix': 'Blend of coded vs clear voice. 1.0 = full crypto grit, 0.5 = half, 0.0 = bypass. Active with codec cvsd.',
    'presence_db': 'Clarity lift above ~2.5 kHz. Positive = crisper consonants; too high = harsh/hissy.',
    'comp_ratio': 'Compressor strength: how flat loud vs quiet speech becomes. Higher = classic squashed radio punch.',
    'comp_thresh_db': 'Compressor threshold: volume level where squashing kicks in. Lower = more of the voice gets squashed.',
    'comp_release_ms': 'Compressor recovery speed after loud peaks. Faster = more pumping; slower = smoother.',
    'sat_drive': 'Grit/distortion amount after the codec. Low = clean, high = crunchy overdriven transmitter.',
    'fade_depth_db': 'Slow fading depth (ionosphere/distance wander). 0 = steady; 4-5 = rolling HF character.',
    'fade_rate_hz': 'Slow fading speed. ~0.3-0.5 Hz = realistic ionospheric wander; higher = seasick flutter.',
    'het_freq_hz': 'Adjacent-station whistle pitch. 0 = off; ~1500-2100 Hz = nighttime AM co-channel whistle.',
    'het_level': 'Adjacent-station whistle volume. Keep tiny (0.006-0.012) or it dominates.',
    'tube_drive': 'Valve warmth: even-harmonic glow of old tube gear. 0 = off; 0.15-0.35 = warm console.',
    'tube_sag': 'Power-supply sag: loud passages briefly duck, like an old set straining. 0 = off.',
    'agc_amount': 'Slow auto-volume that fights fading. Higher = steadier level in AM/HF modes; full = very flat.',
    'mpath_delay_ms': 'Echo path delay (skywave bounce). 0 = off; ~1-1.5 ms = hollow selective-fading notches.',
    'mpath_doppler_hz': 'Echo path shimmer speed. Higher = faster-moving notches and flutter.',
    'mpath_ratio': 'Echo path strength vs direct. Higher = deeper notches and more flutter.',
    'noise_level': 'Background static floor. 0 = silent; ~0.01 clean, ~0.03 noisy, 0.06 heavy storm.',
    'dropout_prob': 'Chance per message of a brief signal cut (5-20 ms). 0.03 = occasional; 0 = never.',
    'ptt_gain': 'Push-to-talk key-down click volume at message start.',
    'ptt_freq': 'Push-to-talk click pitch. Lower = duller thump, higher = sharper snap.',
    'ptt_dur_ms': 'Push-to-talk click length. 5-10 ms is realistic for a switch transient.',
    'squelch_gain': 'Squelch tail click volume at message end (the burst when the channel closes).',
    'squelch_dur_ms': 'Squelch tail click length. Longer = chunkier channel-close thump.',
    'squelch_burst_gain': 'Squelch crash: hiss burst loudness right at carrier drop before mute. 1.0 = off.',
    'speaker_res_db': 'Boxy loudspeaker color around 950 Hz. Higher = smaller, more honky radio speaker.',
    'vox_clip_ms': 'VOX clipping: how much the leading/trailing syllable is nibbled, like real voice-gating.',
    'output_gain': 'Final loudness trim before playback. Use instead of VOLUME if some profiles differ in level.',
}

DEFAULT_PROFILE = 'Air Cover'

# Named profiles use coherent modulation families rather than freely mixing
# unrelated artifacts: narrowband FM with paired pre/de-emphasis, noisy AM
# envelope detection, SSB product detection with tuning error and delayed
# fading paths, or a digital codec path. They are representative classes,
# not bit-exact emulations of a particular fielded radio.
PROFILES = {
    'Tactical Digital': {
        'channel_model': 'digital', 'codec': 'cvsd', 'cvsd_oversample': 2,
        'vocoder_mix': 0.95, 'sync_burst_enabled': True,
        'hp_cut': 350.0, 'lp_cut': 2800.0,
        'comp_ratio': 6.0, 'comp_thresh_db': -16.0, 'sat_drive': 1.6,
        'presence_db': 2.0, 'noise_level': 0.005,
        'dropout_prob': 0.0, 'fade_depth_db': 0.0, 'vox_clip_ms': 4.0,
    },
    'Clear Signal': {
        'channel_model': 'fm', 'channel_snr_db': 42.0, 'codec': 'none',
        'comp_ratio': 3.0, 'comp_thresh_db': -20.0, 'sat_drive': 1.15,
        'presence_db': 3.0, 'noise_level': 0.002, 'dropout_prob': 0.0,
        'fade_depth_db': 0.0, 'vox_clip_ms': 2.0,
    },
    'Secure Military': {
        'channel_model': 'digital', 'codec': 'cvsd', 'cvsd_oversample': 2,
        'vocoder_mix': 0.95, 'sync_burst_enabled': False,
        'hp_cut': 300.0, 'lp_cut': 3400.0, 'comp_ratio': 6.0,
        'comp_thresh_db': -16.0, 'sat_drive': 1.6, 'presence_db': 2.0,
        'noise_level': 0.006, 'dropout_prob': 0.0, 'fade_depth_db': 0.0,
        'vox_clip_ms': 4.0,
    },
    'Long Range HF': {
        'channel_model': 'ssb', 'channel_snr_db': 18.0,
        'ssb_tuning_offset_hz': 14.0, 'codec': 'none',
        'hp_cut': 300.0, 'lp_cut': 2700.0,
        'comp_ratio': 6.0, 'comp_thresh_db': -16.0, 'sat_drive': 1.5,
        'presence_db': 3.0, 'noise_level': 0.016, 'dropout_prob': 0.01,
        'fade_depth_db': 3.0, 'fade_rate_hz': 0.5, 'agc_amount': 0.45,
        'mpath_delay_ms': 1.2, 'mpath_doppler_hz': 0.5, 'mpath_ratio': 0.5,
    },
    'AM Radio Night': {
        'channel_model': 'am', 'channel_snr_db': 25.0,
        'am_modulation_index': 0.70, 'codec': 'none', 'nb_rate': 16000,
        'hp_cut': 100.0, 'lp_cut': 5000.0,
        'comp_ratio': 10.0, 'comp_thresh_db': -14.0, 'sat_drive': 1.8,
        'presence_db': 2.0, 'noise_level': 0.014, 'dropout_prob': 0.03,
        'fade_depth_db': 2.5, 'fade_rate_hz': 0.3,
        'het_freq_hz': 1800.0, 'het_level': 0.008,
        'tube_drive': 0.25, 'tube_sag': 0.35,
        'mpath_delay_ms': 1.0, 'mpath_doppler_hz': 0.5, 'mpath_ratio': 0.5,
        'agc_amount': 0.60,
    },
    'Tube Console': {
        'channel_model': 'am', 'channel_snr_db': 38.0,
        'am_modulation_index': 0.75, 'codec': 'none', 'nb_rate': 16000,
        'hp_cut': 80.0, 'lp_cut': 5000.0,
        'comp_ratio': 8.0, 'comp_thresh_db': -14.0, 'sat_drive': 1.6,
        'presence_db': 2.0, 'noise_level': 0.006, 'dropout_prob': 0.005,
        'fade_depth_db': 0.0,
        'het_freq_hz': 0.0, 'het_level': 0.0,
        'tube_drive': 0.35, 'tube_sag': 0.5,
        'mpath_delay_ms': 0.0,
        'agc_amount': 0.35,
    },
    'Shortwave Broadcast': {
        'channel_model': 'am', 'channel_snr_db': 20.0,
        'am_modulation_index': 0.65, 'codec': 'none', 'nb_rate': 16000,
        'hp_cut': 150.0, 'lp_cut': 4500.0,
        'comp_ratio': 8.0, 'comp_thresh_db': -14.0, 'sat_drive': 1.6,
        'presence_db': 3.0, 'noise_level': 0.018, 'dropout_prob': 0.04,
        'fade_depth_db': 2.5, 'fade_rate_hz': 0.4,
        'het_freq_hz': 1500.0, 'het_level': 0.006,
        'tube_drive': 0.15, 'tube_sag': 0.2,
        'mpath_delay_ms': 1.5, 'mpath_doppler_hz': 1.0, 'mpath_ratio': 0.5,
        'agc_amount': 0.60,
    },
    'Squad Patrol': {
        'channel_model': 'fm', 'channel_snr_db': 32.0, 'codec': 'none',
        'hp_cut': 350.0, 'lp_cut': 2800.0,
        'comp_ratio': 5.5, 'comp_thresh_db': -18.0, 'sat_drive': 1.5,
        'presence_db': 3.0, 'noise_level': 0.007, 'dropout_prob': 0.0,
        'fade_depth_db': 0.0, 'vox_clip_ms': 5.0,
    },
    'Vehicle Convoy': {
        'channel_model': 'fm', 'channel_snr_db': 27.0, 'codec': 'none',
        'hp_cut': 300.0, 'lp_cut': 2800.0,
        'comp_ratio': 7.0, 'comp_thresh_db': -16.0, 'sat_drive': 1.6,
        'presence_db': 2.0, 'noise_level': 0.011, 'dropout_prob': 0.005,
        'fade_depth_db': 0.0, 'tube_drive': 0.0, 'tube_sag': 0.0,
        'agc_amount': 0.0, 'speaker_res_db': 4.5, 'vox_clip_ms': 7.0,
    },
    'Weak Signal': {
        'channel_model': 'fm', 'channel_snr_db': 12.0, 'codec': 'none',
        'hp_cut': 300.0, 'lp_cut': 2800.0,
        'comp_ratio': 8.0, 'comp_thresh_db': -16.0, 'sat_drive': 1.5,
        'presence_db': 3.5, 'noise_level': 0.018, 'dropout_prob': 0.025,
        'fade_depth_db': 0.0, 'agc_amount': 0.0, 'mpath_delay_ms': 0.0,
    },
    'Air Cover': {
        'channel_model': 'am', 'channel_snr_db': 30.0,
        'am_modulation_index': 0.70, 'codec': 'none',
        'hp_cut': 300.0, 'lp_cut': 3000.0, 'comp_ratio': 6.0,
        'comp_thresh_db': -16.0, 'comp_release_ms': 70.0,
        'sat_drive': 1.25, 'presence_db': 3.5, 'noise_level': 0.006,
        'dropout_prob': 0.0, 'fade_depth_db': 0.0,
        'het_freq_hz': 0.0, 'het_level': 0.0, 'agc_amount': 0.15,
        'vox_clip_ms': 4.0, 'sync_burst_enabled': True,
    },
    'Old Walkie': {
        'channel_model': 'fm', 'channel_snr_db': 18.0, 'codec': 'none',
        'hp_cut': 400.0, 'lp_cut': 2700.0,
        'comp_ratio': 10.0, 'comp_thresh_db': -14.0, 'sat_drive': 2.2,
        'presence_db': 1.0, 'noise_level': 0.018, 'dropout_prob': 0.015,
        'fade_depth_db': 0.0, 'tube_drive': 0.1, 'speaker_res_db': 6.0,
        'vox_clip_ms': 10.0,
    },
    'Command Post': {
        'channel_model': 'fm', 'channel_snr_db': 40.0, 'codec': 'none',
        'hp_cut': 250.0, 'lp_cut': 3400.0,
        'comp_ratio': 3.0, 'comp_thresh_db': -20.0, 'sat_drive': 1.2,
        'presence_db': 4.5, 'noise_level': 0.006, 'dropout_prob': 0.0,
        'fade_depth_db': 0.0, 'output_gain': 0.9, 'speaker_res_db': 2.5,
    },
}

PROFILE_INFO = {
    'Tactical Digital': 'Secure digital voice (16 kbps CVSD) with sync burst and restricted 350–2800 Hz audio.',
    'Clear Signal': 'Strong narrowband FM link with pre/de-emphasis and maximum intelligibility.',
    'Secure Military': 'Historically inspired 16 kbps CVSD secure voice; not a bit-exact VINSON emulator.',
    'Long Range HF': 'SSB product detection, slight tuning error and two-path ionospheric fading.',
    'AM Radio Night': 'Medium-wave AM after dark: 5 kHz audio, tube output stage, selective + flat fading, het whistle.',
    'Tube Console': 'Strong local AM on a valve console: warm, sagging, full 5 kHz, barely any static.',
    'Shortwave Broadcast': 'Distant SW broadcast band: fast selective flutter, fading, co-channel het.',
    'Squad Patrol': 'Short-range narrowband FM handheld: limited, punchy and lightly noisy.',
    'Vehicle Convoy': 'Vehicle-mounted narrowband FM with a boxier speaker and moderate link noise.',
    'Weak Signal': 'Low-SNR narrowband FM near the intelligibility threshold.',
    'Air Cover': 'Default: aviation-style AM voice with sync burst and a clean line-of-sight link.',
    'Old Walkie': 'Older narrowband FM handheld: restricted, noisy and strongly speaker-colored.',
    'Command Post': 'Strong base-station FM link: broad voice response and gentle compression.',
    'Custom': 'Manual slider settings.',
}

DEFAULT_CONFIG = {
    'engine_mode': 'NEURAL',
    'neural_ally': ALLY_VOICE,
    'neural_enemy': ENEMY_VOICE,
    'piper_voice': PIPER_VOICE,
    'piper_ally': PIPER_VOICE,
    'piper_enemy': PIPER_ENEMY_VOICE,
    'rate': 1.0,
    'radio': {'enabled': True, 'noise': 0.8, 'ptt': True, 'eq': True, 'volume': 1.0,
              'mute_default_messages': True, **DSP_DEFAULTS,
              **PROFILES[DEFAULT_PROFILE], 'profile': DEFAULT_PROFILE},
}

RADIO_CFG = dict(DEFAULT_CONFIG['radio'])

LOG_PATH = DATA_DIR / 'squelchwt.log'
logger = logging.getLogger('squelchwt')
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _file_handler = logging.FileHandler(LOG_PATH, encoding='utf-8')
    _file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logger.addHandler(_file_handler)


def load_config():
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        logger.warning('could not load config, using defaults: %s', exc)
        data = {}
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))

    def apply(src, dst):
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                apply(value, dst[key])
            else:
                dst[key] = value

    apply(data, cfg)
    # Configurations written before modulation-aware profiles existed must
    # retain their exact established processing. The user opts into a new
    # model by explicitly selecting a named profile again.
    if isinstance(data.get('radio'), dict) and 'channel_model' not in data['radio']:
        cfg['radio']['channel_model'] = 'legacy'
    if 'piper_ally' not in data:
        cfg['piper_ally'] = cfg['piper_voice']
    for key in list(cfg):
        if key not in DEFAULT_CONFIG:
            del cfg[key]
    return cfg


def save_config(cfg):
    temporary = CONFIG_PATH.with_suffix('.json.tmp')
    try:
        temporary.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
        os.replace(temporary, CONFIG_PATH)
        return True
    except OSError as exc:
        logger.warning('could not save config: %s', exc)
        return False

INSTANCE_LOCK = None


def acquire_instance_lock():
    global INSTANCE_LOCK
    INSTANCE_LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        INSTANCE_LOCK.bind(('127.0.0.1', 48711))
        INSTANCE_LOCK.listen(1)
    except OSError:
        INSTANCE_LOCK.close()
        INSTANCE_LOCK = None
        raise SystemExit('SquelchWT is already running.')


_piper_cache = {}
_piper_failed = set()
_piper_lock = threading.Lock()


_CHAT_CONN = None
_CHAT_CONN_LOCK = threading.Lock()


def _drop_chat_conn():
    global _CHAT_CONN
    try:
        if _CHAT_CONN is not None:
            _CHAT_CONN.close()
    except Exception:
        pass
    _CHAT_CONN = None


def fetch_chat(last_id):
    global _CHAT_CONN
    with _CHAT_CONN_LOCK:
        for attempt in (0, 1):
            if _CHAT_CONN is None:
                parts = urlsplit(API_ROOT)
                if parts.scheme not in ('http', 'https') or not parts.hostname or parts.path:
                    raise ValueError('SQUELCHWT_API_ROOT must be an HTTP(S) origin such as http://127.0.0.1:8111')
                port = parts.port or (443 if parts.scheme == 'https' else 80)
                if parts.scheme == 'https':
                    import ssl
                    context = ssl.create_default_context()
                    _CHAT_CONN = http.client.HTTPSConnection(parts.hostname, port,
                                                             timeout=0.35, context=context)
                else:
                    _CHAT_CONN = http.client.HTTPConnection(parts.hostname, port,
                                                            timeout=0.35)
            response = None
            try:
                _CHAT_CONN.request('GET', f'/gamechat?lastId={last_id}',
                                   headers={'Cache-Control': 'no-cache'})
                response = _CHAT_CONN.getresponse()
                if response.status != 200:
                    raise ValueError(f'chat HTTP {response.status}')
                data = json.loads(response.read())
                if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                    raise ValueError('game chat returned an unexpected response')
                closing = response.will_close
                response.close()
                response = None
                if closing:
                    _drop_chat_conn()
                return data
            except Exception:
                _drop_chat_conn()
                if attempt == 1:
                    raise
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass


SLANG_PATH = ASSET_DIR / 'slang.json'
MUTED_PHRASES_PATH = DATA_DIR / 'muted_phrases.json'
if getattr(sys, 'frozen', False) and not MUTED_PHRASES_PATH.exists():
    shutil.copyfile(ASSET_DIR / 'muted_phrases.json', MUTED_PHRASES_PATH)


def load_slang():
    try:
        data = json.loads(SLANG_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return {str(k).strip().lower(): str(v).strip() for k, v in data.items() if k and v}


SLANG = load_slang()
SLANG_RE = re.compile(r"(?<!\w)([\w']+)(?!\w)", re.UNICODE)


def _phrase_key(text):
    return re.sub(r'\s+', ' ', str(text).casefold()).strip().rstrip('!?.,;:')


def load_muted_phrases():
    try:
        data = json.loads(MUTED_PHRASES_PATH.read_text(encoding='utf-8'))
        exact = {_phrase_key(value) for value in data.get('exact', []) if str(value).strip()}
        prefixes = tuple(_phrase_key(value) for value in data.get('starts_with', []) if str(value).strip())
        return exact, prefixes
    except (OSError, ValueError, TypeError) as exc:
        logger.warning('could not load muted phrases: %s', exc)
        return set(), ()


MUTED_EXACT, MUTED_PREFIXES = load_muted_phrases()


def is_default_radio_message(text):
    phrase = _phrase_key(text)
    return bool(phrase) and (
        phrase in MUTED_EXACT
        or any(phrase == prefix or phrase.startswith(prefix + ' ') for prefix in MUTED_PREFIXES)
    )


def normalize_slang(text):
    if not SLANG or not text:
        return text
    return SLANG_RE.sub(lambda m: SLANG.get(m.group(1).lower(), m.group(1)), text)


def _encode_wav(samples, sample_rate):
    result = tempfile.SpooledTemporaryFile()
    with wave.open(result, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes((np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    result.seek(0)
    return result.read()


def _radio_config_from_dict(radio) -> RadioConfig:
    """Map the persisted radio dict onto the new RadioConfig dataclass.

    Generic overlay: every key matching a RadioConfig field is applied
    (with light type coercion), so new DSP fields automatically work
    together with the GUI, profiles, and config.json without a hardcoded
    field list drifting out of sync. UI aliases: noise->noise_amount,
    eq->eq_enabled, ptt->ptt_enabled. 'profile' is GUI-only, ignored.
    """
    r = radio if isinstance(radio, dict) else RADIO_CFG
    codec = str(r.get('codec', DSP_DEFAULTS.get('codec', 'cvsd'))).lower()
    if codec not in CODEC_CHOICES:
        logger.warning('unknown radio codec %r, falling back to cvsd', codec)
        codec = 'cvsd'
    seed_raw = r.get('seed', None)
    seed = None
    if isinstance(seed_raw, (int, float)) and int(seed_raw) >= 0:
        seed = int(seed_raw)
    base = RadioConfig(codec=codec, seed=seed)
    alias = {'noise': 'noise_amount', 'eq': 'eq_enabled', 'ptt': 'ptt_enabled'}
    for key, value in r.items():
        if key in ('profile', 'codec', 'seed'):
            continue
        target = alias.get(key, key)
        if not hasattr(base, target):
            continue
        try:
            current = getattr(base, target)
            if isinstance(current, bool):
                setattr(base, target, bool(value))
            elif isinstance(current, int) and not isinstance(current, bool):
                setattr(base, target, int(float(value)))
            elif isinstance(current, float):
                setattr(base, target, float(value))
            elif isinstance(current, str):
                setattr(base, target, str(value))
            else:
                setattr(base, target, value)
        except (TypeError, ValueError):
            logger.warning('ignoring invalid radio value %r=%r', key, value)
    # Explicit UI mappings that need float() tolerance for slider strings.
    try:
        base.noise_amount = float(r.get('noise', base.noise_amount))
    except (TypeError, ValueError):
        pass
    return base


_TLS = threading.local()


def _get_processor(cfg: RadioConfig) -> RadioProcessor:
    # One instance per synthesis thread (the pool runs 3 workers): sharing a
    # single mutable processor across threads lets concurrent ally/enemy jobs
    # overwrite each other's config mid-flight. The reproducible-seed counter
    # survives per thread: with seed set the sequence is deterministic yet
    # still varies per transmission (no "looped" sound); with seed None every
    # call uses OS entropy. process() itself is otherwise stateless.
    key = ('seeded', int(cfg.seed)) if cfg.seed is not None else ('shared', 0)
    cache = getattr(_TLS, 'procs', None)
    if cache is None:
        cache = {}
        _TLS.procs = cache
    proc = cache.get(key)
    if proc is None:
        proc = RadioProcessor(config=cfg)
        cache[key] = proc
    else:
        calls = int(getattr(proc.config, '_calls', 0))
        proc.config = cfg
        proc.config._calls = calls
    return proc


def _radio_wav(samples, sample_rate, enemy=False, mode='Team', radio=None):
    """Thin adapter: raw TTS PCM -> RadioProcessor -> WAV bytes.

    TTS call sites are untouched DSP-wise; swap/disable/A-B the radio sound
    by changing config or by calling RadioProcessor directly.
    """
    r = radio if radio is not None else RADIO_CFG
    if not isinstance(samples, np.ndarray):
        samples = np.asarray(samples, dtype=np.float32)
    cfg = _radio_config_from_dict(r)
    if enemy or str(mode).lower() == 'all':
        cfg.noise_level = float(cfg.noise_level) * float(cfg.enemy_noise_mult)
        cfg.dropout_prob = float(cfg.dropout_prob) * float(cfg.enemy_drop_mult)
    try:
        processed = _get_processor(cfg).process(samples, int(sample_rate))
    except Exception as exc:
        # Never drop a spoken message because the FX chain failed; play it
        # dry (peak-normalized) and log so the failure is visible.
        logger.warning('radio DSP failed, playing dry audio: %s', exc)
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        processed = (samples / peak * 0.8).astype(np.float32) \
            if peak > 0 else samples.astype(np.float32)
    return _encode_wav(processed, int(sample_rate))


def tts_neural(text, speed, voice, enemy=False, mode='Team', radio=None):
    path = os.path.join(tempfile.gettempdir(), f'squelchwt-{os.getpid()}-{threading.get_ident()}.mp3')
    try:
        rate = f'{int((max(0.6, min(2.5, speed)) - 1) * 100):+d}%'
        for attempt in range(3):
            try:
                async def synthesize_with_timeout():
                    await asyncio.wait_for(edge_tts.Communicate(text[:500], voice, rate=rate).save(path), 20)
                asyncio.run(synthesize_with_timeout())
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    break
            except Exception as exc:
                if attempt == 2:
                    raise
                logger.debug('neural TTS attempt %d failed, retrying: %s', attempt + 1, exc)
                time.sleep(0.12)
        decoded = miniaudio.decode_file(path, miniaudio.SampleFormat.SIGNED16,
                                        nchannels=1, sample_rate=24000).samples
        sample_rate = 24000
        samples = np.asarray(decoded, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            raise ValueError('Neural TTS returned no audio')
        return _radio_wav(samples, sample_rate, enemy, mode, radio=radio)
    finally:
        if os.path.exists(path):
            os.remove(path)


def _piper_config_valid(path):
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
        return int(config['audio']['sample_rate']) > 0
    except (OSError, ValueError, KeyError, TypeError):
        return False


def piper_installed(model):
    model_path = VOICES_DIR / f'{model}.onnx'
    config_path = VOICES_DIR / f'{model}.onnx.json'
    return model_path.is_file() and model_path.stat().st_size > 0 and _piper_config_valid(config_path)


def piper_failed(model):
    return model in _piper_failed


def load_piper(model=None):
    model = model or PIPER_VOICE
    if model in _piper_cache:
        return _piper_cache[model]
    if model in _piper_failed or not piper_installed(model):
        return None
    with _piper_lock:
        if model in _piper_cache:
            return _piper_cache[model]
        try:
            from piper import PiperVoice
            _piper_cache[model] = PiperVoice.load(VOICES_DIR / f'{model}.onnx')
        except Exception as exc:
            logger.warning('Piper load failed for %s: %s', model, exc)
            _piper_failed.add(model)
    return _piper_cache.get(model)


def download_piper_model(model=None, progress=None, force=False):
    """Download the model and config with per-byte progress and atomic file writes."""
    from piper import download_voices
    model = model or PIPER_VOICE
    match = download_voices.VOICE_PATTERN.match(model)
    if not match or model not in PIPER_PRESET_BY_MODEL:
        raise ValueError(f'Unknown Piper model: {model}')
    parts = match.groupdict()
    lang_code = parts['lang_family'] + '_' + parts['lang_region']
    fields = {'lang_family': parts['lang_family'], 'lang_code': lang_code,
              'voice_name': parts['voice_name'], 'voice_quality': parts['voice_quality']}
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    for suffix, start, weight in (('.onnx', 0, 95), ('.onnx.json', 95, 5)):
        path = VOICES_DIR / f'{model}{suffix}'
        ready = path.is_file() and path.stat().st_size > 0
        if suffix == '.onnx.json':
            ready = ready and _piper_config_valid(path)
        if ready and not force:
            if progress:
                progress(start + weight)
            continue
        url = download_voices.URL_FORMAT.format(extension=suffix, **fields)
        temporary = path.with_name(path.name + '.part')
        try:
            with urlopen(url, timeout=30) as response, temporary.open('wb') as output:
                total = int(response.headers.get('Content-Length') or 0)
                fallback = PIPER_PRESET_BY_MODEL[model][1] * 1024 * 1024 if suffix == '.onnx' else 32768
                expected = total or fallback
                received = 0
                while chunk := response.read(128 * 1024):
                    output.write(chunk)
                    received += len(chunk)
                    if progress:
                        progress(start + min(weight - 1, int(weight * received / expected)))
                if total and received != total:
                    raise OSError(f'Incomplete model download: {received}/{total} bytes')
                if received == 0:
                    raise OSError('Empty model download')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        if progress:
            progress(start + weight)
    if not piper_installed(model):
        raise OSError('Piper model and config were not both installed')


def set_piper_voice(name):
    global PIPER_VOICE
    PIPER_VOICE = name
    _piper_failed.discard(name)


def _synth_piper(voice, text, speed):
    from piper import SynthesisConfig
    length = float(np.clip(1.0 / max(0.5, min(2.5, speed)), 0.4, 2.0))
    chunks = voice.synthesize(text[:500], SynthesisConfig(length_scale=length, noise_scale=0.667))
    return np.concatenate([c.audio_float_array for c in chunks])


def _synth_sapi(text, speed, enemy, mode, radio=None):
    path = os.path.join(tempfile.gettempdir(), f'squelchwt-sapi-{os.getpid()}-{threading.get_ident()}.wav')
    try:
        ps_rate = int(np.clip((speed - 1.0) * 6.0, -10.0, 10.0))
        build = path.replace('\\', '/')
        voice_name = 'Mark' if enemy else 'David'
        script = (f"$ErrorActionPreference='Stop'\n"
                  f"Add-Type -AssemblyName System.Speech\n"
                  f"$s=New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                  f"$s.SetOutputToWaveFile('{build}')\n"
                  f"$s.Rate={ps_rate}\n"
                  f"try {{ $s.SelectVoice('Microsoft {voice_name} Desktop') }} catch {{}}\n"
                  f"$s.Speak($env:SQUELCHWT_TTS_TEXT)\n"
                  f"$s.SetOutputToNull()\n"
                  f"$s.Dispose()")
        env = dict(os.environ)
        env['SQUELCHWT_TTS_TEXT'] = text[:160]
        subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                       check=True, capture_output=True, env=env, timeout=20,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with wave.open(path, 'rb') as w:
            sr = w.getframerate()
            channels = w.getnchannels()
            raw = w.readframes(w.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if samples.size == 0:
            raise ValueError('SAPI returned no audio')
        return _radio_wav(samples, int(sr), enemy, mode, radio=radio)
    finally:
        if os.path.exists(path):
            os.remove(path)


def tts_local(text, speed, enemy=False, mode='Team', force_sapi=False, radio=None, model=None):
    if not force_sapi:
        voice = load_piper(model)
        if voice is not None:
            samples = _synth_piper(voice, text, speed)
            if samples.size == 0:
                raise ValueError('Piper returned no audio')
            return _radio_wav(samples, int(voice.config.sample_rate), enemy, mode, radio=radio)
    return _synth_sapi(text, speed, enemy, mode, radio=radio)


def local_backend_label(model=None):
    model = model or PIPER_VOICE
    if model in _piper_cache:
        return 'PIPER RDY'
    if model in _piper_failed:
        return 'SAPI (fallback)'
    return 'PIPER'


class RadioService:
    """UI-independent orchestration. Dispatch must marshal callbacks to the UI thread."""
    def __init__(self, dispatch, changed, record, fault):
        self.dispatch = dispatch
        self.changed = changed
        self.record = record
        self.fault = fault
        self.downloading = False
        self.download_target = None
        self.download_percent = 0
        self.test_busy = False
        self.synth_waiting = False
        self.last_id = 0
        self.initialized = False
        self.running = True
        self.connected = False
        self.speaking = False
        self.muted = False
        self.cfg = load_config()
        global PIPER_VOICE, RADIO_CFG
        self.engine_mode = self.cfg['engine_mode']
        self.neural_ally = self.cfg['neural_ally']
        self.neural_enemy = self.cfg['neural_enemy']
        self.piper_ally = self.cfg['piper_ally']
        self.piper_enemy = self.cfg['piper_enemy']
        self.piper_voice = self.piper_ally  # legacy API/config alias
        PIPER_VOICE = self.piper_ally
        allowed = set(DSP_DEFAULTS) | {'enabled', 'noise', 'ptt', 'eq', 'volume', 'profile',
                                      'mute_default_messages'}
        self.cfg['radio'] = {k: v for k, v in self.cfg['radio'].items() if k in allowed}
        for key, value in DSP_DEFAULTS.items():
            self.cfg['radio'].setdefault(key, value)
        self.cfg['radio'].setdefault('profile', DEFAULT_PROFILE)
        RADIO_CFG = self.cfg['radio']
        self.rate = self.cfg['rate']
        self.pending = deque(maxlen=8)
        self.pending_lock = threading.Lock()
        self.cfg_lock = threading.RLock()
        self.poll_lock = threading.Lock()
        self.speech_event = threading.Event()
        self.play_queue = deque(maxlen=3)
        self.play_lock = threading.Lock()
        self.play_event = threading.Event()
        self.prep = OrderedDict()
        self.next_seq = 0
        self._test_seqs = set()
        self.executor = ThreadPoolExecutor(max_workers=SYNTH_WORKERS, thread_name_prefix='tts')

    def start(self):
        self.worker = threading.Thread(target=self.speech_worker, daemon=True)
        self.player = threading.Thread(target=self.player_worker, daemon=True)
        self.worker.start()
        self.player.start()
        if self.engine_mode == 'LOCAL':
            threading.Thread(target=self._warm_selected_local, daemon=True).start()

    def _post(self, fn, *args):
        if self.running:
            self.dispatch(fn, args)

    def refresh_status_ui(self):
        self.changed()

    def add_line(self, record):
        self.record(record)

    def report_fault(self, message):
        self._post(self.fault, message)

    def poll(self):
        if self.running and not self.poll_lock.locked():
            threading.Thread(target=self.poll_once, daemon=True).start()

    def stop(self):
        self.save_cfg()
        self.running = False
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.speech_event.set()
        self.play_event.set()
        with self.pending_lock:
            self.pending.clear()
        with self.play_lock:
            self.play_queue.clear()
        if INSTANCE_LOCK is not None:
            INSTANCE_LOCK.close()

    def set_radio(self, key, value, custom=True):
        with self.cfg_lock:
            self.cfg['radio'][key] = value
            if custom:
                self.cfg['radio']['profile'] = 'Custom'
        self.refresh_status_ui()

    def select_profile(self, name):
        if name not in PROFILES:
            return
        with self.cfg_lock:
            self.cfg['radio'].update(DSP_DEFAULTS)
            self.cfg['radio'].update(PROFILES[name])
            self.cfg['radio']['profile'] = name
        self.save_cfg()
        self.refresh_status_ui()

    def set_voice(self, which, voice):
        if which == 'ally':
            self.neural_ally = voice
        elif which == 'enemy':
            self.neural_enemy = voice
        else:
            if which in ('piper', 'piper_ally'):
                self.piper_ally = voice
                self.piper_voice = voice
                set_piper_voice(voice)
            elif which == 'piper_enemy':
                self.piper_enemy = voice
            else:
                raise ValueError(f'Unknown voice role: {which}')
            if piper_installed(voice):
                threading.Thread(target=self._warm_local, args=(voice,), daemon=True).start()
        self.save_cfg()
        self.refresh_status_ui()

    def download_voice(self, which='ally'):
        if self.downloading:
            return
        self.downloading = True
        model = self.piper_enemy if which == 'enemy' else self.piper_ally
        self.download_target = model
        self.download_percent = 0
        self.refresh_status_ui()
        threading.Thread(target=self._download, args=(model,), daemon=True).start()

    def _download_progress(self, percent):
        percent = max(0, min(100, int(percent)))
        if percent != self.download_percent:
            self.download_percent = percent
            self._post(self.refresh_status_ui)

    def _download(self, model):
        try:
            download_piper_model(model, self._download_progress, force=model in _piper_failed)
            _piper_failed.discard(model)
            if model in (self.piper_ally, self.piper_enemy):
                if load_piper(model) is None:
                    self.report_fault('OFFLINE VOICE UNAVAILABLE  /  Reinstall it in VOICE.')
        except Exception:
            logger.exception('voice download failed')
            self.report_fault('VOICE DOWNLOAD FAILED  /  Check internet and retry in VOICE.')
        finally:
            self.downloading = False
            self.download_target = None
            self._post(self.refresh_status_ui)

    def save_cfg(self):
        self.cfg['engine_mode'] = self.engine_mode
        self.cfg['neural_ally'] = self.neural_ally
        self.cfg['neural_enemy'] = self.neural_enemy
        self.cfg['piper_voice'] = self.piper_voice
        self.cfg['piper_ally'] = self.piper_ally
        self.cfg['piper_enemy'] = self.piper_enemy
        self.cfg['rate'] = self.rate
        ok = save_config(self.cfg)
        if not ok:
            self.report_fault('CONFIG NOT SAVED  /  Check folder write access and retry.')
        return ok

    def _do_reset(self):
        global PIPER_VOICE, RADIO_CFG
        try:
            if CONFIG_PATH.exists():
                shutil.copyfile(CONFIG_PATH, CONFIG_BACKUP_PATH)
        except OSError as exc:
            logger.warning('could not back up config before reset: %s', exc)
        with self.cfg_lock:
            fresh = json.loads(json.dumps(DEFAULT_CONFIG))
            self.cfg.clear()
            self.cfg.update(fresh)
            self.engine_mode = self.cfg['engine_mode']
            self.neural_ally = self.cfg['neural_ally']
            self.neural_enemy = self.cfg['neural_enemy']
            self.piper_ally = self.cfg['piper_ally']
            self.piper_enemy = self.cfg['piper_enemy']
            self.piper_voice = self.piper_ally
            set_piper_voice(self.piper_ally)
            RADIO_CFG = self.cfg['radio']
            self.rate = self.cfg['rate']
        PIPER_VOICE = self.piper_ally
        logger.info('settings reset to factory defaults')
        try:
            save_config(self.cfg)
            self.refresh_status_ui()
        except Exception:
            traceback.print_exc()

    def toggle_mute(self):
        self.muted = not self.muted
        self.refresh_status_ui()

    def poll_once(self):
        if not self.poll_lock.acquire(blocking=False):
            return
        try:
            records = fetch_chat(self.last_id)
            if records:
                was_initialized = self.initialized
                self.last_id = int(records[-1].get('id', self.last_id))
                recent = records[-12:] if not was_initialized else records
                self.initialized = True
                for record in recent:
                    self._post(self.add_line, record)
                if was_initialized:
                    queued = False
                    for record in records:
                        if self.cfg['radio'].get('mute_default_messages', True) \
                                and is_default_radio_message(record.get('msg', '')):
                            continue
                        with self.pending_lock:
                            # Keep an accepted test in the bounded queue during chat bursts.
                            if len(self.pending) == self.pending.maxlen:
                                oldest = next((r for r in self.pending if not r.get('test')), None)
                                if oldest is not None:
                                    self.pending.remove(oldest)
                            self.pending.append(record)
                            queued = True
                    if queued:
                        self.speech_event.set()
            self._post(self.set_status, True)
        except Exception:
            # A restarted game starts its chat IDs from a new session. Re-seed
            # on reconnection so the first batch is displayed without speech.
            self.last_id = 0
            self.initialized = False
            self._post(self.set_status, False)
        finally:
            self.poll_lock.release()

    def set_status(self, connected):
        if connected == self.connected:
            return
        logger.info('game chat link %s', 'up' if connected else 'down')
        self.connected = connected
        self.refresh_status_ui()

    def change_rate(self, delta):
        self.rate = round(max(0.5, min(2.5, self.rate + delta)), 1)
        self.save_cfg()
        self.refresh_status_ui()

    def set_engine(self, mode):
        self.engine_mode = mode
        if mode == 'LOCAL':
            threading.Thread(target=self._warm_selected_local, daemon=True).start()
        self.save_cfg()
        self.refresh_status_ui()

    def _warm_selected_local(self):
        for model in dict.fromkeys((self.piper_ally, self.piper_enemy)):
            if self.running and piper_installed(model) and model not in _piper_cache:
                load_piper(model)
                self._post(self.refresh_status_ui)

    def _warm_local(self, model=None):
        load_piper(model)
        self._post(self.refresh_status_ui)

    def _test_voice(self, engine, voice=None, enemy=False):
        if not self.running or self.test_busy:
            return
        with self.pending_lock:
            if any(r.get('test') for r in self.pending):
                return
        if self._test_seqs:
            return
        if self.speaking:
            return
        with self.play_lock:
            if self.play_queue:
                return
        with self.pending_lock:
            self.test_busy = True
            record = {'sender': 'TEST', 'msg': TEST_PHRASE, 'mode': 'Team', 'enemy': enemy,
                      'force_engine': engine, 'test': True}
            if voice:
                record['force_voice'] = voice
            self.pending.append(record)
        self.refresh_status_ui()
        self.speech_event.set()

    def synthesize(self, record):
        text = normalize_slang(str(record.get('msg', '')).strip())
        if not text:
            return None
        enemy = bool(record.get('enemy', False))
        mode = str(record.get('mode', 'Team'))
        speed = max(0.5, min(2.5, self.rate))
        with self.cfg_lock:
            radio = dict(self.cfg['radio'])
        engine = record.get('force_engine') or self.engine_mode
        if engine == 'NEURAL':
            voice = record.get('force_voice') or (self.neural_enemy if enemy else self.neural_ally)
            return tts_neural(text, speed, voice, enemy, mode, radio=radio)
        voice = record.get('force_voice') or (self.piper_enemy if enemy else self.piper_ally)
        return tts_local(text, speed, enemy, mode, radio=radio, model=voice)

    def _fill_prep(self):
        while self.running and len(self.prep) < 3:
            with self.pending_lock:
                record = self.pending.popleft() if self.pending else None
            if not record:
                break
            seq = self.next_seq
            self.next_seq += 1
            if record.get('test'):
                self._test_seqs.add(seq)
            try:
                self.prep[seq] = (self.executor.submit(self.synthesize, record), bool(record.get("test")))
            except Exception as exc:
                logger.warning('could not submit synthesis job: %s', exc)
                with self.pending_lock:
                    self.pending.appendleft(record)
                return

    def process_burst(self):
        self._fill_prep()
        while self.prep:
            if not self.running:
                self.prep.clear()
                return
            seq, (fut, is_test) = next(iter(self.prep.items()))
            with self.play_lock:
                space = len(self.play_queue) < 3
            if not space:
                time.sleep(0.02)
                continue
            self.prep.pop(seq)
            self._test_seqs.discard(seq)
            self.synth_waiting = True
            try:
                audio = fut.result()
            except Exception as exc:
                logger.warning('synthesis failed: %s', exc)
                self.report_fault('VOICE FAILED  /  Check internet or select LOCAL in VOICE.')
                if is_test:
                    self.test_busy = False
                    self._post(self.refresh_status_ui)
                continue
            finally:
                self.synth_waiting = False
            if not audio or self.muted or not self.running:
                if is_test:
                    self.test_busy = False
                    self._post(self.refresh_status_ui)
                self._fill_prep()
                continue
            with self.play_lock:
                self.play_queue.append((audio, is_test))
                self.play_event.set()
            self._fill_prep()

    def speech_worker(self):
        while self.running:
            try:
                self.process_burst()
            except Exception:
                logger.exception('speech worker error')
            if not self.running:
                break
            self.speech_event.wait(0.05)
            self.speech_event.clear()

    def player_worker(self):
        while self.running:
            self.play_event.wait(0.03)
            self.play_event.clear()
            with self.play_lock:
                item = self.play_queue.popleft() if self.play_queue else None
            if not item:
                continue
            audio, is_test = item
            if not self.running:
                break
            try:
                self.speaking = True
                self._post(self.refresh_status_ui)
                winsound.PlaySound(audio, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
            except Exception as exc:
                logger.warning('audio playback failed: %s', exc)
                self.report_fault('PLAYBACK ERROR  /  Check your Windows audio output.')
            finally:
                self.speaking = False
                if is_test:
                    self.test_busy = False
                self._post(self.refresh_status_ui)
